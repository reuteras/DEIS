#!/bin/bash

[[ ! -e /extracted/files/done ]] && echo "Waiting for files."
while [[ ! -e /extracted/files/done ]]; do
    sleep 5
done

if [[ $(find /extracted/sha256/ -type l | wc -l | awk '{print $1}') == "0" ]]; then
    echo "Running pre-start tasks..."
    cd /extracted/sha256 || exit
    while IFS= read -r -d '' file; do
        sha=$(sha256sum "${file}" | awk '{print $1}')
        if [[ ! -e "${sha}" ]]; then
            ln -s "${file}" "${sha}"
        fi
    done < <(find /extracted/files -type f -print0)
    echo "Pre-start tasks finished. Starting FastAPI..."
fi

# Finally, run the FastAPI app
cd /app || exit
echo "Run app.py"
exec uvicorn app:app --host 0.0.0.0 --port 8081
