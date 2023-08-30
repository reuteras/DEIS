#!/bin/bash

cd / || exit

[[ -f /extracted/ingest_done ]] && exit

echo "Wait for extraction to finish."
while [[ ! -f /extracted/files/done ]]; do
    sleep 30
done
echo "Run ingest.py."
exec /app/ingest.py
