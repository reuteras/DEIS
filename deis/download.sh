#!/bin/bash

# Only evaluate once urls.sh has actually submitted something, and only once
# (skip once we've already recorded a final downloaded/download_failed result).
[[ -f /files/added_urls ]] || exit 0
[[ -f /files/downloaded ]] && exit 0
[[ -f /files/download_failed ]] && exit 0
[[ -s /files/batch_gids ]] || exit 0

# Purely a display flag for bin/progress.py; not used for control flow below.
touch /files/running

stopped="$(curl --silent "http://downloader:6800/jsonrpc" --header "Content-Type: application/json" --header "Accept: application/json" --data '
{
    "jsonrpc": "2.0",
    "id": "'"${RANDOM}"'",
    "method": "aria2.tellStopped",
    "params": [
        "token:'"${RPCSECRET}"'",
        0,
        1000,
        ["gid", "status", "errorMessage", "files"]
    ]
}')"

# Only look at the GIDs this batch actually submitted (deis/urls.sh writes
# them to /files/batch_gids). This deliberately never purges or otherwise
# touches aria2's own history, so AriaNg's Stopped/Waiting views stay intact.
pending=false
errors=""
while IFS= read -r gid; do
    [[ -z "${gid}" ]] && continue
    entry="$(echo "${stopped}" | jq -c --arg gid "${gid}" '.result[] | select(.gid == $gid)')"
    if [[ -z "${entry}" ]]; then
        pending=true
        continue
    fi
    if [[ "$(echo "${entry}" | jq -r '.status')" == "error" ]]; then
        uri="$(echo "${entry}" | jq -r '.files[0].uris[0].uri')"
        msg="$(echo "${entry}" | jq -r '.errorMessage')"
        errors+="${gid} ${uri} -> ${msg}"$'\n'
    fi
done < /files/batch_gids

if [[ "${pending}" == "true" ]]; then
    exit 0
fi

rm -f /files/running

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
