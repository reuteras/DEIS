#!/bin/bash
# Shared helpers for the download stage's scripts.
#
# The routing decision below lives here rather than in addurl.sh because
# two scripts now depend on it and they must not be able to disagree:
# addurl.sh uses it to decide whether a given URL goes through the Tor
# proxy, and torcheck.sh uses it to decide whether a broken Tor proxy is
# grounds for refusing to start the batch at all (item 42). If those two
# ever answered differently, the preflight would be checking something
# other than what the downloader actually does - which is the one failure
# a leak test must not have.

# Extracts the host from a URL, so that a clearnet link which merely
# mentions ".onion" somewhere in its path or query is not mistaken for a
# hidden service.
url_host() {
    local url="$1" host
    host="${url#*://}"      # drop the scheme
    host="${host%%/*}"      # drop the path
    host="${host%%\?*}"     # drop a query string on a path-less URL
    host="${host##*@}"      # drop any user:password@
    host="${host%%:*}"      # drop the port
    printf '%s' "${host}"
}

# True when this URL will be fetched through Tor. .onion has to be -
# nothing else resolves it - and FORCE_TOR=true opts everything in.
url_needs_tor() {
    local host
    host="$(url_host "$1")"
    [[ "${host}" == *.onion ]] || [[ "${FORCE_TOR:-false}" == "true" ]]
}
