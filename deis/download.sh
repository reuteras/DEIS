#!/bin/bash

if /deis/bin/download_status.sh | grep "gid" > /dev/null ; then
    if [[ ! -f /files/running ]]; then
        echo "Download is running. Creating /files/running."
        touch /files/running
    fi
else
    if [[ -f /files/running ]]; then
        rm /files/running
        if [[ ! -f /files/downloaded ]]; then
            echo "Download done. Creating /files/downloaded."
            touch /files/downloaded
        fi
    fi
fi
