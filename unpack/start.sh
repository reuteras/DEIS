#!/bin/bash

set -u

LOG=/logs/unpack.log
STILL_ENCRYPTED=/extracted/still_encrypted.txt
MAX_DEPTH_DEFAULT=6
PARALLELISM="${PARALLELISM:-$(command -v nproc > /dev/null && nproc || echo 4)}"

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

    if try_extract "${path}" "${dest}"; then
        [[ -n "${resultfile}" ]] && echo "extracted" > "${resultfile}"
        log EXTRACTED "Extracted: ${path} -> ${dest}"
        queue_new_files "${dest}"
        dispose_of_original "${path}" zip
        return
    fi

    rmdir "${dest}" 2>/dev/null
    [[ -n "${resultfile}" ]] && echo "${EXTRACT_RESULT}" > "${resultfile}"
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
    local path="$1" sha="$2" resultfile="${3:-}" dest="/extracted/files/${2}"

    if [[ -e "${dest}" ]]; then
        [[ -n "${resultfile}" ]] && echo "extracted" > "${resultfile}"
        dispose_of_original "${path}" pst
        return
    fi
    mkdir -p "${dest}"

    if readpst -D -S -j 2 -q -r -o "${dest}" "${path}" < /dev/null 2>>"${LOG}"; then
        [[ -n "${resultfile}" ]] && echo "extracted" > "${resultfile}"
        log EXTRACTED "Extracted PST: ${path} -> ${dest}"
        queue_new_files "${dest}"
        dispose_of_original "${path}" pst
    else
        rmdir "${dest}" 2>/dev/null
        [[ -n "${resultfile}" ]] && echo "corrupt" > "${resultfile}"
        [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
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
            [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
            log COPIED "Not an archive (same content already checked this round), left/copied as-is: ${path}"
            ;;
        encrypted)
            [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
            echo "${path}" >> "${STILL_ENCRYPTED}"
            log ENCRYPTED "Still encrypted (same content already checked this round), left/copied as-is: ${path}"
            ;;
        corrupt)
            [[ "${path}" == /extracted/files/* ]] || cp "${path}" /extracted/files/
            log CORRUPT "Could not extract (same content already checked this round), left/copied as-is: ${path}"
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
# Everything this needs - functions and WORKDIR/LOG/STILL_ENCRYPTED - is
# exported into the environment before xargs -P spawns these. Takes only the
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
    queue_new_files try_extract process_zip_like process_pst apply_known_result \
    worker_entrypoint process_one_file

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
    WORKDIR="$(mktemp -d)"
    export WORKDIR LOG STILL_ENCRYPTED
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
