#!/bin/bash

log_error() {
    echo "$1"
    echo "$(date -Iseconds) $1" >> /logs/download_errors.log
}

if [[ ! -f /files/added_urls ]]; then
    if [[ "$(wc -l /urls/* | tail -1 | awk '{print $1}')" != "0" ]]; then
        echo "Waiting for Aria2"
        while ! curl -s http://downloader:6800 > /dev/null 2>&1; do
            sleep 1
        done
        echo "Aria2 is up."

        sleep 1

        while read -r url; do
            # Skip blank lines and comments.
            [[ -z "${url}" || "${url}" == \#* ]] && continue

            # aria2 is only set up to fetch files over these schemes. Anything
            # else (magnet:, file:, ...) would either fail or bypass the proxy
            # rules in addurl.sh, so refuse it loudly instead of dropping it.
            case "${url}" in
                http://*|https://*|ftp://*) ;;
                *)
                    log_error "Skipping URL with unsupported scheme: ${url}"
                    continue
                    ;;
            esac

            echo "Adding URL: ${url}"
            if gid="$(/deis/bin/addurl.sh "${url}")" && [[ -n "${gid}" && "${gid}" != "null" ]]; then
                echo "${gid}" >> /files/batch_gids
            else
                log_error "Could not queue URL: ${url}"
            fi
        done < <(cat /urls/* | sort | uniq)

        # Used by download.sh to tell a slow batch from a stuck one.
        date +%s > /files/batch_started
    fi

    echo ""
    echo "Added URLs and creating /files/added_urls"
    touch /files/added_urls
fi
