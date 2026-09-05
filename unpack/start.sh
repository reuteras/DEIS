#!/bin/bash

set -u

LOG=/logs/unpack.log
STILL_ENCRYPTED=/extracted/still_encrypted.txt
STILL_CORRUPT=/extracted/still_corrupt.txt
STILL_UNSAFE=/extracted/still_unsafe.txt
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

# Marker files deis/*.sh and web/startup.sh drop into /files. They must never
# be treated as leak data - keep this in sync with what those scripts touch
# (grep -ohrE '/files/[a-z_]+' deis/*.sh web/startup.sh is the source of truth).
CONTROL_FILES='/(\.gitignore|added_urls|batch_gids|batch_started|dies_done|done|download_failed|downloaded|extract|pending_count|running|unpack)$'

log() {
    # $1 = level (EXTRACTED / COPIED / ENCRYPTED / CORRUPT / DEPTH-LIMIT)
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
    local path="$1" kind="$2" file base ext n dest
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
    mv "${path}" "${dest}" 2>/dev/null
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
            /* | *..*)
                SAFETY_REASON="entry path escapes the destination directory: ${entry_path}"
                return 1
                ;;
            esac
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

# Attempts every password candidate against $1, extracting into $2 on
# success. Never omits -p: 7-Zip prompts interactively for a password on an
# encrypted archive if none is given at all, which would hang the pipeline
# the first time deis.cfg's ZIP_PASSWORD is left empty against a genuinely
# encrypted file. Sets EXTRACT_RESULT to one of: extracted, encrypted,
# not-archive, corrupt - these are 7-Zip's own distinction (its exit code
# alone does not tell them apart; the message text does), rather than a
# single generic "not an archive" for all three as before. Wrapped in
# timeout so one hung archive cannot stall this worker (and, once every
# worker slot is hung the same way, the whole serial round-based pipeline)
# indefinitely.
try_extract() {
    local path="$1" dest="$2" candidate output="" extract_timeout
    extract_timeout="$(config_int extract_timeout "${EXTRACT_TIMEOUT_DEFAULT}")"
    for candidate in "${PASSWORDS[@]}"; do
        if output="$(timeout "${extract_timeout}" /7zz x -y -p"${candidate}" -o"${dest}" -- "${path}" < /dev/null 2>&1)"; then
            EXTRACT_RESULT="extracted"
            return 0
        fi
        # A non-password failure on the first attempt means trying more
        # passwords is pointless - stop rather than repeating the same
        # corrupt-archive error once per password candidate.
        grep -q "Wrong password" <<< "${output}" || break
    done
    if grep -qE "Cannot open the file as archive|Can't open as archive" <<< "${output}"; then
        EXTRACT_RESULT="not-archive"
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
    config_true ocr || return 0
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

    if ! check_archive_safety "${path}" "${dest}"; then
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
    # A file discovered inside a parent's extraction directory is already at
    # a real, correctly namespaced path under /extracted/files - copying it
    # again into /extracted/files itself would just duplicate it there under
    # its bare filename, reintroducing exactly the cross-archive filename
    # collisions namespacing by the archive's own sha256 exists to avoid.
    # Only a genuinely top-level /files/* entry needs to be placed for the
    # first time.
    local final_path="${path}"
    if [[ "${path}" != /extracted/files/* ]]; then
        cp "${path}" /extracted/files/
        final_path="/extracted/files/$(basename -- "${path}")"
    fi
    case "${EXTRACT_RESULT}" in
        not-archive)
            log COPIED "Not an archive, left/copied as-is: ${path}"
            maybe_ocr "${final_path}"
            ;;
        encrypted)
            echo "${sha}" >> "${STILL_ENCRYPTED}"
            log ENCRYPTED "Still encrypted after trying ${#PASSWORDS[@]} password(s), left/copied as-is: ${path}"
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
    local path="$1" sha="$2" resultfile="${3:-}" dest="/extracted/files/${2}" extract_timeout

    if [[ -e "${dest}" ]]; then
        [[ -n "${resultfile}" ]] && echo "extracted" > "${resultfile}"
        dispose_of_original "${path}" pst
        return
    fi
    mkdir -p "${dest}"
    extract_timeout="$(config_int extract_timeout "${EXTRACT_TIMEOUT_DEFAULT}")"

    if timeout "${extract_timeout}" readpst -D -S -j 2 -q -r -o "${dest}" "${path}" < /dev/null 2>>"${LOG}"; then
        [[ -n "${resultfile}" ]] && echo "extracted" > "${resultfile}"
        log EXTRACTED "Extracted PST: ${path} -> ${dest}"
        queue_new_files "${dest}"
        dispose_of_original "${path}" pst
    else
        rm -rf "${dest}" 2>/dev/null
        [[ -n "${resultfile}" ]] && echo "corrupt" > "${resultfile}"
        [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
        echo "${sha}" >> "${STILL_CORRUPT}"
        log CORRUPT "Could not extract PST (corrupt or unsupported), left/copied as-is: ${path}"
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
            local dup_final_path="${path}"
            if [[ "${path}" != /extracted/files/* ]]; then
                cp "${path}" /extracted/files/
                dup_final_path="/extracted/files/$(basename -- "${path}")"
            fi
            log COPIED "Not an archive (same content already checked this round), left/copied as-is: ${path}"
            maybe_ocr "${dup_final_path}"
            ;;
        encrypted)
            [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
            echo "${sha}" >> "${STILL_ENCRYPTED}"
            log ENCRYPTED "Still encrypted (same content already checked this round), left/copied as-is: ${path}"
            ;;
        corrupt)
            [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
            echo "${sha}" >> "${STILL_CORRUPT}"
            log CORRUPT "Could not extract (same content already checked this round), left/copied as-is: ${path}"
            ;;
        unsafe)
            [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
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
    if [[ "${path,,}" == *.pst ]]; then kind="pst"; else kind="zip"; fi
    resultfile="${WORKDIR}/results/${sha}-${kind}"

    if [[ "${kind}" == "pst" ]]; then
        if config_true pst; then
            process_pst "${path}" "${sha}" "${resultfile}"
        else
            echo "extracted" > "${resultfile}"  # "extracted" here just means "handled, nothing left to do"
            [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
            log COPIED "PST extraction disabled (pst=false), left/copied as-is: ${path}"
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
    if [[ "${path,,}" == *.pst ]]; then
        if config_true pst; then
            process_pst "${path}" "$(sha256sum "${path}" | awk '{print $1}')"
        else
            [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
            log COPIED "PST extraction disabled (pst=false), left/copied as-is: ${path}"
        fi
    else
        process_zip_like "${path}" "$(sha256sum "${path}" | awk '{print $1}')"
    fi
}

export -f log read_cfg config_true config_int load_passwords dispose_of_original \
    queue_new_files check_archive_safety maybe_ocr try_extract process_zip_like process_pst \
    apply_known_result worker_entrypoint process_one_file

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
        if [[ "${path,,}" == *.pst ]]; then kind="pst"; else kind="zip"; fi
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
    WORKDIR="$(mktemp -d)"
    export WORKDIR LOG STILL_ENCRYPTED STILL_CORRUPT STILL_UNSAFE
    build_password_list | awk '!seen[$0]++' > "${WORKDIR}/passwords.list"

    local -a current
    mapfile -d '' -t current < <(find /files -maxdepth 1 -type f -print0 \
        | grep -zvE "${CONTROL_FILES}" \
        | sort -z)

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

    rm -rf "${WORKDIR}"
}

function summary {
    find /extracted/files -type f -exec basename {} \; | grep -E '^[^.]+\.' | sed 's/^.*\.//' | sort | uniq -c | sort -nr > /extracted/extensions.txt
    find /extracted/files -type f -exec file -b --mime-type {} \; | sort | uniq -c | sort -nr > /extracted/mime.txt
    find /extracted/files > /extracted/files/path.txt
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
    if [[ -f /files/unpack && ! -e /extracted/files/done ]]; then
        echo "Configuration:"
        cat /deis.cfg
        echo ""
        prepare
        touch /extracted/files/done
        exit
    fi
    sleep 5
done
