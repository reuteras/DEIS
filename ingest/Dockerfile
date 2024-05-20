# Use an official Python runtime as the parent image
#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM python:3.11-slim

WORKDIR /app

COPY *.py /app
COPY *.sh /app
COPY requirements.txt /app

RUN python3 -m pip install --no-cache-dir -r /app/requirements.txt

CMD ["/app/start.sh"]
