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

# True for a magnet URI or a URL naming a .torrent file - the two entry
# points aria2 uses to switch into BitTorrent mode (--follow-torrent is on
# by default, so fetching a .torrent file transitions straight into a BT
# download of whatever it describes). Query string is stripped first so
# "foo.torrent?dl=1" still counts.
url_is_bittorrent() {
    local url="$1" path
    [[ "${url}" == magnet:* ]] && return 0
    path="${url%%\?*}"
    # tr, not bash 4's ${path,,} - the container's bash is modern enough
    # either way, but macOS's own /bin/bash (3.2, still the system default)
    # isn't, and this only needs to be portable, not fast.
    path="$(printf '%s' "${path}" | tr '[:upper:]' '[:lower:]')"
    [[ "${path}" == *.torrent ]]
}

# True when a BitTorrent URL must be refused rather than queued.
#
# aria2 has no proxy support at all for BitTorrent traffic: every
# *-proxy option is tagged only #http/#https/#ftp in aria2's own option
# metadata, and none of the dozens of options tagged #bittorrent (trackers,
# peers, DHT, peer exchange) is proxy-aware - there is no --bt-proxy option
# at all. Confirmed directly against aria2 1.37.0 (`aria2c
# --help=#bittorrent`), the version downloader/Dockerfile builds, not
# assumed from memory. That means a magnet/.torrent URL can never be
# routed through TOR the way url_needs_tor() routes everything else, so
# FORCE_TOR (this pipeline's explicit "route everything through TOR"
# opt-in) opts BitTorrent out entirely rather than silently downloading it
# in the clear anyway. An .onion-hosted .torrent file's own metadata fetch
# would itself be proxied correctly by url_needs_tor()'s ordinary host
# check, but the BT content download aria2 automatically starts once it
# has parsed that file never is - so that case is refused too, not
# silently downgraded to an unprotected direct download.
url_bt_refused() {
    local url="$1"
    [[ "${FORCE_TOR:-false}" == "true" ]] && return 0
    [[ "${url}" == magnet:* ]] && return 1
    [[ "$(url_host "${url}")" == *.onion ]]
}
