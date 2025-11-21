#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
ARG ELASTIC_VERSION

# https://www.docker.elastic.co/
FROM docker.elastic.co/elasticsearch/elasticsearch:${ELASTIC_VERSION}

COPY export.ndjson /export.ndjson

USER elasticsearch

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:9200/_cluster/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
