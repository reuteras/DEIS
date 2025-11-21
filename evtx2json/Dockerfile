#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM rust:1-bookworm

RUN cargo install evtx && \
    useradd -m -u 1000 -s /bin/bash evtx

USER evtx

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD evtx_dump -h || exit 1

CMD ["evtx_dump -h"]
