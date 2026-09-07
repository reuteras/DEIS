#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
# Default is only a fallback for a bare `docker build` outside compose -
# docker-compose.yml always passes the real value from .env as a build arg.
ARG ELASTIC_VERSION=9.5.3

# https://www.docker.elastic.co/
FROM docker.elastic.co/elasticsearch/elasticsearch:${ELASTIC_VERSION}

COPY export.ndjson /export.ndjson

USER elasticsearch

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:9200/_cluster/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
