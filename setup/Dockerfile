#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
ARG ELASTIC_VERSION

# https://www.docker.elastic.co/
FROM docker.elastic.co/elasticsearch/elasticsearch:${ELASTIC_VERSION}

COPY export.ndjson /export.ndjson

ENTRYPOINT ["/entrypoint.sh"]
