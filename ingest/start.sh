#!/bin/bash

cd / || exit

[[ -f /status/ingest_done ]] && exit

echo "Wait for extraction to finish."
while [[ ! -f /status/extract_done ]]; do
    sleep 30
done
echo "Run ingest.py."
exec /app/ingest.py
