#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
#checkov:skip=CKV_DOCKER_7
#checkov:skip=CKV_DOCKER_8
# hadolint ignore=DL3007
FROM alpine:latest

LABEL maintainer="sn0b4ll"

# hadolint ignore=DL3018
RUN apk add --update --no-cache bash nginx

# RUN wget https://github.com/mayswind/AriaNg-DailyBuild/archive/master.zip

CMD ["nginx", "-c", "/conf/nginx.conf", "-g", "daemon off;"]

