#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
ARG ELASTIC_VERSION

FROM docker.elastic.co/beats/elastic-agent:${ELASTIC_VERSION}

# Ensure the 'state' directory exists and is owned by the 'elastic-agent' user,
# otherwise mounting a named volume in that location creates a directory owned
# by root:root which the 'elastic-agent' user isn't allowed to write to.
RUN mkdir state

USER elastic-agent

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD elastic-agent status || exit 1
