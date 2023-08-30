#!/bin/bash

curl --silent "http://downloader:6800/jsonrpc" --header "Content-Type: application/json" --header "Accept: application/json" --data '
{
    "jsonrpc": "2.0",
    "id": "'"${RANDOM}"'",
    "method": "aria2.tellActive",
    "params": [
        "token:'"${RPCSECRET}"'",
                ["gid"]
              ]
}' | jq '.result' | grep gid
