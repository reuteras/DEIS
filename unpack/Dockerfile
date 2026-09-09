#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM debian:trixie-slim

LABEL maintainer="code@ongoing.today"

COPY install.sh /
COPY start.sh /

ENV DEBIAN_FRONTEND noninteractive

# hadolint ignore=DL3008
RUN apt-get update && \
    apt-get upgrade -yq && \
    apt-get -y install --no-install-recommends \
        ca-certificates \
        file \
        mdbtools \
        pst-utils \
        python3 \
        python3-venv \
        qpdf \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-swe \
        wget \
        xz-utils && \
    /install.sh && \
    python3 -m venv /opt/msoffcrypto-venv && \
    /opt/msoffcrypto-venv/bin/pip install --no-cache-dir "msoffcrypto-tool==6.0.0" && \
    apt-get autoremove -y \
        ca-certificates \
        python3-venv \
        wget \
        xz-utils && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /usr/share/doc && \
    rm -rf /usr/local/share/man /var/cache/debconf/*-old /*.txt /install.sh && \
    useradd -m -u 1000 -s /bin/bash unpack

USER unpack

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD test -f /start.sh || exit 1

CMD ["/start.sh"]
