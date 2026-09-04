#!/usr/bin/env python3
"""Generate downloader/conf/config.json from TORSERVNUM.

v2ray load-balances downloads across one loopback-bound outbound per desired
parallel circuit, all pointing at the single local TOR SOCKS port. Every
outbound used to be hardcoded in config.json (tor-1..tor-50), so TORSERVNUM
looked like a live setting but changing it did nothing. Run this once before
starting v2ray instead, and it becomes the actual count.
"""

import json
import os
import sys

DEFAULT_COUNT = 50
# 127.0.0.0/8 is loopback, so this is generous; capped mainly so a typo in
# TORSERVNUM (e.g. an extra zero) fails loudly instead of writing a
# multi-thousand-outbound config.
MAX_COUNT = 250
TOR_SOCKS_PORT = 9050


def outbound_count():
    raw = os.environ.get("TORSERVNUM", "").strip()
    if not raw:
        return DEFAULT_COUNT
    try:
        count = int(raw)
    except ValueError:
        print(f"WARNING: TORSERVNUM={raw!r} is not an integer, using {DEFAULT_COUNT}.", file=sys.stderr)
        return DEFAULT_COUNT
    if not 1 <= count <= MAX_COUNT:
        print(f"WARNING: TORSERVNUM={count} is out of range 1-{MAX_COUNT}, using {DEFAULT_COUNT}.", file=sys.stderr)
        return DEFAULT_COUNT
    return count


def build_config(count):
    outbounds = [
        {
            "protocol": "socks",
            "sendThrough": f"127.0.0.{i}",
            "tag": f"tor-{i}",
            "settings": {"servers": [{"address": "127.0.0.1", "port": TOR_SOCKS_PORT}]},
        }
        for i in range(1, count + 1)
    ]
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "port": 16001,
                "listen": "127.0.0.1",
                "protocol": "http",
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            }
        ],
        "outbounds": outbounds,
        "routing": {
            "rules": [{"type": "field", "network": "tcp", "balancerTag": "balancer"}],
            "balancers": [{"tag": "balancer", "selector": ["tor-"], "strategy": {"type": "random"}}],
        },
    }


if __name__ == "__main__":
    count = outbound_count()
    print(f"Generating v2ray config with {count} outbound(s) (TORSERVNUM).", file=sys.stderr)
    json.dump(build_config(count), sys.stdout, indent=2)
    print(file=sys.stdout)
