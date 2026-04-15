# Update all dependencies to latest versions and re-export pinned requirements.txt
update:
    uv lock --upgrade
    just export

# Re-export pinned requirements.txt for all components without upgrading
export:
    uv export --package deis-bin    --no-hashes --no-dev --no-emit-workspace --quiet > bin/requirements.txt
    uv export --package deis-ingest --no-hashes --no-dev --no-emit-workspace --quiet > ingest/requirements.txt
    uv export --package deis-web    --no-hashes --no-dev --no-emit-workspace --quiet > web/requirements.txt
