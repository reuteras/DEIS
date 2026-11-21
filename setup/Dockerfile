#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
# The Elastic Stack version lives here (tag + digest). Dependabot bumps
# elasticsearch/, kibana/ and setup/ together in one grouped PR (see
# .github/dependabot.yml), so the three always run the same version.
# https://www.docker.elastic.co/
FROM docker.elastic.co/elasticsearch/elasticsearch:9.5.6@sha256:79b4f1de4c15e6eca824723086a51e5934406d614d410cc11be77748cafac537

# Unlike entrypoint.sh/lib.sh (bind-mounted read-only in docker-compose.yml,
# so an edit takes effect on the next 'deis setup' with no rebuild needed),
# this file is baked into the image - a change here needs
# 'docker compose build setup' before it actually takes effect. Found the
# hard way: an edited export.ndjson silently kept importing the old,
# pre-edit saved objects until the image was rebuilt.
COPY export.ndjson /export.ndjson

USER 1000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["/bin/sh", "-c", "curl -f http://localhost:9200/_cluster/health || exit 1"]

ENTRYPOINT ["/entrypoint.sh"]
