#!/bin/bash

cd / || exit

[[ -f /status/ingest_done ]] && exit

echo "Wait for extraction to finish."
while [[ ! -f /status/extract_done ]]; do
    sleep 30
done
echo "Run ingest.py."
# Real liveness signal (like /status/running for download.sh) for
# deis.py/web/progress.py to tell "ingest.py is actually running" apart
# from "extract_done exists but no ingest container has picked it up yet".
touch /status/ingesting
exec /app/ingest.py
