#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
ARG ELASTIC_VERSION

# https://www.docker.elastic.co/
FROM docker.elastic.co/enterprise-search/enterprise-search:${ELASTIC_VERSION}

USER enterprise-search

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:3002/api/version || exit 1
