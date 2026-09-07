#!/bin/bash

# Starts immediately rather than waiting for /status/extract_done: the
# landing page ("/") exists precisely to show pipeline progress from the
# very start, and every file-serving route already 404s cleanly on content
# that doesn't exist yet.
cd /app || exit
echo "Run app.py"
exec uvicorn app:app --host 0.0.0.0 --port 8081
