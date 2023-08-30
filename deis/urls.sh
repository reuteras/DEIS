#!/bin/bash

if [[ ! -f /files/added_urls ]]; then
    if [[ "$(wc -l /urls/* | tail -1 | awk '{print $1}')" != "0" ]]; then
        echo "Waiting for Aria2"
        while ! curl -s http://downloader:6800 > /dev/null 2>&1 ; do
            sleep 1
        done
        echo "Aria2 is up."

        sleep 1

        while read -r  url ; do
            echo "Adding URL: ${url}"
            /deis/bin/addurl.sh "${url}"
        done
        fi < <(cat /urls/* | sort | uniq )
        
        echo ""
        echo "Added URLs and creating /files/added_urls"
        touch /files/added_urls
fi
