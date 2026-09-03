#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
#checkov:skip=CKV_DOCKER_7
#checkov:skip=CKV_DOCKER_8
# hadolint ignore=DL3007
FROM alpine:3.24

LABEL maintainer="sn0b4ll"

# hadolint ignore=DL3018
RUN addgroup -g 1000 -S nginx && adduser -u 1000 -S nginx -G nginx && \
    apk add --update --no-cache bash nginx

# RUN wget https://github.com/mayswind/AriaNg-DailyBuild/archive/master.zip

COPY --chmod=755 docker-entrypoint.sh /docker-entrypoint.sh

USER nginx

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080 || exit 1

CMD ["/docker-entrypoint.sh"]

