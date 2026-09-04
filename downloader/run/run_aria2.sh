#!/bin/sh
set -e

touch /conf/aria2.session
touch /log/aria2_log.txt

# creatorrc.py picks the fastest guards and exits and writes a torrc, which is
# what makes downloads over TOR reasonably quick. It writes tor_config.txt into
# the current directory, and the image's WORKDIR (/home/creatorrc) is not
# writable by the aria2 user, so it used to fail with a PermissionError on
# every single start - and the fallback below hid that, leaving TOR running on
# stock defaults. Run it somewhere writable instead.
generate_torrc() (
    cd /tmp || exit 1
    rm -f /tmp/tor_config.txt
    # Fetching relay descriptors needs the network and takes a few minutes.
    # Bound it so an unreachable directory authority cannot stop the container
    # from starting at all.
    timeout 300 python /home/creatorrc/creatorrc.py --speetor || exit 1
    [ -s /tmp/tor_config.txt ] || exit 1
    mv -f /tmp/tor_config.txt /conf/torrc
)

# Generating it takes minutes, and the relay list goes stale slowly, so only
# rebuild it once a week. /conf is a bind mount, so it survives restarts.
# Delete /conf/torrc (or run "just clean") to force a fresh one.
if [ -s /conf/torrc ] && [ -z "$(find /conf/torrc -mtime +7)" ]; then
    echo "Using existing /conf/torrc."
    tor --runasdaemon 1 -f /conf/torrc
elif generate_torrc; then
    echo "Generated /conf/torrc, starting TOR with the fastest guards and exits."
    tor --runasdaemon 1 -f /conf/torrc
else
    echo "WARNING: could not generate /conf/torrc, starting TOR with defaults." >&2
    tor --runasdaemon 1
fi

# Regenerated on every start (it's cheap - no network needed) so TORSERVNUM
# actually controls how many parallel circuits v2ray load-balances across.
python /run/generate_v2ray_config.py > /conf/config.json

exec v2ray run -c /conf/config.json &
exec aria2c --conf-path=/conf/aria2.conf --log=/log/aria2_log.txt --rpc-listen-port="${RPCPORT}" --rpc-secret="${RPCSECRET}"
