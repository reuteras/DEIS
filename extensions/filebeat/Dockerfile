#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
ARG ELASTIC_VERSION

FROM docker.elastic.co/beats/filebeat:${ELASTIC_VERSION}

USER root

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD filebeat test output || exit 1

USER filebeat
