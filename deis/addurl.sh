#!/bin/bash

# Submits one URL to aria2 and prints the GID it was assigned.

curl --silent "http://downloader:6800/jsonrpc" --header "Content-Type: application/json" --header "Accept: application/json" --data '
{
    "jsonrpc": "2.0",
    "id": "'"${RANDOM}"'",
    "method": "aria2.addUri",
    "params": [
        "token:'"${RPCSECRET}"'",
            [
                    "'"${1}"'"
            ]
        ]
}' | jq -r '.result'
