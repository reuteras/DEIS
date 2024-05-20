#!/bin/bash

sleep 6

[[ -f /files/dies_done ]] && pkill -9 crond

if [[ -f /files/downloaded ]]; then
    if [[ ! -f /files/extract ]]; then
        touch /files/extract
        echo "Move files for extraction."
        while read -r path; do
            file=$(basename "$path")
            mv "/downloader/data/${file}" /files/
        done < <(find /downloader/data -type f -exec basename {} \; | grep -v .gitignore | sort)
        echo "Files have been moved and creating /files/unpack"
        touch /files/unpack /files/dies_done
    fi
fi
