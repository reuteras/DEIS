#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM python:3.11-slim

WORKDIR /app

COPY *.py /app
COPY *.sh /app
COPY requirements.txt /app

# hadolint ignore=DL3008
RUN apt-get update && \
    apt-get upgrade -yq && \
    apt-get -y install --no-install-recommends \
        file \
        libmagic1 && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /usr/share/doc && \
    rm -rf /usr/local/share/man /var/cache/debconf/*-old && \
    useradd -m -u 1000 -s /bin/bash web

USER web

EXPOSE 8081

ENV UVICORN_LOG_LEVEL=info

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8081/ || exit 1

CMD ["./startup.sh"]
