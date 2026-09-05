#!/bin/bash
# Preflight TOR egress leak test (docs/IMPROVEMENTS.md item 42).
#
# Asks check.torproject.org which IP it sees, and refuses to let the batch
# start if a URL that must go through TOR would in fact go out in the
# clear. For a non-expert operator this is the most valuable opsec check
# in the tool, because the failure it guards against is silent: a broken
# proxy chain does not announce itself, it just downloads faster.
#
# The probe is submitted through aria2's own RPC, with exactly the proxy
# options deis/addurl.sh would compute for a URL of that kind, rather than
# through a separate curl. That matters for three reasons: it exercises
# the real code path (aria2 -> v2ray on 127.0.0.1:16001 -> tor on 9050)
# instead of a parallel one that could be configured differently and pass
# while the real one leaks; v2ray is bound to loopback *inside* the
# downloader container, so nothing outside it can reach the proxy anyway;
# and the downloader image has no curl (it is apk del'd at the end of the
# build), so there is nothing there to probe with even if it were.
#
# Policy, per item 42: assert the *intended* routing rather than blanket
# TOR use. Since 5ce18cc, .onion goes through TOR and clearnet goes direct
# unless FORCE_TOR=true. So TOR is only required when the batch actually
# contains a .onion URL, or FORCE_TOR is set; a clearnet-only batch is
# reported on but not blocked by a TOR failure it would never have used.

set -u

# Resolved at runtime inside the container; same suppression
# setup/entrypoint.sh already uses for its own lib.sh.
# shellcheck disable=SC1091
source "${BASH_SOURCE[0]%/*}/lib.sh"

# All five default to the values that apply inside the deis container.
# They are overridable only so this can be exercised from the host against
# a running downloader, which is how the check was verified end to end -
# production behaviour is whatever the defaults do.
ARIA2_RPC_URL="${ARIA2_RPC_URL:-http://downloader:6800/jsonrpc}"
URLS_DIR="${URLS_DIR:-/urls}"
TORCHECK_LOG="${TORCHECK_LOG:-/logs/download_errors.log}"

PROBE_URL="${TORCHECK_URL:-https://check.torproject.org/api/ip}"
# A dot-directory under aria2's own download dir: it is the one path both
# containers can see (./downloader/data is /data in downloader and
# /downloader/data here). Probes are removed as soon as they are read, and
# deis/done.sh skips this directory, so a probe left behind by a crash can
# never be swept into /files and indexed as if it were leaked evidence.
PROBE_DIR_LOCAL="${PROBE_DIR_LOCAL:-/downloader/data/.torcheck}"
PROBE_DIR_ARIA=/data/.torcheck
# Generous enough for a cold TOR circuit, which can take a while to build
# right after the container starts.
TORCHECK_TIMEOUT="${TORCHECK_TIMEOUT:-120}"

CLEARNET_OPTIONS='{"all-proxy":"","http-proxy":"","https-proxy":""}'
TOR_OPTIONS='{}'

log_error() {
    echo "$1" >&2
    echo "$(date -Iseconds) $1" >> "${TORCHECK_LOG}"
}

rpc() {
    curl --silent --max-time 20 "${ARIA2_RPC_URL}" \
        --header "Content-Type: application/json" \
        --header "Accept: application/json" \
        --data "$1"
}

# Fetches PROBE_URL through aria2 with the given options and prints the
# response body. Returns non-zero if it could not be fetched at all.
probe() {
    local options="$1" name="$2" request gid status deadline path

    mkdir -p "${PROBE_DIR_LOCAL}"
    rm -f "${PROBE_DIR_LOCAL}/${name}"

    # max-tries=1 overrides aria2.conf's max-tries=0 (retry forever), which
    # is right for a real download over a flaky circuit but would make a
    # genuinely unreachable probe hang until the timeout instead of failing.
    request="$(jq -n -c \
        --arg secret "token:${RPCSECRET}" \
        --arg url "${PROBE_URL}" \
        --arg dir "${PROBE_DIR_ARIA}" \
        --arg out "${name}" \
        --argjson options "${options}" \
        '{jsonrpc: "2.0", id: "torcheck", method: "aria2.addUri",
          params: [$secret, [$url],
                   ($options + {dir: $dir, out: $out, "allow-overwrite": "true",
                                "auto-file-renaming": "false", "max-tries": "1"})]}')"

    gid="$(rpc "${request}" | jq -r '.result // empty')"
    if [[ -z "${gid}" ]]; then
        return 1
    fi

    deadline=$(( $(date +%s) + TORCHECK_TIMEOUT ))
    while (( $(date +%s) < deadline )); do
        status="$(rpc "$(jq -n -c \
            --arg secret "token:${RPCSECRET}" \
            --arg gid "${gid}" \
            '{jsonrpc: "2.0", id: "torcheck", method: "aria2.tellStatus",
              params: [$secret, $gid, ["status"]]}')" | jq -r '.result.status // empty')"
        case "${status}" in
            complete) break ;;
            error|removed) return 1 ;;
        esac
        sleep 2
    done

    path="${PROBE_DIR_LOCAL}/${name}"
    [[ -s "${path}" ]] || return 1
    cat "${path}"
    rm -f "${path}"
}

# check.torproject.org/api/ip answers {"IsTor":true,"IP":"..."}.
is_tor() { jq -r '.IsTor // false' <<< "$1"; }
exit_ip() { jq -r '.IP // "unknown"' <<< "$1"; }

# Does anything in this batch actually need TOR?
tor_required() {
    local url
    [[ "${FORCE_TOR:-false}" == "true" ]] && return 0
    while IFS= read -r url; do
        [[ -z "${url}" || "${url}" == \#* ]] && continue
        url_needs_tor "${url}" && return 0
    done < <(cat "${URLS_DIR}"/* 2>/dev/null)
    return 1
}

main() {
    local required=1 body tor_ok

    if tor_required; then
        required=0
    fi

    if (( required != 0 )); then
        echo "TOR preflight: no .onion URLs queued and FORCE_TOR is not set - nothing will use TOR."
        # Still worth reporting where clearnet traffic actually exits, so an
        # operator who believes they are behind a VPN can confirm it. Never
        # fatal: this path is not supposed to be TOR.
        if body="$(probe "${CLEARNET_OPTIONS}" "clearnet.json")"; then
            echo "TOR preflight: direct (non-TOR) downloads exit from $(exit_ip "${body}"). Confirm this is your VPN, not your own address."
        else
            echo "TOR preflight: could not reach ${PROBE_URL} directly to report the clearnet exit IP." >&2
        fi
        return 0
    fi

    echo "TOR preflight: .onion URLs queued or FORCE_TOR set - verifying egress really goes through TOR."
    if ! body="$(probe "${TOR_OPTIONS}" "tor.json")"; then
        log_error "TOR preflight FAILED: could not reach ${PROBE_URL} through the configured proxy chain. Refusing to start downloads that were meant to go through TOR."
        return 1
    fi

    tor_ok="$(is_tor "${body}")"
    if [[ "${tor_ok}" != "true" ]]; then
        log_error "TOR preflight FAILED: traffic meant for TOR exited from $(exit_ip "${body}"), which check.torproject.org does not recognise as a TOR exit node. This would have leaked your real address. Refusing to start downloads."
        return 1
    fi

    echo "TOR preflight OK: proxied traffic exits from TOR node $(exit_ip "${body}")."
    return 0
}

main "$@"
