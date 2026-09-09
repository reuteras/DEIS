#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
# Default is only a fallback for a bare `docker build` outside compose -
# docker-compose.yml always passes the real value from .env as a build arg.
ARG ELASTIC_VERSION=9.5.3

# https://www.docker.elastic.co/
FROM docker.elastic.co/elasticsearch/elasticsearch:${ELASTIC_VERSION}

# Unlike entrypoint.sh/lib.sh (bind-mounted read-only in docker-compose.yml,
# so an edit takes effect on the next 'deis setup' with no rebuild needed),
# this file is baked into the image - a change here needs
# 'docker compose build setup' before it actually takes effect. Found the
# hard way: an edited export.ndjson silently kept importing the old,
# pre-edit saved objects until the image was rebuilt.
COPY export.ndjson /export.ndjson

USER elasticsearch

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:9200/_cluster/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
