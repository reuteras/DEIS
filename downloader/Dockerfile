#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
#checkov:skip=CKV_DOCKER_7
FROM alpine:3.18

LABEL maintainer="sn0b4ll"

WORKDIR /home

COPY *.patch ./
COPY --chmod=777 install-release.sh ./

# hadolint ignore=DL3018
RUN apk add --update --no-cache bash tor tzdata vim autoconf build-base git libgcc gmp libtool gnutls gnutls-dev nettle libssh2 libssh2-dev ca-certificates c-ares c-ares-dev libxml2 libxml2-dev sqlite-libs sqlite-dev automake cppunit cppunit-dev util-macros gettext gettext-dev zlib zlib-dev curl unzip \
&& ./install-release.sh && git clone https://github.com/aria2/aria2.git ./aria2 && patch ./aria2/src/HttpSkipResponseCommand.cc < ./500retry.patch && patch ./aria2/src/DownloadCommand.cc < ./slowretry.patch && patch ./aria2/src/OptionHandlerFactory.cc < ./removeconnlimit.patch && patch ./aria2/src/SocketBuffer.cc < ./connclosed.patch \
&& rm -f ./500retry.patch ./slowretry.patch ./removeconnlimit.patch ./connclosed.patch ./install-release.sh \
&& cd ./aria2 && autoreconf -i && ./configure && make -j"$(nproc)" && make install && cd .. && rm -rf ./aria2 \
&& apk del autoconf build-base git libtool gnutls-dev libssh2-dev c-ares-dev libxml2-dev sqlite-dev automake cppunit-dev util-macros gettext-dev zlib-dev curl unzip \
&& apk add --update --no-cache python3 py3-stem && mkdir -p creatorrc && cd creatorrc && aria2c https://raw.githubusercontent.com/hephaest0s/creatorrc/master/creatorrc.py \
&& aria2c https://raw.githubusercontent.com/hephaest0s/creatorrc/master/guard_country_resolver.py

RUN addgroup -g 1000 -S aria2 && adduser -u 1000 -S aria2 -G aria2
USER aria2

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD aria2c --version || exit 1

CMD ["/run/run_aria2.sh"]

