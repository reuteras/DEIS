#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM python:3.11-slim

WORKDIR /app

COPY *.py /app
COPY *.sh /app
COPY requirements.txt /app

RUN apt-get update && \
    apt-get upgrade -yq && \
    apt-get -y install --no-install-recommends \
        file \
        libmagic1 && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /usr/share/doc && \
    rm -rf /usr/local/share/man /var/cache/debconf/*-old

EXPOSE 8081

ENV UVICORN_LOG_LEVEL=info

CMD ["./startup.sh"]
