FROM debian:12.1-slim

LABEL maintainer="code@ongoing.today"

COPY install.sh /
COPY start.sh /

ENV DEBIAN_FRONTEND noninteractive

RUN apt-get update && \
    apt-get upgrade -yq && \
    apt-get -y install --no-install-recommends \
        ca-certificates \
        file \
        pst-utils \
        wget \
        xz-utils && \
    /install.sh && \
    apt-get autoremove -y \
        ca-certificates \
        wget \
        xz-utils && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /usr/share/doc && \
    rm -rf /usr/local/share/man /var/cache/debconf/*-old /*.txt /install.sh

CMD ["/start.sh"]
