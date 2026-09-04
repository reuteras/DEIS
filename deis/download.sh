#!/bin/bash

# Only evaluate once urls.sh has actually submitted something, and only once
# (skip once we've already recorded a final downloaded/download_failed result).
[[ -f /files/added_urls ]] || exit 0
[[ -f /files/downloaded ]] && exit 0
[[ -f /files/download_failed ]] && exit 0
[[ -s /files/batch_gids ]] || exit 0

# Purely a display flag for bin/progress.py; not used for control flow below.
touch /files/running

# aria2 keeps retrying a failing download forever (max-tries=0 in aria2.conf,
# which is what makes flaky Tor circuits survivable). Without a deadline a
# single unreachable URL would keep the whole pipeline waiting silently, so
# give up after DOWNLOAD_TIMEOUT seconds and report what is still stuck.
timeout="${DOWNLOAD_TIMEOUT:-86400}"
if [[ ! -f /files/batch_started ]]; then
    date +%s > /files/batch_started
fi
started="$(cat /files/batch_started)"

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
# them to /files/batch_gids). This deliberately never purges or otherwise
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
    fi
done < /files/batch_gids

if [[ -n "${pending}" ]]; then
    # Report the count whenever it changes, so a slow batch still shows signs
    # of life instead of looking identical to a stuck one.
    if [[ "$(cat /files/pending_count 2>/dev/null)" != "${pending_count}" ]]; then
        echo "Waiting for ${pending_count} download(s) to finish."
        echo "${pending_count}" > /files/pending_count
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
    rm -f /files/running
    touch /files/download_failed
    exit 0
fi

rm -f /files/running
rm -f /files/pending_count

if [[ -n "${errors}" ]]; then
    echo "Download finished with errors, not marking as downloaded:"
    echo "${errors}"
    {
        echo "$(date -Iseconds) download errors:"
        echo "${errors}"
    } >> /logs/download_errors.log
    touch /files/download_failed
else
    echo "Download done. Creating /files/downloaded."
    touch /files/downloaded
fi
