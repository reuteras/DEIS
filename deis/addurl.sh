#!/bin/bash

# Submits one URL to aria2 and prints the GID it was assigned.
#
# Routing: .onion URLs have to go through the Tor proxy configured in
# aria2.conf - nothing else can resolve them. Clearnet URLs are fetched
# directly instead, which is a lot faster and costs nothing in privacy as
# long as the operator is on a VPN. Set FORCE_TOR=true to push everything
# through Tor anyway.

set -u

# Resolved at runtime inside the container; same suppression
# setup/entrypoint.sh already uses for its own lib.sh.
# shellcheck disable=SC1091
source "${BASH_SOURCE[0]%/*}/lib.sh"

url="${1}"
# Optional: save under this subdirectory of aria2's /data instead of its
# root. deis/crawl.sh passes ".crawl" so listing pages it fetches to
# discover a site's file tree land next to, but separate from, real
# downloads - deis/done.sh prunes this directory rather than sweeping a
# leak site's own "Index of /" pages into /files/ as if they were evidence,
# the same way it already prunes torcheck.sh's .torcheck probe directory.
dir="${2:-}"

if url_needs_tor "${url}"; then
    # Use the proxy from aria2.conf.
    options='{}'
else
    # Empty values override the global proxy for this download only.
    options='{"all-proxy":"","http-proxy":"","https-proxy":""}'
fi

if [[ -n "${dir}" ]]; then
    options="$(jq -c --arg dir "/data/${dir}" '. + {dir: $dir}' <<< "${options}")"
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
