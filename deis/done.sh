#!/bin/bash

sleep 6

[[ -f /status/dies_done ]] && pkill -9 crond

# Guarded on "not yet moved everything" rather than a one-shot flag, so a file
# that lands in /downloader/data late, or a move that failed the first time
# around (disk full, permissions), is retried on the next cron tick instead
# of being silently abandoned.
if [[ -f /status/downloaded ]] && [[ ! -f /status/unpack ]]; then
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
        else
            # item 46's provenance tracking: "${file}" (this file's name
            # before the dup-rename above, if any) is the same basename
            # deis/download.sh recorded a URL under in batch_urls.jsonl,
            # while it still had aria2's own pre-move path - look it up
            # by that original name, then re-key by sha256 (only stable
            # once the file has actually landed at its final path) into
            # source_urls.jsonl, which ingest.py reads. No entry (a file
            # dropped in some other way, or an older batch that predates
            # this feature) just means no known origin - not an error.
            sha256="$(sha256sum "${dest}" | cut -d' ' -f1)"
            url="$(jq -rs --arg filename "${file}" \
                'map(select(.filename == $filename)) | (.[0].url // empty)' \
                /status/batch_urls.jsonl 2>/dev/null)"
            jq -nc --arg sha256 "${sha256}" --arg url "${url}" --arg filename "${file}" \
                '{sha256: $sha256, url: $url, filename: $filename}' >> /status/source_urls.jsonl
        fi
    # .torcheck holds item 42's preflight probe responses, not leak data.
    # torcheck.sh deletes each probe as soon as it reads it, so this only
    # matters if one was left behind by a crash - but sweeping it into
    # /files would index a check.torproject.org response as evidence, so
    # prune the directory rather than rely on that cleanup.
    done < <(find /downloader/data -name .torcheck -prune -o -type f ! -name '.gitignore' -print0 | sort -z)

    remaining="$(find /downloader/data -name .torcheck -prune -o -type f ! -name '.gitignore' -print | wc -l | tr -d ' ')"
    if (( failed > 0 || remaining > 0 )); then
        echo "Move incomplete: ${failed} failed, ${remaining} file(s) left in /downloader/data. Will retry."
    else
        echo "Files have been moved and creating /status/unpack"
        touch /status/unpack /status/dies_done
    fi
fi
