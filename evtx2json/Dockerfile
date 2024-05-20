#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
FROM rust:1-bookworm

RUN cargo install evtx

CMD ["evtx_dump -h"]
