#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
# The Elastic Stack version lives here (tag + digest). Dependabot bumps
# elasticsearch/, kibana/ and setup/ together in one grouped PR (see
# .github/dependabot.yml), so the three always run the same version.
# https://www.docker.elastic.co/
FROM docker.elastic.co/kibana/kibana:9.5.6@sha256:c7ed45da5c3cfea020fc6c99ce027f4d21d19dab330379c5115d5eb95f6ecebc

# Add your kibana plugins setup here
# Example: RUN kibana-plugin install <name|url>

USER 1000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["/bin/sh", "-c", "curl -f http://localhost:5601/api/status || exit 1"]
