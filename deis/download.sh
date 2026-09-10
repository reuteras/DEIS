#!/bin/bash

# Only evaluate once urls.sh has actually submitted something, and only once
# (skip once we've already recorded a final downloaded/download_failed result).
[[ -f /status/added_urls ]] || exit 0
[[ -f /status/downloaded ]] && exit 0
[[ -f /status/download_failed ]] && exit 0
[[ -s /status/batch_gids ]] || exit 0

# Purely a display flag for bin/progress.py; not used for control flow below.
touch /status/running

# aria2 keeps retrying a failing download forever (max-tries=0 in aria2.conf,
# which is what makes flaky Tor circuits survivable). Without a deadline a
# single unreachable URL would keep the whole pipeline waiting silently, so
# give up after DOWNLOAD_TIMEOUT seconds and report what is still stuck.
timeout="${DOWNLOAD_TIMEOUT:-86400}"
if [[ ! -f /status/batch_started ]]; then
    date +%s > /status/batch_started
fi
started="$(cat /status/batch_started)"

rpc() {
    curl --silent "http://downloader:6800/jsonrpc" \
        --header "Content-Type: application/json" \
        --header "Accept: application/json" \
        --data "$1"
}

# aria2.tellStopped returns at most one page, so walk it until it runs out.
# A batch with more entries than a single page would otherwise leave the
# oldest downloads looking permanently pending.
page_size=1000
offset=0
stopped=""
while true; do
    page="$(rpc "$(jq -n \
        --arg secret "token:${RPCSECRET}" \
        --arg id "${RANDOM}" \
        --argjson offset "${offset}" \
        --argjson num "${page_size}" \
        '{jsonrpc: "2.0", id: $id, method: "aria2.tellStopped",
        params: [$secret, $offset, $num, ["gid", "status", "errorMessage", "files"]]}')")"
    entries="$(echo "${page}" | jq -c '.result[]?')"
    [[ -z "${entries}" ]] && break
    stopped+="${entries}"$'\n'
    (( $(echo "${entries}" | grep -c '^') < page_size )) && break
    offset=$(( offset + page_size ))
done

# Only look at the GIDs this batch actually submitted (deis/urls.sh writes
# them to /status/batch_gids). This deliberately never purges or otherwise
# touches aria2's own history, so AriaNg's Stopped/Waiting views stay intact.
pending=""
pending_count=0
errors=""
while IFS= read -r gid; do
    [[ -z "${gid}" ]] && continue
    entry="$(echo "${stopped}" | jq -c --arg gid "${gid}" 'select(.gid == $gid)' | head -1)"
    if [[ -z "${entry}" ]]; then
        pending+="${gid}"$'\n'
        pending_count=$(( pending_count + 1 ))
        continue
    fi
    if [[ "$(echo "${entry}" | jq -r '.status')" == "error" ]]; then
        uri="$(echo "${entry}" | jq -r '.files[0].uris[0].uri')"
        msg="$(echo "${entry}" | jq -r '.errorMessage')"
        errors+="${gid} ${uri} -> ${msg}"$'\n'
    else
        # item 46's provenance tracking: record which URL produced which
        # filename while both are still known together - aria2's own path
        # (in /downloader/data/) is gone once deis/done.sh moves the file
        # into /files/, and done.sh's own dup-rename means the final
        # filename isn't decided yet either, so this only records the
        # pre-move basename; done.sh looks entries up by that same
        # basename before it does any renaming, then re-keys by sha256
        # (computed once the file is stable and named) into
        # /status/source_urls.jsonl. This loop re-runs on every
        # download.sh invocation until the whole batch finishes, so an
        # already-completed gid can be appended again here on a later
        # tick - harmless duplicate lines, not a correctness issue: done.sh
        # only needs one matching entry, and it stops looking once found.
        path="$(echo "${entry}" | jq -r '.files[0].path // empty')"
        uri="$(echo "${entry}" | jq -r '.files[0].uris[0].uri // empty')"
        if [[ -n "${path}" && -n "${uri}" ]]; then
            jq -nc --arg filename "$(basename "${path}")" --arg url "${uri}" \
                '{filename: $filename, url: $url}' >> /status/batch_urls.jsonl
        fi
    fi
done < /status/batch_gids

if [[ -n "${pending}" ]]; then
    # Report the count whenever it changes, so a slow batch still shows signs
    # of life instead of looking identical to a stuck one.
    if [[ "$(cat /status/pending_count 2>/dev/null)" != "${pending_count}" ]]; then
        echo "Waiting for ${pending_count} download(s) to finish."
        echo "${pending_count}" > /status/pending_count
    fi

    elapsed=$(( $(date +%s) - started ))
    if (( elapsed < timeout )); then
        exit 0
    fi

    stalled=""
    while IFS= read -r gid; do
        [[ -z "${gid}" ]] && continue
        status="$(rpc "$(jq -n \
            --arg secret "token:${RPCSECRET}" \
            --arg id "${RANDOM}" \
            --arg gid "${gid}" \
            '{jsonrpc: "2.0", id: $id, method: "aria2.tellStatus",
            params: [$secret, $gid, ["gid", "status", "errorMessage", "files"]]}')")"
        uri="$(echo "${status}" | jq -r '.result.files[0].uris[0].uri // "unknown URL"')"
        state="$(echo "${status}" | jq -r '.result.status // "unknown"')"
        stalled+="${gid} ${uri} -> still ${state} after ${elapsed}s"$'\n'
    done <<< "${pending}"

    echo "Giving up on ${pending_count} download(s) after ${elapsed}s:"
    echo "${stalled}"
    {
        echo "$(date -Iseconds) download timed out after ${elapsed}s:"
        echo "${stalled}"
        [[ -n "${errors}" ]] && echo "${errors}"
    } >> /logs/download_errors.log
    rm -f /status/running
    touch /status/download_failed
    exit 0
fi

rm -f /status/running
rm -f /status/pending_count

if [[ -n "${errors}" ]]; then
    echo "Download finished with errors, not marking as downloaded:"
    echo "${errors}"
    {
        echo "$(date -Iseconds) download errors:"
        echo "${errors}"
    } >> /logs/download_errors.log
    touch /status/download_failed
else
    echo "Download done. Creating /status/downloaded."
    touch /status/downloaded
fi
