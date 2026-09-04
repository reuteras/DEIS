#!/bin/bash

sleep 6

[[ -f /files/dies_done ]] && pkill -9 crond

# Guarded on "not yet moved everything" rather than a one-shot flag, so a file
# that lands in /downloader/data late, or a move that failed the first time
# around (disk full, permissions), is retried on the next cron tick instead
# of being silently abandoned.
if [[ -f /files/downloaded ]] && [[ ! -f /files/unpack ]]; then
    echo "Move files for extraction."
    failed=0
    while IFS= read -r -d '' path; do
        file="$(basename "${path}")"
        dest="/files/${file}"
        if [[ -e "${dest}" ]]; then
            # Two different downloads producing the same filename would
            # otherwise silently clobber one of them. Disambiguate rather than
            # skip: skipping would leave the file in /downloader/data forever,
            # which would stall the pipeline on one clashing name.
            base="${file%.*}"
            ext="${file##*.}"
            [[ "${base}" == "${file}" ]] && ext=""
            n=2
            while [[ -e "/files/${base}-dup${n}${ext:+.${ext}}" ]]; do
                n=$(( n + 1 ))
            done
            dest="/files/${base}-dup${n}${ext:+.${ext}}"
            echo "$(date -Iseconds) WARNING: /files/${file} already exists, moving ${path} to ${dest} instead." >> /logs/download_errors.log
            echo "WARNING: /files/${file} already exists, moving ${path} to ${dest} instead."
        fi
        if ! mv "${path}" "${dest}"; then
            echo "$(date -Iseconds) ERROR: could not move ${path} to ${dest}" >> /logs/download_errors.log
            echo "ERROR: could not move ${path} to ${dest}"
            failed=$(( failed + 1 ))
        fi
    done < <(find /downloader/data -type f ! -name '.gitignore' -print0 | sort -z)

    remaining="$(find /downloader/data -type f ! -name '.gitignore' | wc -l | tr -d ' ')"
    if (( failed > 0 || remaining > 0 )); then
        echo "Move incomplete: ${failed} failed, ${remaining} file(s) left in /downloader/data. Will retry."
    else
        echo "Files have been moved and creating /files/unpack"
        touch /files/unpack /files/dies_done
    fi
fi
