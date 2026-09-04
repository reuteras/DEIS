#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM python:3.14-slim

WORKDIR /app

COPY *.py /app
COPY *.sh /app
COPY requirements.txt /app

# hadolint ignore=DL3008
RUN apt-get update && \
    apt-get upgrade -yq && \
    apt-get -y install --no-install-recommends \
        curl \
        file \
        libmagic1 && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /usr/share/doc && \
    rm -rf /usr/local/share/man /var/cache/debconf/*-old && \
    useradd -m -u 1000 -s /bin/bash web && \
    mkdir -p /extracted/sha256 && \
    chown web:web /extracted/sha256

USER web

EXPOSE 8081

ENV UVICORN_LOG_LEVEL=info

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8081/openapi.json || exit 1

CMD ["./startup.sh"]
