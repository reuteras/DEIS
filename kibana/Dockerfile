#checkov:skip=CKV_DOCKER_2
#checkov:skip=CKV_DOCKER_3
# The Elastic Stack version lives here (tag + digest). Dependabot bumps
# elasticsearch/, kibana/ and setup/ together in one grouped PR (see
# .github/dependabot.yml), so the three always run the same version.
# https://www.docker.elastic.co/
FROM docker.elastic.co/kibana/kibana:9.5.7@sha256:e34ecf8da9b5d858cbdff21a9049b4f21ee99e1039933b20c4fbdf8b500eabf5

# Add your kibana plugins setup here
# Example: RUN kibana-plugin install <name|url>

USER 1000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["/bin/sh", "-c", "curl -f http://localhost:5601/api/status || exit 1"]
