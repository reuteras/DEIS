#!/bin/bash

set -u

LOG=/logs/unpack.log
STILL_ENCRYPTED=/extracted/still_encrypted.txt
MAX_DEPTH_DEFAULT=6

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

# Reads a /deis.cfg key. Same substring match the rest of this project's
# config reading already uses (not section-aware - a key that merely contains
# this name as a substring would also match).
config_true() {
    grep -q "^$1=true" /deis.cfg
}

config_int() {
    local value
    value="$(grep -oE "^$1=[0-9]+" /deis.cfg | head -1 | cut -d= -f2)"
    [[ -n "${value}" ]] && echo "${value}" || echo "$2"
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

# Appends every regular file under $1 to the global NEXT_ROUND array, so it
# gets attempted in the next round - this is what turns extraction into
# recursion instead of two fixed passes.
queue_new_files() {
    local f
    while IFS= read -r -d '' f; do
        NEXT_ROUND+=("${f}")
    done < <(find "$1" -type f -print0)
}

# Attempts every password candidate against $1, extracting into $2 on
# success. Never omits -p: 7-Zip prompts interactively for a password on an
# encrypted archive if none is given at all, which would hang the pipeline
# the first time deis.cfg's ZIP_PASSWORD is left empty against a genuinely
# encrypted file. Sets EXTRACT_RESULT to one of: extracted, encrypted,
# not-archive, corrupt - these are 7-Zip's own distinction (its exit code
# alone does not tell them apart; the message text does), rather than a
# single generic "not an archive" for all three as before.
try_extract() {
    local path="$1" dest="$2" candidate output=""
    for candidate in "${PASSWORDS[@]}"; do
        if output="$(/7zz x -y -p"${candidate}" -o"${dest}" -- "${path}" < /dev/null 2>&1)"; then
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

process_zip_like() {
    local path="$1" sha dest
    sha="$(sha256sum "${path}" | awk '{print $1}')"
    dest="/extracted/files/${sha}"

    if [[ -e "${dest}" ]]; then
        # Identical content already extracted from elsewhere (a duplicate
        # archive) - nothing new to extract, but this copy still gets its own
        # archive/remove disposition.
        dispose_of_original "${path}" zip
        return
    fi
    mkdir -p "${dest}"

    if try_extract "${path}" "${dest}"; then
        log EXTRACTED "Extracted: ${path} -> ${dest}"
        queue_new_files "${dest}"
        dispose_of_original "${path}" zip
        return
    fi

    rmdir "${dest}" 2>/dev/null
    # A file discovered inside a parent's extraction directory is already at
    # a real, correctly namespaced path under /extracted/files - copying it
    # again into /extracted/files itself would just duplicate it there under
    # its bare filename, reintroducing exactly the cross-archive filename
    # collisions namespacing by the archive's own sha256 exists to avoid.
    # Only a genuinely top-level /files/* entry needs to be placed for the
    # first time.
    [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
    case "${EXTRACT_RESULT}" in
        not-archive)
            log COPIED "Not an archive, left/copied as-is: ${path}"
            ;;
        encrypted)
            echo "${path}" >> "${STILL_ENCRYPTED}"
            log ENCRYPTED "Still encrypted after trying ${#PASSWORDS[@]} password(s), left/copied as-is: ${path}"
            ;;
        corrupt)
            log CORRUPT "Could not extract (corrupt or unsupported), left/copied as-is: ${path} - $(grep -m1 -iE 'unexpected|error' <<< "${EXTRACT_ERR}")"
            ;;
    esac
}

process_pst() {
    local path="$1" sha dest
    sha="$(sha256sum "${path}" | awk '{print $1}')"
    dest="/extracted/files/${sha}"

    if [[ -e "${dest}" ]]; then
        dispose_of_original "${path}" pst
        return
    fi
    mkdir -p "${dest}"

    if readpst -D -S -j 2 -q -r -o "${dest}" "${path}" < /dev/null 2>>"${LOG}"; then
        log EXTRACTED "Extracted PST: ${path} -> ${dest}"
        queue_new_files "${dest}"
        dispose_of_original "${path}" pst
    else
        rmdir "${dest}" 2>/dev/null
        [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
        log CORRUPT "Could not extract PST (corrupt or unsupported), left/copied as-is: ${path}"
    fi
}

process_one_file() {
    local path="$1"
    [[ -f "${path}" ]] || return   # may already have been consumed elsewhere
    if [[ "${path,,}" == *.pst ]]; then
        if config_true pst; then
            process_pst "${path}"
        else
            [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
            log COPIED "PST extraction disabled (pst=false), left/copied as-is: ${path}"
        fi
    else
        process_zip_like "${path}"
    fi
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
    mapfile -t PASSWORDS < <(build_password_list | awk '!seen[$0]++')

    local -a current
    mapfile -d '' -t current < <(find /files -maxdepth 1 -type f -print0 \
        | grep -zvE "${CONTROL_FILES}" \
        | sort -z)

    local max_depth
    max_depth="$(config_int max_depth "${MAX_DEPTH_DEFAULT}")"
    local depth=1

    while (( ${#current[@]} > 0 )); do
        if (( depth > max_depth )); then
            log DEPTH-LIMIT "Reached max_depth=${max_depth} with ${#current[@]} file(s) still to check; left as-is: ${current[*]}"
            break
        fi
        echo "Round ${depth}: ${#current[@]} file(s) to check."
        NEXT_ROUND=()
        for path in "${current[@]}"; do
            process_one_file "${path}"
        done
        current=("${NEXT_ROUND[@]}")
        depth=$(( depth + 1 ))
    done
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
