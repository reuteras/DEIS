#!/bin/bash

[[ ! -e /status/extract_done ]] && echo "Waiting for files."
while [[ ! -e /status/extract_done ]]; do
    sleep 5
done

# Finally, run the FastAPI app
cd /app || exit
echo "Run app.py"
exec uvicorn app:app --host 0.0.0.0 --port 8081
