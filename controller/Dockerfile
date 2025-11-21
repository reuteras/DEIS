#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
#checkov:skip=CKV_DOCKER_7
#checkov:skip=CKV_DOCKER_8
# hadolint ignore=DL3007
FROM alpine:3.18

LABEL maintainer="sn0b4ll"

# hadolint ignore=DL3018
RUN apk add --update --no-cache bash nginx && \
    addgroup -g 1000 -S nginx && adduser -u 1000 -S nginx -G nginx

# RUN wget https://github.com/mayswind/AriaNg-DailyBuild/archive/master.zip

USER nginx

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://localhost || exit 1

CMD ["nginx", "-c", "/conf/nginx.conf", "-g", "daemon off;"]

