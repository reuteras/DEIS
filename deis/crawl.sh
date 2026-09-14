#!/bin/bash

# Expands a "directory listing" leak site (an Apache/nginx "Index of /" or
# similar, with no single archive to download - just links to files and
# subfolders) into the concrete file URLs deis/urls.sh already knows how to
# queue. Triggered by `deis crawl-site <root-url>`, which writes the root
# into /urls/crawl_roots.txt.
#
# Each listing page is fetched through aria2 via deis/addurl.sh, unchanged -
# that is deliberate: addurl.sh already makes the right TOR-vs-clearnet
# routing decision per URL and item 42's egress preflight already verifies
# that routing actually holds, and this reuses both rather than opening a
# second, unverified path to the leak site from this container. Pages are
# saved under aria2's own /data/.crawl (addurl.sh's optional "dir" arg),
# kept out of deis/done.sh's sweep into /files/ the same way .torcheck's
# probe responses are - a site's own listing pages are not leak data.
#
# Runs like every other stage here: idempotent per cron tick, resumable
# after a crash. State lives in /status so a restart picks up exactly where
# it left off rather than restarting the whole crawl:
#   crawl_frontier.txt  - append-only queue: "depth<TAB>root<TAB>url" per line
#   crawl_frontier_pos  - how many of that file's lines are already handled
#   crawl_visited.txt   - URLs already fetched and parsed, for de-dup
# Discovered file URLs land in /urls/discovered.txt, which deis/urls.sh's
# existing "cat /urls/* | sort | uniq" sweep already picks up unchanged.

set -u

# shellcheck disable=SC1091
source "${BASH_SOURCE[0]%/*}/lib.sh"

CRAWL_ROOTS=/urls/crawl_roots.txt
FRONTIER=/status/crawl_frontier.txt
FRONTIER_POS=/status/crawl_frontier_pos
VISITED=/status/crawl_visited.txt
DISCOVERED=/urls/discovered.txt
CRAWL_LOG=/logs/download_errors.log

MAX_DEPTH_DEFAULT=10
MAX_URLS_DEFAULT=20000
# Leaves headroom in the 60s cron tick for the rest of the loop's own
# overhead; a page fetch that is still running when the budget runs out is
# simply picked up again next tick (aria2 dedupes an identical URL already
# in flight rather than double-fetching it).
TICK_BUDGET="${CRAWL_TICK_BUDGET:-45}"
# Generous enough for a cold TOR circuit - same reasoning as
# torcheck.sh's own TORCHECK_TIMEOUT.
PAGE_TIMEOUT="${CRAWL_PAGE_TIMEOUT:-120}"

[[ -s "${CRAWL_ROOTS}" ]] || exit 0
[[ -f /status/crawl_done ]] && exit 0

log_error() {
    echo "$1"
    echo "$(date -Iseconds) $1" >> "${CRAWL_LOG}"
}

rpc() {
    curl --silent --max-time 20 "http://downloader:6800/jsonrpc" \
        --header "Content-Type: application/json" \
        --header "Accept: application/json" \
        --data "$1"
}

# Reads a single key from /deis.cfg's [crawl] section - same shape as
# unpack/start.sh's own read_cfg/config_int for [unpack].
read_cfg() {
    awk -F= -v key="$1" '
        /^\[/ { insection = ($0 == "[crawl]"); next }
        insection && $1 == key { print substr($0, index($0, "=") + 1); found = 1 }
        END { exit !found }
    ' /deis.cfg 2>/dev/null
}

config_int() {
    local value
    value="$(read_cfg "$1")"
    [[ "${value}" =~ ^[0-9]+$ ]] && echo "${value}" || echo "$2"
}

max_depth="$(config_int max_depth "${MAX_DEPTH_DEFAULT}")"
max_urls="$(config_int max_urls "${MAX_URLS_DEFAULT}")"

if [[ ! -f "${FRONTIER}" ]]; then
    echo "Crawl starting: running TOR preflight before the first request."
    if ! /deis/bin/torcheck.sh; then
        log_error "Refusing to start crawl: the TOR preflight check failed (see above)."
        touch /status/download_failed
        exit 1
    fi

    : > "${VISITED}"
    : > "${DISCOVERED}"
    echo 0 > "${FRONTIER_POS}"
    while IFS= read -r root; do
        [[ -z "${root}" || "${root}" == \#* ]] && continue
        printf '0\t%s\t%s\n' "${root}" "${root}" >> "${FRONTIER}"
    done < "${CRAWL_ROOTS}"
fi

mkdir -p /downloader/data/.crawl

# Approximate total URLs discovered so far: already-visited plus whatever is
# still queued ahead of the current position. Just a safety cap, not an
# exact count, so an approximation is fine.
url_count() {
    local visited total
    visited="$(wc -l < "${VISITED}" 2>/dev/null || echo 0)"
    total="$(wc -l < "${FRONTIER}" 2>/dev/null || echo 0)"
    echo $(( visited + total - pos ))
}

# Persists pos, i.e. "this frontier entry is fully handled" - deliberately
# called only after every durable side effect of processing it (VISITED/
# DISCOVERED/FRONTIER appends) has already happened, on every code path
# below, never before. Advancing pos first and fetching/parsing second
# would mean a crash between the two permanently skips that URL and
# whatever it would have discovered - silent data loss, the one failure
# mode this pipeline exists to avoid.
advance() {
    pos=$(( pos + 1 ))
    echo "${pos}" > "${FRONTIER_POS}"
}

pos="$(cat "${FRONTIER_POS}" 2>/dev/null || echo 0)"
[[ "${pos}" =~ ^[0-9]+$ ]] || pos=0

while (( SECONDS < TICK_BUDGET )); do
    total="$(wc -l < "${FRONTIER}" 2>/dev/null || echo 0)"
    (( pos < total )) || break

    entry="$(sed -n "$(( pos + 1 ))p" "${FRONTIER}")"
    depth="$(cut -f1 <<< "${entry}")"
    root="$(cut -f2 <<< "${entry}")"
    url="$(cut -f3 <<< "${entry}")"

    if [[ -z "${url}" ]]; then
        advance
        continue
    fi
    if grep -Fxq "${url}" "${VISITED}" 2>/dev/null; then
        advance
        continue
    fi
    if (( depth > max_depth )); then
        log_error "Crawl: max_depth=${max_depth} reached, not following ${url}"
        advance
        continue
    fi
    if (( $(url_count) >= max_urls )); then
        log_error "Crawl: max_urls=${max_urls} reached, stopping crawl with URLs still queued."
        break
    fi

    echo "Crawling: ${url}"
    gid=""
    if ! gid="$(/deis/bin/addurl.sh "${url}" .crawl)" || [[ -z "${gid}" || "${gid}" == "null" ]]; then
        log_error "Crawl: could not queue listing page ${url}"
        echo "${url}" >> "${VISITED}"
        advance
        continue
    fi

    status="" path=""
    deadline=$(( $(date +%s) + PAGE_TIMEOUT ))
    while (( $(date +%s) < deadline )); do
        result="$(rpc "$(jq -n -c --arg secret "token:${RPCSECRET}" --arg gid "${gid}" \
            '{jsonrpc: "2.0", id: "crawl", method: "aria2.tellStatus",
              params: [$secret, $gid, ["status", "errorMessage", "files"]]}')")"
        status="$(jq -r '.result.status // empty' <<< "${result}")"
        case "${status}" in
            complete)
                path="$(jq -r '.result.files[0].path // empty' <<< "${result}")"
                break
                ;;
            error | removed)
                log_error "Crawl: fetching ${url} failed: $(jq -r '.result.errorMessage // "unknown error"' <<< "${result}")"
                break
                ;;
        esac
        sleep 2
    done

    if [[ "${status}" != "complete" || -z "${path}" ]]; then
        if [[ "${status}" != "error" && "${status}" != "removed" ]]; then
            log_error "Crawl: timed out after ${PAGE_TIMEOUT}s waiting for ${url}"
        fi
        # Mark visited even on failure - a listing page that is down or
        # times out should not be retried forever on every future tick.
        echo "${url}" >> "${VISITED}"
        advance
        continue
    fi

    links="$(python3 /deis/bin/crawl_links.py "${path}" "${url}" "${root}" 2>>"${CRAWL_LOG}")" || links=""
    rm -f "${path}"

    if [[ -z "${links}" ]]; then
        log_error "Crawl: could not parse listing page ${url}"
        echo "${url}" >> "${VISITED}"
        advance
        continue
    fi

    while IFS= read -r file_url; do
        [[ -z "${file_url}" ]] && continue
        echo "${file_url}" >> "${DISCOVERED}"
    done < <(jq -r '.files[]?' <<< "${links}")

    while IFS= read -r dir_url; do
        [[ -z "${dir_url}" ]] && continue
        if grep -Fxq "${dir_url}" "${VISITED}" 2>/dev/null; then
            continue
        fi
        printf '%s\t%s\t%s\n' "$(( depth + 1 ))" "${root}" "${dir_url}" >> "${FRONTIER}"
    done < <(jq -r '.dirs[]?' <<< "${links}")

    # Everything discovered from this page is now durably recorded above -
    # only now is it safe to mark the page itself visited and move on.
    echo "${url}" >> "${VISITED}"
    advance
done

total="$(wc -l < "${FRONTIER}" 2>/dev/null || echo 0)"
if (( pos < total )); then
    echo "Crawl: pausing for this tick, $(( total - pos )) URL(s) still queued."
else
    echo "Crawl finished. Creating /status/crawl_done."
    touch /status/crawl_done
fi
