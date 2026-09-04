#!/bin/bash

# Submits one URL to aria2 and prints the GID it was assigned.
#
# Routing: .onion URLs have to go through the Tor proxy configured in
# aria2.conf - nothing else can resolve them. Clearnet URLs are fetched
# directly instead, which is a lot faster and costs nothing in privacy as
# long as the operator is on a VPN. Set FORCE_TOR=true to push everything
# through Tor anyway.

set -u

url="${1}"

# Pick the host out of the URL so that a clearnet link which merely mentions
# ".onion" somewhere in its path isn't mistaken for a hidden service.
host="${url#*://}"      # drop the scheme
host="${host%%/*}"      # drop the path
host="${host##*@}"      # drop any user:password@
host="${host%%:*}"      # drop the port

if [[ "${host}" == *.onion ]] || [[ "${FORCE_TOR:-false}" == "true" ]]; then
    # Use the proxy from aria2.conf.
    options='{}'
else
    # Empty values override the global proxy for this download only.
    options='{"all-proxy":"","http-proxy":"","https-proxy":""}'
fi

# jq builds the payload so that a URL containing quotes or backslashes can't
# break out of the JSON string.
request="$(jq -n -c \
    --arg secret "token:${RPCSECRET}" \
    --arg url "${url}" \
    --arg id "${RANDOM}" \
    --argjson options "${options}" \
    '{jsonrpc: "2.0", id: $id, method: "aria2.addUri", params: [$secret, [$url], $options]}')"

response="$(curl --silent --show-error \
    "http://downloader:6800/jsonrpc" \
    --header "Content-Type: application/json" \
    --header "Accept: application/json" \
    --data "${request}")"

gid="$(echo "${response}" | jq -r '.result // empty')"

if [[ -z "${gid}" ]]; then
    reason="$(echo "${response}" | jq -r '.error.message // empty')"
    echo "ERROR: aria2 would not accept ${url}: ${reason:-no response from aria2}" >&2
    exit 1
fi

echo "${gid}"
