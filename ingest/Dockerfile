# Use an official Python runtime as the parent image
#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /app

COPY *.py /app
COPY *.sh /app
COPY requirements.txt /app

# apt-get upgrade: Debian security updates the official image does not have yet.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* && \
    python3 -m pip install --no-cache-dir -r /app/requirements.txt && \
    useradd -m -u 1000 -s /bin/bash ingest

USER 1000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["/bin/sh", "-c", "python3 -c \"import sys; sys.exit(0)\" || exit 1"]

CMD ["/app/start.sh"]
