#!/bin/bash

function usage {
    echo "Usage: ${0} <onion url>"
	echo "   <onion url> should end with a /"
}

if [[ "$#" != "1" ]]; then
    usage
    exit
fi

if [[ "${1}" == '-h' || "${1}" == '--help' ]]; then
    usage
    exit
fi

if [[ -f "list.txt" ]]; then
	echo "ERROR: list.txt already exists. Remove and run the script again."
	exit
fi

URL="${1}"
LIST=$(echo "${URL}" | sed -E "s#.*onion/##" | tr -d '/')

for filename in $(curl -s --socks5-hostname 127.0.0.1:9050 "${URL}" | grep -E '\.rar</a>' | cut -f2 -d\"); do
    echo "${URL}${filename}" >> "./urls/${LIST}.txt"
done
