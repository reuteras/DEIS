#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM alpine:3.18

RUN mkdir -p /deis/bin

COPY *.sh /deis/bin/
COPY crontab.txt /deis/

# hadolint ignore=DL3018
RUN apk add --no-cache \
        bash \
        curl \
        dcron \
        jq && \
    /usr/bin/crontab /deis/crontab.txt

ENV OWNER_UID=1000
ENV OWNER_GID=1000

RUN addgroup -g 1000 -S deis && adduser -u 1000 -S deis -G deis

USER deis

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9999/ || exit 1

CMD ["/deis/bin/dcron.sh", "-f", "-L", "/dev/stdout"]
