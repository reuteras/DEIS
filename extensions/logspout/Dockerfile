#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
# uses ONBUILD instructions described here:
# https://github.com/gliderlabs/logspout/tree/master/custom

FROM gliderlabs/logspout:master
ENV SYSLOG_FORMAT rfc3164

RUN addgroup -g 1000 -S logspout && adduser -u 1000 -S logspout -G logspout

USER logspout

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD test -f /var/run/docker.sock || exit 1
