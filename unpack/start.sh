#!/bin/bash

set -u

LOG=/logs/unpack.log
STILL_ENCRYPTED=/status/still_encrypted.txt
STILL_CORRUPT=/status/still_corrupt.txt
STILL_UNSAFE=/status/still_unsafe.txt
STILL_MULTIVOLUME=/status/still_multivolume.txt
DECRYPTED=/status/decrypted.txt
MAX_DEPTH_DEFAULT=6
PARALLELISM="${PARALLELISM:-$(command -v nproc > /dev/null && nproc || echo 4)}"
# Defaults for the hostile-archive guards in check_archive_safety() and the
# per-extraction timeout below - generous, since multi-GB archives and
# multi-minute 7-Zip/readpst runs are normal for real leak dumps, but bound
# to something rather than nothing. All overridable per deis.cfg.default's
# [unpack] section. Exported (unlike MAX_DEPTH_DEFAULT above, only ever read
# in the parent process's own unpack() loop): check_archive_safety() and
# try_extract() run inside xargs -P's worker processes, which only inherit
# exported variables, not plain ones - found by testing this against a real
# archive rather than assuming export -f for the functions was enough.
export MAX_EXTRACT_BYTES_DEFAULT=$((10 * 1024 * 1024 * 1024)) # 10 GiB uncompressed
export MAX_COMPRESSION_RATIO_DEFAULT=200                      # uncompressed:compressed
export EXTRACT_TIMEOUT_DEFAULT=1800                           # seconds per archive

log() {
    # $1 = level (EXTRACTED / COPIED / ENCRYPTED / CORRUPT / UNSAFE / RENAMED / MULTIVOLUME / DEPTH-LIMIT / OCR / TABLES / DECRYPTED)
    # $2 = message
    echo "$2"
    echo "$(date -Iseconds) [$1] $2" >> "${LOG}"
}

# Reads a single key from /deis.cfg's [unpack] section - the only section
# this script ever reads. Unlike a plain substring grep, a key in a
# different section, or a key that merely contains this name as a substring
# (e.g. "not_unpack=true" when reading "unpack"), is not matched.
read_cfg() {
    awk -F= -v key="$1" '
        /^\[/ { insection = ($0 == "[unpack]"); next }
        insection && $1 == key { print substr($0, index($0, "=") + 1); found = 1 }
        END { exit !found }
    ' /deis.cfg
}

config_true() {
    [[ "$(read_cfg "$1")" == "true" ]]
}

# Same as config_true, but distinguishes "key set to something other than
# true" from "key not in the file at all", falling back to $2 in the latter
# case. deis.cfg is gitignored and 'deis init' deliberately leaves an
# existing one alone, so a key added to deis.cfg.default later never appears
# in the config of anyone who set the project up before it existed - and
# config_true reads that absence as "false", silently disabling a feature
# documented as on by default. read_cfg already exits non-zero when the key
# is missing, which is what makes the distinction available here.
config_true_default() {
    local value
    value="$(read_cfg "$1")" || { [[ "$2" == "true" ]]; return; }
    [[ "${value}" == "true" ]]
}

config_int() {
    local value
    value="$(read_cfg "$1")"
    [[ "${value}" =~ ^[0-9]+$ ]] && echo "${value}" || echo "$2"
}

# Every password worth trying, in order: unencrypted first (most files), then
# ZIP_PASSWORD (kept for backward compatibility), then anything in
# passwords/*.txt - one password per line, blank lines and #comments ignored.
# Deduplicated so the same password is never tried twice against one file.
build_password_list() {
    printf '%s\n' ""
    [[ -n "${ZIP_PASSWORD:-}" ]] && printf '%s\n' "${ZIP_PASSWORD}"
    if [[ -d /passwords ]]; then
        find /passwords -maxdepth 1 -type f -name '*.txt' -print0 2>/dev/null \
            | xargs -0 -r cat -- \
            | sed -e 's/\r$//' \
            | grep -vE '^[[:space:]]*(#|$)'
    fi
}

# Loads PASSWORDS from the file the parent wrote once per unpack() run.
# Needed because each round's extraction workers are separate processes
# (spawned by xargs -P, not just backgrounded subshells of this one) and bash
# cannot export an array into a child process's environment.
load_passwords() {
    mapfile -t PASSWORDS < "${WORKDIR}/passwords.list"
}

# Prints a version of path component $1 that's safe to write to a
# bind-mounted host filesystem enforcing valid UTF-8 names (macOS, via
# Docker Desktop's virtiofs) - legacy zip archives often store a non-ASCII
# filename (Swedish å/ä/ö, seen in the wild) in a codepage like
# CP437/Windows-1252 without the zip "UTF-8 names" flag, so 7-Zip decodes
# it into a byte string that isn't valid UTF-8. Unmodified, that's
# invisible on a Linux host filesystem (any byte string but NUL/'/' is a
# legal name there), but a macOS host rejects the write outright with
# "Operation not permitted" - confirmed by extracting the same archive
# into this container's own (Linux) filesystem instead of the bind mount.
# Only once iconv confirms the component is genuinely invalid does this
# percent-encode every non-ASCII byte - losslessly, the original bytes
# are recoverable - so the write can never be rejected.
sanitize_component() {
    local component="$1"
    if printf '%s' "${component}" | iconv -f UTF-8 -t UTF-8 > /dev/null 2>&1; then
        printf '%s' "${component}"
    else
        printf '%s' "${component}" | LC_ALL=C perl -pe 's/([\x80-\xFF])/sprintf("%%%02X", ord($1))/ge'
    fi
}

# Copies $1 into directory $2 unless it's already somewhere under $2 (a
# file discovered inside a parent's own extraction directory is already at
# a correctly sha256-namespaced path - copying it again into $2 directly
# would flatten it under its bare basename, reintroducing exactly the
# cross-archive filename collisions that namespacing exists to avoid).
# Falls back to a sanitized basename (see sanitize_component) if the plain
# copy is rejected. Echoes the path actually used, so a caller that needs
# to know (process_zip_like's not-archive branch, for maybe_ocr) can.
safe_copy() {
    local src="$1" destdir="${2%/}" name sanitized dest
    if [[ "${src}" == "${destdir}"/* ]]; then
        printf '%s' "${src}"
        return
    fi
    name="$(basename -- "${src}")"
    dest="${destdir}/${name}"
    if cp "${src}" "${dest}" 2>/dev/null; then
        printf '%s' "${dest}"
        return
    fi
    sanitized="$(sanitize_component "${name}")"
    dest="${destdir}/${sanitized}"
    if [[ "${sanitized}" != "${name}" ]] && cp "${src}" "${dest}" 2>/dev/null; then
        log RENAMED "Non-UTF-8 filename sanitized for the host filesystem: ${name} -> ${sanitized}"
        printf '%s' "${dest}"
        return
    fi
    log CORRUPT "Could not copy to ${destdir}/ (even after sanitizing filename): ${src}"
    printf '%s' "${src}"
}

# Moves every regular file under internal staging directory $1 (always a
# fresh mktemp -d that only try_extract/process_pst write into - nothing
# else needs preserving under it once this returns) into bind-mounted
# destination directory $2, sanitizing any path component that isn't
# valid UTF-8 along the way (see sanitize_component/safe_copy's comments
# for why). Used instead of extracting straight into $2, which is what
# used to fail outright on a legacy-encoded filename.
place_sanitized() {
    local src="${1%/}" dest="${2%/}" srcfile relpath part joined destfile
    local -a parts
    mkdir -p "${dest}"
    while IFS= read -r -d '' srcfile; do
        relpath="${srcfile#"${src}"/}"
        joined=""
        IFS='/' read -ra parts <<< "${relpath}"
        for part in "${parts[@]}"; do
            joined="${joined}/$(sanitize_component "${part}")"
        done
        destfile="${dest}${joined}"
        if [[ "${destfile}" != "${dest}/${relpath}" ]]; then
            log RENAMED "Non-UTF-8 filename sanitized for the host filesystem: ${relpath} -> ${joined#/}"
        fi
        mkdir -p "$(dirname -- "${destfile}")"
        mv "${srcfile}" "${destfile}"
    done < <(find "${src}" -mindepth 1 -type f -print0)
}

# Moves/deletes an original after it has been successfully extracted, per
# deis.cfg's <kind>_archive/<kind>_remove (kind is "zip" or "pst"). Applied
# uniformly to top-level and nested archives alike - previously only nested
# archives were ever moved or removed, which was an inconsistency rather than
# a deliberate choice. Now that both feed the same flat /extracted/archive
# directory, two unrelated archives sharing a basename (e.g. two different
# leak folders each containing "invoice.zip") are a real possibility, so a
# collision is disambiguated the same way deis/done.sh already does for
# /files, rather than one silently overwriting the other.
dispose_of_original() {
    local path="$1" kind="$2" file base ext n dest sanitized
    config_true "${kind}_archive" || { config_true "${kind}_remove" && rm -f "${path}"; return; }

    mkdir -p /extracted/archive
    file="$(basename "${path}")"
    dest="/extracted/archive/${file}"
    if [[ -e "${dest}" ]]; then
        base="${file%.*}"
        ext="${file##*.}"
        [[ "${base}" == "${file}" ]] && ext=""
        n=2
        while [[ -e "/extracted/archive/${base}-dup${n}${ext:+.${ext}}" ]]; do
            n=$(( n + 1 ))
        done
        dest="/extracted/archive/${base}-dup${n}${ext:+.${ext}}"
    fi
    if ! mv "${path}" "${dest}" 2>/dev/null; then
        sanitized="$(sanitize_component "$(basename -- "${dest}")")"
        if [[ "${sanitized}" == "$(basename -- "${dest}")" ]] || ! mv "${path}" "$(dirname -- "${dest}")/${sanitized}" 2>/dev/null; then
            log CORRUPT "Could not move original into /extracted/archive (even after sanitizing filename): ${path}"
            return
        fi
        log RENAMED "Non-UTF-8 filename sanitized for the host filesystem: $(basename -- "${dest}") -> ${sanitized}"
    fi
}

# Appends every regular file under $1 to this worker's own output file, later
# merged by the parent into the next round - this is what turns extraction
# into recursion instead of two fixed passes. A worker is a separate process
# (see load_passwords above), so it cannot append to a shared bash array
# directly; $$ is unique per worker, so concurrent workers never collide here.
queue_new_files() {
    find "$1" -type f -print0 >> "${WORKDIR}/next.$$"
}

# Pre-extraction check against a hostile archive, using 7-Zip's own -slt
# listing rather than trusting anything about the archive that would only
# be known after extracting it. Two independent things this catches: an
# entry whose own path would write outside the destination directory
# (zip-slip - an absolute path, or one containing a ".." component), and an
# archive whose *declared* uncompressed size is implausible relative to its
# compressed size on disk (the classic decompression-bomb shape) or simply
# too large to extract at all. Sets SAFETY_REASON on failure, for the log.
# Applies only to zip-like archives - PST has no equivalent dry-run listing,
# so the per-extraction timeout below is its only guard.
check_archive_safety() {
    local path="$1" dest="$2"
    local max_bytes max_ratio total_size=0 compressed_size avail_kb line entry_path entry_size in_entries=0
    local -a entry_parts
    local entry_part

    max_bytes="$(config_int max_extract_bytes "${MAX_EXTRACT_BYTES_DEFAULT}")"
    max_ratio="$(config_int max_compression_ratio "${MAX_COMPRESSION_RATIO_DEFAULT}")"

    # -slt's first "Path = " (before the "----------" separator) is the
    # archive file itself, not an entry - skip everything up to and
    # including that separator, or the archive's own (legitimately
    # absolute) path would be misread as an entry trying to escape.
    while IFS= read -r line; do
        if [[ "${line}" == "----------" ]]; then
            in_entries=1
            continue
        fi
        ((in_entries)) || continue
        case "${line}" in
        "Path = "*)
            entry_path="${line#Path = }"
            case "${entry_path}" in
            /*)
                SAFETY_REASON="entry path escapes the destination directory: ${entry_path}"
                return 1
                ;;
            esac
            # Split on '/' and check each component for an exact '..' - a
            # real zip-slip entry, e.g. '../../etc/passwd' or
            # 'foo/../../bar'. A plain substring match on '*..*' also flags
            # ordinary filenames that merely contain two consecutive dots
            # (e.g. 'report..pdf'), rejecting legitimate archives outright.
            IFS='/' read -ra entry_parts <<< "${entry_path}"
            for entry_part in "${entry_parts[@]}"; do
                if [[ "${entry_part}" == ".." ]]; then
                    SAFETY_REASON="entry path escapes the destination directory: ${entry_path}"
                    return 1
                fi
            done
            ;;
        "Size = "*)
            entry_size="${line#Size = }"
            [[ "${entry_size}" =~ ^[0-9]+$ ]] && total_size=$((total_size + entry_size))
            ;;
        esac
    done < <(/7zz l -slt -- "${path}" 2> /dev/null)

    if ((total_size > max_bytes)); then
        SAFETY_REASON="would extract to ${total_size} bytes, over the ${max_bytes}-byte cap"
        return 1
    fi

    compressed_size="$(stat --format=%s "${path}" 2> /dev/null || echo 0)"
    if ((compressed_size > 0 && total_size / compressed_size > max_ratio)); then
        SAFETY_REASON="compression ratio $((total_size / compressed_size))x, over the ${max_ratio}x cap"
        return 1
    fi

    avail_kb="$(df -kP "$(dirname -- "${dest}")" 2> /dev/null | awk 'NR==2 {print $4}')"
    if [[ "${avail_kb}" =~ ^[0-9]+$ ]] && ((avail_kb * 1024 < total_size)); then
        SAFETY_REASON="not enough disk space (${avail_kb}KB available, ${total_size} bytes needed)"
        return 1
    fi

    return 0
}

# True for anything readpst handles: .pst and .ost share the same underlying
# libpst format (OST is Outlook's offline cache of the same data), so both
# are dispatched through process_pst() and gated by the same pst/pst_archive/
# pst_remove deis.cfg keys - readpst reads the file by its own header, not by
# extension, so no separate tool or config surface is needed for OST.
is_pst_like() {
    [[ "${1,,}" == *.pst || "${1,,}" == *.ost ]]
}

# Legacy (pre-2007) Microsoft Office formats are OLE/CFBF files - the same
# container format .msg uses, and 7-Zip's own format list explicitly names
# doc/xls/ppt as extensions for its "Compound" archive type (confirmed via
# `7zz i` against this image), so these are if anything more certain to be
# shredded than .msg was. Confirmed against a real corpus: a .xls extracted
# to just [5]SummaryInformation/[5]DocumentSummaryInformation - metadata
# streams only, the actual Workbook/Book stream with the real spreadsheet
# data never made it out at all.
#
# The extension list alone isn't enough: also confirmed against a real
# corpus, a legacy accounting system's *.kli export was, byte-for-byte, an
# OLE2 Word document - extension-blind 7-Zip shredded it the same way,
# additionally hitting a genuine "Data Error" on one of its embedded
# native OLE objects along the way (7-Zip's OLE reader has real limits of
# its own once you're this deep inside a compound file). The CFBF magic
# number is one fixed 8 bytes regardless of what the file is named, so
# check that directly rather than trusting an extension leak dumps are
# already known to get wrong.
is_ole_document() {
    case "${1,,}" in
    *.msg | *.doc | *.dot | *.xls | *.xlt | *.xla | *.ppt | *.pot | *.pps | *.pub)
        return 0
        ;;
    esac
    [[ "$(head -c 8 -- "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')" == "d0cf11e0a1b11ae1" ]]
}

# OOXML (Office 2007+) and OpenDocument files are themselves ZIP archives -
# a .xlsx is a package of internal XML parts (workbook.xml, sharedStrings.xml,
# individual sheet XML with cells referencing sharedStrings by index, ...),
# so 7-Zip's generic archive detection (signature-based, not extension-based
# - see is_ole_document() above) happily "extracts" one into those loose
# parts instead of leaving it whole. Found via a real corpus: one .xlsx
# became 1029 separate documents of raw internal XML, each individually
# meaningless, instead of one document with Tika's actual parsed cell
# content. Same fix as is_ole_document(): skip 7-Zip for these and let
# Tika's real OOXML/ODF parsers handle the whole file at ingest time.
is_zip_based_document() {
    case "${1,,}" in
    *.docx | *.docm | *.dotx | *.dotm | \
        *.xlsx | *.xlsm | *.xltx | *.xltm | *.xlsb | \
        *.pptx | *.pptm | *.potx | *.potm | *.ppsx | *.ppsm | *.sldx | *.sldm | \
        *.odt | *.ods | *.odp | *.odg | *.odf | *.odb | *.odc | *.odi | *.odm | \
        *.ott | *.ots | *.otp | *.otg)
        return 0
        ;;
    *)
        return 1
        ;;
    esac
}

# A real, otherwise perfectly valid PDF can still trip 7-Zip's generic
# archive detection: some PDF authoring tools append proprietary sidecar
# data (an editor's own cache/metadata, unrelated to the document content)
# as a small ZIP fragment after the PDF's own %%EOF. 7-Zip's signature
# scan finds that trailing fragment, tries to open it as an archive, and
# fails - "Unconfirmed start of archive" / "Data Error" on whatever that
# fragment's one entry was - even though every byte of the actual PDF
# content is intact and Tika parses it fine. Confirmed against a real
# corpus: `7zz l -slt` on the file showed `Type = zip` starting near the
# very end of the file, with `Tail Size` matching the leftover fragment.
# Same fix as is_ole_document()/is_zip_based_document(): skip 7-Zip
# entirely for PDFs and let Tika handle the whole file at ingest time.
is_pdf_document() {
    [[ "${1,,}" == *.pdf ]]
}

# Prints the shared prefix of a multi-volume RAR set's filename (its
# "family") - "foo.part03.rar" -> "foo" - so different volumes of the
# same set can be recognized as belonging together regardless of which
# part number each one is. Fails (prints nothing, returns 1) for anything
# that doesn't match that naming convention, which 7-Zip/WinRAR always
# uses for split RAR archives.
multivolume_family() {
    local lower="${1,,}"
    [[ "${lower}" =~ ^(.*)\.part[0-9]+\.rar$ ]] || return 1
    printf '%s' "${BASH_REMATCH[1]}"
}

# Attempts every password candidate against $1, extracting into $2 on
# success. Never omits -p: 7-Zip prompts interactively for a password on an
# encrypted archive if none is given at all, which would hang the pipeline
# the first time deis.cfg's ZIP_PASSWORD is left empty against a genuinely
# encrypted file. Sets EXTRACT_RESULT to one of: extracted, encrypted,
# multivolume, not-archive, corrupt - these are 7-Zip's own distinction
# (its exit code alone does not tell them apart; the message text does),
# rather than a single generic "not an archive" for all three as before.
# Wrapped in timeout so one hung archive cannot stall this worker (and,
# once every worker slot is hung the same way, the whole serial
# round-based pipeline) indefinitely. Extracts into an internal staging
# directory first, then moves the result into $2 (see place_sanitized)
# rather than extracting straight there: $2 is the bind-mounted /extracted
# volume, which - on a macOS host - rejects a write outright for a
# legacy-encoded filename 7-Zip decoded into something that isn't valid
# UTF-8, something a native Linux filesystem (this staging dir) never
# does.
try_extract() {
    local path="$1" dest="$2" candidate output="" extract_timeout stage is_multivolume_name
    extract_timeout="$(config_int extract_timeout "${EXTRACT_TIMEOUT_DEFAULT}")"
    stage="$(mktemp -d)"
    # Confirmed against a real corpus: a multi-volume RAR's non-first part,
    # tried on its own, does NOT always fail the same way - with none of
    # its sibling volumes present it names the specific one it needs
    # ("Missing volume : X"), but with most-of-the-set present and only
    # the true first volume absent, 7-Zip instead gives up with a generic
    # "Headers Error" (no mention of "volume" at all). "Headers Error"
    # alone is too generic a signal on its own - a genuinely corrupt,
    # unrelated file can trigger it too - so it only counts as a
    # multi-volume signal here alongside the file's own *.partNN.rar
    # name, the one thing that's unambiguous either way.
    is_multivolume_name=0
    multivolume_family "$(basename -- "${path}")" > /dev/null 2>&1 && is_multivolume_name=1
    for candidate in "${PASSWORDS[@]}"; do
        if output="$(timeout "${extract_timeout}" /7zz x -y -p"${candidate}" -o"${stage}" -- "${path}" < /dev/null 2>&1)"; then
            place_sanitized "${stage}" "${dest}"
            rm -rf "${stage}"
            EXTRACT_RESULT="extracted"
            return 0
        fi
        # 7-Zip also reports "Wrong password" on every entry in the
        # missing-volume case (it can't decode anything without the
        # header that lives in the first volume), but that's a red
        # herring: no password will ever fix a missing volume, so stop
        # immediately rather than retrying every candidate uselessly
        # against a (typically large) file.
        if grep -q "Missing volume" <<< "${output}" || { (( is_multivolume_name )) && grep -q "Headers Error" <<< "${output}"; }; then
            break
        fi
        # A non-password failure on the first attempt means trying more
        # passwords is pointless - stop rather than repeating the same
        # corrupt-archive error once per password candidate.
        grep -q "Wrong password" <<< "${output}" || break
        rm -rf "${stage:?}"/* 2>/dev/null
    done
    rm -rf "${stage}"
    if grep -qE "Cannot open the file as archive|Can't open as archive" <<< "${output}"; then
        EXTRACT_RESULT="not-archive"
    elif grep -q "Missing volume" <<< "${output}" || { (( is_multivolume_name )) && grep -q "Headers Error" <<< "${output}"; }; then
        # The content isn't lost - it's already included whenever the
        # set's first volume succeeds (see resolve_multivolume_stragglers,
        # which reconciles this once extraction as a whole is done). Kept
        # out of "encrypted" so it doesn't imply a password is the fix.
        EXTRACT_RESULT="multivolume"
    elif grep -q "Wrong password" <<< "${output}"; then
        EXTRACT_RESULT="encrypted"
    else
        EXTRACT_RESULT="corrupt"
    fi
    EXTRACT_ERR="${output}"
    return 1
}

# OCR for image-only content (item 21/31's origin story: a scanned
# passport or invoice indexes with content_length: 0 and is invisible to
# every content search). Runs against a file already at its final resting
# place under /extracted/files, not the pre-copy source path. Writes a
# "<name>.ocr.txt" sidecar - picked up by ingest.py's normal directory walk
# as its own independently indexed document - rather than merging OCR text
# into the same Elasticsearch document as the original image, which would
# mean touching ingest.py's already crash-safety-tuned bulk/marker flow for
# a text source (Tesseract, forked as a subprocess) that has nothing to do
# with the sha256-confirmed-before-marked guarantee that flow provides.
# Scoped to image files only for now, not scanned PDFs (which would need
# PDF rasterization tooling too - a real gap, left as a known limitation
# rather than attempted here).
maybe_ocr() {
    local final_path="$1" mime ocr_languages ocr_timeout text
    # Defaults to on when "ocr" is absent entirely, so an existing deis.cfg
    # written before item 21 gets the behaviour README documents as the
    # default rather than silently skipping OCR forever.
    config_true_default ocr true || return 0
    mime="$(file --mime-type -b -- "${final_path}" 2>/dev/null)"
    case "${mime}" in
    image/*) ;;
    *) return 0 ;;
    esac
    ocr_languages="$(read_cfg ocr_languages)"
    [[ -z "${ocr_languages}" ]] && ocr_languages="eng"
    ocr_timeout="$(config_int extract_timeout "${EXTRACT_TIMEOUT_DEFAULT}")"
    text="$(timeout "${ocr_timeout}" tesseract -l "${ocr_languages}" "${final_path}" stdout 2>/dev/null)"
    if [[ -n "${text//[[:space:]]/}" ]]; then
        printf '%s\n' "${text}" > "${final_path}.ocr.txt"
        log OCR "Extracted OCR text: ${final_path}.ocr.txt"
    fi
}

# MS Access (.mdb/.wdb) databases index with content_length: 0 - Tika has no
# Access parser, so the actual data (found via a real corpus: per-client
# accounting exports) is otherwise invisible to every content search.
# mdbtools reads the file directly (Jet DB isn't archive-shaped, so this
# never touches 7-Zip), one CSV sidecar per table - picked up by ingest.py's
# normal directory walk as its own independently indexed document, same
# reasoning as maybe_ocr's .ocr.txt sidecar above. .accdb (2007+/ACE) is
# only partially supported by mdbtools; dBase (.dbf) isn't attempted at all
# (a different format entirely, no tool for it here yet - see
# docs/IMPROVEMENTS.md).
maybe_export_access_tables() {
    local final_path="$1" mime table_timeout table sanitized_table csv_out exported=0
    local -a tables
    config_true_default access_export true || return 0
    mime="$(file --mime-type -b -- "${final_path}" 2>/dev/null)"
    [[ "${mime}" == "application/x-msaccess" ]] || return 0
    table_timeout="$(config_int extract_timeout "${EXTRACT_TIMEOUT_DEFAULT}")"
    if ! mapfile -t tables < <(timeout "${table_timeout}" mdb-tables -1 -- "${final_path}" 2>/dev/null); then
        log CORRUPT "Could not list MS Access tables (corrupt or unsupported): ${final_path}"
        return
    fi
    for table in "${tables[@]}"; do
        [[ -n "${table}" ]] || continue
        sanitized_table="$(sanitize_component "${table}")"
        csv_out="${final_path}.${sanitized_table}.csv"
        if timeout "${table_timeout}" mdb-export -- "${final_path}" "${table}" > "${csv_out}" 2>/dev/null && [[ -s "${csv_out}" ]]; then
            exported=$(( exported + 1 ))
        else
            rm -f "${csv_out}"
        fi
    done
    (( exported > 0 )) && log TABLES "Exported ${exported} table(s) to CSV: ${final_path}.*.csv"
}

# Tries every password from PASSWORDS against a document already confirmed
# individually password-protected (application/encrypted - a specific,
# reliable mime signal, confirmed against a real corpus where it never
# false-positived on an ordinary document). Unlike try_extract's archive
# passwords, this never touches dispose_of_original: the original wasn't an
# archive, so the existing not-archive handling (the file already left in
# place under /files) applies unchanged - only the live copy under
# /extracted/files is replaced in place with the recovered plaintext. The
# password itself is never logged, matching try_extract's existing
# precedent for archives.
decrypt_office_document() {
    local final_path="$1" candidate stage sha
    config_true_default document_decrypt_office true || return 0
    stage="$(mktemp)"
    for candidate in "${PASSWORDS[@]}"; do
        if timeout "$(config_int extract_timeout "${EXTRACT_TIMEOUT_DEFAULT}")" \
            /opt/msoffcrypto-venv/bin/msoffcrypto-tool "${final_path}" "${stage}" -p "${candidate}" \
            < /dev/null > /dev/null 2>&1 && [[ -s "${stage}" ]]; then
            mv "${stage}" "${final_path}"
            sha="$(sha256sum "${final_path}" | awk '{print $1}')"
            echo "${sha}" >> "${DECRYPTED}"
            log DECRYPTED "Recovered password-protected Office document: ${final_path}"
            return
        fi
    done
    rm -f "${stage}"
    sha="$(sha256sum "${final_path}" | awk '{print $1}')"
    echo "${sha}" >> "${STILL_ENCRYPTED}"
    log ENCRYPTED "Still password-protected after trying ${#PASSWORDS[@]} password(s) (Office), left as-is: ${final_path}"
}

# Same idea as decrypt_office_document, but PDF encryption can't be told
# apart from an ordinary PDF by mime type alone (both report
# application/pdf), so this checks qpdf's own --is-encrypted signal first -
# exit 0 means encrypted, non-zero (2 = not encrypted, or a timeout) means
# not - before ever trying a password. Skipping that check would mean
# logging every ordinary, never-encrypted PDF as DECRYPTED once qpdf's
# --decrypt no-ops successfully on it (a documented, intentional qpdf
# behavior for unencrypted input), which would be actively wrong, not just
# wasted work.
decrypt_pdf_document() {
    local final_path="$1" doc_timeout candidate stage sha
    config_true_default document_decrypt_pdf true || return 0
    doc_timeout="$(config_int extract_timeout "${EXTRACT_TIMEOUT_DEFAULT}")"
    timeout "${doc_timeout}" qpdf --is-encrypted -- "${final_path}" > /dev/null 2>&1 || return 0
    stage="$(mktemp)"
    for candidate in "${PASSWORDS[@]}"; do
        if timeout "${doc_timeout}" qpdf --decrypt --password="${candidate}" -- "${final_path}" "${stage}" \
            < /dev/null > /dev/null 2>&1 && [[ -s "${stage}" ]]; then
            mv "${stage}" "${final_path}"
            sha="$(sha256sum "${final_path}" | awk '{print $1}')"
            echo "${sha}" >> "${DECRYPTED}"
            log DECRYPTED "Recovered password-protected PDF: ${final_path}"
            return
        fi
    done
    rm -f "${stage}"
    sha="$(sha256sum "${final_path}" | awk '{print $1}')"
    echo "${sha}" >> "${STILL_ENCRYPTED}"
    log ENCRYPTED "Still password-protected after trying ${#PASSWORDS[@]} password(s) (PDF), left as-is: ${final_path}"
}

# Routes a not-archive file to the right decryption attempt, if any, based
# on its mime type - see decrypt_office_document/decrypt_pdf_document above
# for why each is gated differently.
maybe_decrypt_document() {
    local final_path="$1" mime
    mime="$(file --mime-type -b -- "${final_path}" 2>/dev/null)"
    case "${mime}" in
    application/encrypted) decrypt_office_document "${final_path}" ;;
    application/pdf) decrypt_pdf_document "${final_path}" ;;
    esac
}

# Runs the real extraction for one sha256 group's representative file
# ("primary" - see dispatch_round below). $3, if given, is the group's
# result-file name; every other file sharing this group's content reuses
# whatever outcome is recorded here instead of repeating identical, wasted
# 7-Zip work - two files with the same sha256 are byte-identical, so 7-Zip
# extracting them is guaranteed to succeed or fail the same way both times.
process_zip_like() {
    local path="$1" sha="$2" resultfile="${3:-}" dest="/extracted/files/${2}"

    if [[ -e "${dest}" ]]; then
        # Content already extracted, either earlier in this same round by
        # this group's primary, or in an earlier round.
        [[ -n "${resultfile}" ]] && echo "extracted" > "${resultfile}"
        dispose_of_original "${path}" zip
        return
    fi
    mkdir -p "${dest}"

    # Document formats that 7-Zip's generic archive detection would
    # "successfully" tear apart instead of leaving whole for Tika's real
    # parsers at ingest time - see is_ole_document()/is_zip_based_document()
    # above for why, and why this is a real content-loss bug rather than
    # cosmetic. Skip 7-Zip entirely for both and fall straight into the same
    # not-archive/copy path used below for anything 7-Zip itself reports as
    # not an archive.
    if is_ole_document "${path}" || is_zip_based_document "${path}" || is_pdf_document "${path}"; then
        EXTRACT_RESULT="not-archive"
        EXTRACT_ERR=""
    elif ! check_archive_safety "${path}" "${dest}"; then
        EXTRACT_RESULT="unsafe"
        EXTRACT_ERR="${SAFETY_REASON}"
    elif try_extract "${path}" "${dest}"; then
        [[ -n "${resultfile}" ]] && echo "extracted" > "${resultfile}"
        log EXTRACTED "Extracted: ${path} -> ${dest}"
        queue_new_files "${dest}"
        dispose_of_original "${path}" zip
        return
    fi

    rm -rf "${dest}" 2>/dev/null
    [[ -n "${resultfile}" ]] && echo "${EXTRACT_RESULT}" > "${resultfile}"
    local final_path
    final_path="$(safe_copy "${path}" /extracted/files/)"
    case "${EXTRACT_RESULT}" in
        not-archive)
            log COPIED "Not an archive, left/copied as-is: ${path}"
            maybe_decrypt_document "${final_path}"
            maybe_ocr "${final_path}"
            maybe_export_access_tables "${final_path}"
            ;;
        encrypted)
            echo "${sha}" >> "${STILL_ENCRYPTED}"
            log ENCRYPTED "Still encrypted after trying ${#PASSWORDS[@]} password(s), left/copied as-is: ${path}"
            ;;
        multivolume)
            echo "${sha}" >> "${STILL_MULTIVOLUME}"
            log MULTIVOLUME "Non-first volume of a multi-volume archive, not independently extractable - resolved once its first volume's own outcome is known, left/copied as-is for now: ${path}"
            ;;
        corrupt)
            echo "${sha}" >> "${STILL_CORRUPT}"
            log CORRUPT "Could not extract (corrupt or unsupported), left/copied as-is: ${path} - $(grep -m1 -iE 'unexpected|error' <<< "${EXTRACT_ERR}")"
            ;;
        unsafe)
            echo "${sha}" >> "${STILL_UNSAFE}"
            log UNSAFE "Rejected before extraction, left/copied as-is: ${path} - ${EXTRACT_ERR}"
            ;;
    esac
}

process_pst() {
    local path="$1" sha="$2" resultfile="${3:-}" dest="/extracted/files/${2}" extract_timeout stage

    if [[ -e "${dest}" ]]; then
        [[ -n "${resultfile}" ]] && echo "extracted" > "${resultfile}"
        dispose_of_original "${path}" pst
        return
    fi
    extract_timeout="$(config_int extract_timeout "${EXTRACT_TIMEOUT_DEFAULT}")"
    # Staged internally first, then moved into $dest - see try_extract's
    # comment on place_sanitized for why extracting straight into the
    # bind-mounted $dest isn't safe.
    stage="$(mktemp -d)"

    if timeout "${extract_timeout}" readpst -D -S -j 2 -q -r -o "${stage}" "${path}" < /dev/null 2>>"${LOG}"; then
        place_sanitized "${stage}" "${dest}"
        rm -rf "${stage}"
        [[ -n "${resultfile}" ]] && echo "extracted" > "${resultfile}"
        log EXTRACTED "Extracted PST/OST: ${path} -> ${dest}"
        queue_new_files "${dest}"
        dispose_of_original "${path}" pst
    else
        rm -rf "${stage}" "${dest}" 2>/dev/null
        [[ -n "${resultfile}" ]] && echo "corrupt" > "${resultfile}"
        safe_copy "${path}" /extracted/files/ > /dev/null
        echo "${sha}" >> "${STILL_CORRUPT}"
        log CORRUPT "Could not extract PST/OST (corrupt or unsupported), left/copied as-is: ${path}"
    fi
}

# Applies an already-known outcome (from this group's primary) to a
# duplicate file, without repeating the extraction attempt. Mirrors the
# per-outcome handling in process_zip_like/process_pst exactly, so a
# duplicate is indistinguishable in the log/still_encrypted.txt/final output
# from what an independent attempt would have produced.
apply_known_result() {
    local path="$1" sha="$2" kind="$3" result="$4"
    case "${result}" in
        extracted)
            dispose_of_original "${path}" "${kind}"
            ;;
        not-archive)
            # No maybe_ocr/maybe_export_access_tables here, unlike
            # process_zip_like's not-archive arm: this file is
            # byte-identical to the primary that was already OCR'd/
            # table-exported this round, so re-running would produce the
            # same output again, and ingest.py would then dedupe the
            # resulting sidecar(s) away by sha256 anyway - just a second
            # Tesseract/mdbtools pass and an orphan sidecar per duplicate.
            # maybe_decrypt_document *is* still called, unlike those two:
            # this duplicate's own copy is its own (still-ciphertext) file
            # via safe_copy below, not the primary's already-decrypted one,
            # so skipping it would leave this specific duplicate
            # permanently opaque despite a byte-identical sibling having
            # been recovered - a real correctness gap, not a wasted-work
            # nice-to-have like OCR/table-export's case.
            local dup_final_path
            dup_final_path="$(safe_copy "${path}" /extracted/files/)"
            maybe_decrypt_document "${dup_final_path}"
            log COPIED "Not an archive (same content already checked this round), left/copied as-is: ${path}"
            ;;
        encrypted)
            safe_copy "${path}" /extracted/files/ > /dev/null
            echo "${sha}" >> "${STILL_ENCRYPTED}"
            log ENCRYPTED "Still encrypted (same content already checked this round), left/copied as-is: ${path}"
            ;;
        multivolume)
            safe_copy "${path}" /extracted/files/ > /dev/null
            echo "${sha}" >> "${STILL_MULTIVOLUME}"
            log MULTIVOLUME "Non-first volume of a multi-volume archive (same content already checked this round), left/copied as-is for now: ${path}"
            ;;
        corrupt)
            safe_copy "${path}" /extracted/files/ > /dev/null
            echo "${sha}" >> "${STILL_CORRUPT}"
            log CORRUPT "Could not extract (same content already checked this round), left/copied as-is: ${path}"
            ;;
        unsafe)
            safe_copy "${path}" /extracted/files/ > /dev/null
            echo "${sha}" >> "${STILL_UNSAFE}"
            log UNSAFE "Rejected before extraction (same content already checked this round), left/copied as-is: ${path}"
            ;;
        *)
            # Should not happen (the primary always writes a result before
            # exiting) - fail safe by attempting this file independently
            # rather than silently dropping it.
            process_one_file "${path}"
            ;;
    esac
}

# Entry point for a round's parallel worker process (see dispatch_round).
# Everything this needs - functions and WORKDIR/LOG/STILL_ENCRYPTED/STILL_CORRUPT/
# STILL_UNSAFE - is exported into the environment before xargs -P spawns these. Takes only the
# path and re-derives sha/kind/resultfile itself (dispatch_round's grouping
# loop computes the same values the same way) rather than having the caller
# pass three values through one xargs -I{} placeholder, which would need an
# awkward custom delimiter; sha256sum is cheap enough that hashing twice is
# not worth that.
worker_entrypoint() {
    local path="$1" sha kind resultfile
    load_passwords
    sha="$(sha256sum "${path}" | awk '{print $1}')"
    if is_pst_like "${path}"; then kind="pst"; else kind="zip"; fi
    resultfile="${WORKDIR}/results/${sha}-${kind}"

    if [[ "${kind}" == "pst" ]]; then
        if config_true pst; then
            process_pst "${path}" "${sha}" "${resultfile}"
        else
            echo "extracted" > "${resultfile}"  # "extracted" here just means "handled, nothing left to do"
            safe_copy "${path}" /extracted/files/ > /dev/null
            log COPIED "PST/OST extraction disabled (pst=false), left/copied as-is: ${path}"
        fi
    else
        process_zip_like "${path}" "${sha}" "${resultfile}"
    fi
}

# Serial fallback entry point (no parallel worker/result-file machinery),
# used for the WORKDIR-less "should never happen" case in
# apply_known_result and for anything that isn't part of a round dispatch.
process_one_file() {
    local path="$1"
    [[ -f "${path}" ]] || return   # may already have been consumed elsewhere
    if is_pst_like "${path}"; then
        if config_true pst; then
            process_pst "${path}" "$(sha256sum "${path}" | awk '{print $1}')"
        else
            safe_copy "${path}" /extracted/files/ > /dev/null
            log COPIED "PST/OST extraction disabled (pst=false), left/copied as-is: ${path}"
        fi
    else
        process_zip_like "${path}" "$(sha256sum "${path}" | awk '{print $1}')"
    fi
}

export -f log read_cfg config_true config_true_default config_int load_passwords \
    sanitize_component safe_copy place_sanitized dispose_of_original multivolume_family \
    queue_new_files check_archive_safety maybe_ocr maybe_export_access_tables \
    maybe_decrypt_document decrypt_office_document decrypt_pdf_document try_extract \
    is_pst_like is_ole_document is_zip_based_document is_pdf_document process_zip_like \
    process_pst apply_known_result worker_entrypoint process_one_file

# Extracts one round of files in parallel (up to $PARALLELISM at a time).
# Files are deduplicated by content (sha256) plus type (.pst vs not, since
# that decides which tool runs and which deis.cfg keys apply) before
# dispatch: only one representative per group - the "primary" - actually
# runs 7-Zip/readpst, since two files with the same sha256 are byte-identical
# and extracting both would be guaranteed-redundant, wasted work at best and,
# run truly concurrently, a real race (two processes writing into the same
# destination directory at once). Every other file in a group - a
# "duplicate" - is handled after every primary in the round has finished, by
# replaying the primary's already-known outcome (apply_known_result) rather
# than attempting extraction itself.
dispatch_round() {
    local -a files=("$@")
    local path sha kind group
    declare -A seen_group   # group -> 1, just to detect the first occurrence
    local -a primaries=()
    local -a dup_paths=() dup_shas=() dup_kinds=()

    mkdir -p "${WORKDIR}/results"
    rm -f "${WORKDIR}"/next.* 2>/dev/null

    for path in "${files[@]}"; do
        sha="$(sha256sum "${path}" | awk '{print $1}')"
        if is_pst_like "${path}"; then kind="pst"; else kind="zip"; fi
        group="${sha}-${kind}"
        if [[ -n "${seen_group[${group}]:-}" ]]; then
            dup_paths+=("${path}")
            dup_shas+=("${sha}")
            dup_kinds+=("${kind}")
        else
            seen_group["${group}"]=1
            primaries+=("${path}")
        fi
    done

    echo "Round ${ROUND}: ${#files[@]} file(s) to check (${#primaries[@]} unique content, $(( ${#files[@]} - ${#primaries[@]} )) duplicate), up to ${PARALLELISM} in parallel."

    if (( ${#primaries[@]} > 0 )); then
        # shellcheck disable=SC2016 # deliberately deferred: "$1" must expand
        # in the worker bash -c spawns, not in this parent shell.
        printf '%s\0' "${primaries[@]}" \
            | xargs -0 -r -P "${PARALLELISM}" -I{} bash -c 'worker_entrypoint "$1"' _ {}
    fi

    local i
    for (( i = 0; i < ${#dup_paths[@]}; i++ )); do
        group="${dup_shas[$i]}-${dup_kinds[$i]}"
        apply_known_result "${dup_paths[$i]}" "${dup_shas[$i]}" "${dup_kinds[$i]}" \
            "$(cat "${WORKDIR}/results/${group}" 2>/dev/null)"
    done

    NEXT_ROUND=()
    local f
    while IFS= read -r -d '' f; do
        NEXT_ROUND+=("${f}")
    done < <(cat "${WORKDIR}"/next.* 2>/dev/null)
}

# Reconciles every file still sitting in /files with EXTRACT_RESULT
# "multivolume" (see try_extract) once extraction as a whole is done -
# not sooner, since dispatch_round runs volumes in parallel and in no
# guaranteed order, so a later volume's outcome can't be judged mid-round.
# For each one, checks whether any sibling volume of the same family
# already landed in /extracted/archive (proof the set was successfully
# extracted via its first volume) - if so, this volume's content is
# already fully accounted for, so it's disposed of the same way a
# directly-successful extraction's original would be, instead of sitting
# in /files looking like unfinished work. Only scoped to top-level /files
# entries (not a multi-volume set nested inside another archive) - not
# encountered in practice, and re-deriving "was this whole nested set
# resolved" from inside an arbitrary parent tree is a lot of complexity
# for a case with no evidence it happens.
resolve_multivolume_stragglers() {
    [[ -s "${STILL_MULTIVOLUME}" ]] || return
    local candidate family archive_entry archive_family resolved sha
    local -A stuck_shas
    while IFS= read -r sha; do
        [[ -n "${sha}" ]] && stuck_shas["${sha}"]=1
    done < "${STILL_MULTIVOLUME}"
    : > "${STILL_MULTIVOLUME}"

    for candidate in /files/*; do
        [[ -f "${candidate}" ]] || continue
        family="$(multivolume_family "$(basename -- "${candidate}")")" || continue
        resolved=0
        for archive_entry in /extracted/archive/*; do
            [[ -e "${archive_entry}" ]] || continue
            archive_family="$(multivolume_family "$(basename -- "${archive_entry}")")" || continue
            if [[ "${archive_family}" == "${family}" ]]; then
                resolved=1
                break
            fi
        done
        if (( resolved )); then
            sha="$(sha256sum "${candidate}" | awk '{print $1}')"
            unset 'stuck_shas[$sha]'
            # The safe_copy fallback left an unopened, redundant copy of
            # this volume in /extracted/files - clutter now that its
            # content is confirmed already indexed via the first volume.
            # Removed regardless of what dispose_of_original below does
            # with the original: that copy was only ever a placeholder
            # for the unresolved case, and this one just resolved.
            rm -f "/extracted/files/$(basename -- "${candidate}")"
            dispose_of_original "${candidate}" zip
            if [[ -e "${candidate}" ]]; then
                # zip_archive/zip_remove (deis.cfg) both false - the
                # operator's choice to leave archive originals exactly
                # where they are, so dispose_of_original was a no-op.
                log MULTIVOLUME "Companion volume(s) already fully extracted - content already accounted for, left in place per zip_archive/zip_remove config: ${candidate}"
            else
                log MULTIVOLUME "Companion volume(s) already fully extracted - moved/removed per zip_archive/zip_remove config instead of being left flagged: ${candidate}"
            fi
        fi
    done

    for sha in "${!stuck_shas[@]}"; do
        echo "${sha}" >> "${STILL_MULTIVOLUME}"
    done
}

# Extracts everything under /files, then recurses into whatever that
# extraction produced, and so on, until a round produces nothing new or
# max_depth rounds have run. Replaces two fixed passes (top-level, then one
# pass over the result) that could miss archives nested three or more levels
# deep. Detection is "try extraction and see" rather than a fixed extension
# list, so wrong or missing extensions - routine in leak dumps - no longer
# hide an archive from this at all.
unpack() {
    : > "${STILL_ENCRYPTED}"
    : > "${STILL_CORRUPT}"
    : > "${STILL_UNSAFE}"
    : > "${STILL_MULTIVOLUME}"
    : > "${DECRYPTED}"
    WORKDIR="$(mktemp -d)"
    export WORKDIR LOG STILL_ENCRYPTED STILL_CORRUPT STILL_UNSAFE STILL_MULTIVOLUME DECRYPTED
    build_password_list | awk '!seen[$0]++' > "${WORKDIR}/passwords.list"

    # -name '.*' excludes /files/.gitignore - status/progress markers no
    # longer live in /files, so a dotfile is the only non-data entry left to
    # skip here.
    local -a current
    mapfile -d '' -t current < <(find /files -maxdepth 1 -type f ! -name '.*' -print0 | sort -z)

    local max_depth
    max_depth="$(config_int max_depth "${MAX_DEPTH_DEFAULT}")"
    export ROUND=1

    while (( ${#current[@]} > 0 )); do
        if (( ROUND > max_depth )); then
            log DEPTH-LIMIT "Reached max_depth=${max_depth} with ${#current[@]} file(s) still to check; left as-is: ${current[*]}"
            break
        fi
        dispatch_round "${current[@]}"
        current=("${NEXT_ROUND[@]}")
        ROUND=$(( ROUND + 1 ))
    done

    resolve_multivolume_stragglers
    rm -rf "${WORKDIR}"
}

function summary {
    find /extracted/files -type f -exec basename {} \; | grep -E '^[^.]+\.' | sed 's/^.*\.//' | sort | uniq -c | sort -nr > /status/extensions.txt
    find /extracted/files -type f -exec file -b --mime-type {} \; | sort | uniq -c | sort -nr > /status/mime.txt
    find /extracted/files > /status/path.txt
}

function prepare {
    if config_true unpack; then
        echo "Start unpack"
        unpack
    else
        return
    fi
    if config_true summary; then
        echo "Start summary"
        summary
    fi
    echo "Unpack done."
}

[[ -d /extracted/files ]] || mkdir /extracted/files

while true; do
    if [[ -f /status/unpack && ! -e /status/extract_done ]]; then
        echo "Configuration:"
        cat /deis.cfg
        echo ""
        # A real liveness signal (like /status/running for download.sh) for
        # deis.py/web/progress.py to tell "actually extracting right now"
        # apart from "unpack marker is set but this round hasn't started
        # yet" - the latter is at most a 5s window (the sleep below), but
        # worth distinguishing from a run that can take hours.
        touch /status/extracting
        prepare
        touch /status/extract_done
        exit
    fi
    sleep 5
done
