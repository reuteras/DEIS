# Update all dependencies to latest versions and re-export pinned requirements.txt
update:
    uv lock --upgrade
    just export

# Re-export pinned requirements.txt for all components without upgrading
export:
    uv export --package deis-bin    --no-hashes --no-dev --no-emit-workspace > bin/requirements.txt
    uv export --package deis-ingest --no-hashes --no-dev --no-emit-workspace > ingest/requirements.txt
    uv export --package deis-web    --no-hashes --no-dev --no-emit-workspace > web/requirements.txt
