#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf

RUN mkdir -p /deis/bin

COPY *.sh /deis/bin/
COPY crawl_links.py /deis/bin/
COPY crontab.txt /deis/

# hadolint ignore=DL3018
RUN apk upgrade --no-cache && \
    apk add --no-cache \
        bash \
        curl \
        dcron \
        jq \
        python3 && \
    /usr/bin/crontab /deis/crontab.txt

ENV OWNER_UID=1000
ENV OWNER_GID=1000

RUN addgroup -g 1000 -S deis && adduser -u 1000 -S deis -G deis

# crond must run as root: it needs privilege to manage /etc/crontabs
# and to execute cron entries, so we do not drop to the deis user here.

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["/bin/sh", "-c", "pgrep crond || exit 1"]

CMD ["/deis/bin/dcron.sh", "-f", "-L", "/dev/stdout"]
