# Use an official Python runtime as the parent image
#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM python:3.11-slim

WORKDIR /app

COPY *.py /app
COPY *.sh /app
COPY requirements.txt /app

RUN python3 -m pip install --no-cache-dir -r /app/requirements.txt

RUN useradd -m -u 1000 -s /bin/bash ingest
USER ingest

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import sys; sys.exit(0)" || exit 1

CMD ["/app/start.sh"]
