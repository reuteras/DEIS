#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM alpine:3.24

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

# crond must run as root: it needs privilege to manage /etc/crontabs
# and to execute cron entries, so we do not drop to the deis user here.

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD pgrep crond || exit 1

CMD ["/deis/bin/dcron.sh", "-f", "-L", "/dev/stdout"]
