set shell := ["bash", "-uc"]

virtualenv := ".venv"

default: venv

# Update all dependencies to latest versions and re-export pinned requirements.txt
update:
    uv lock --upgrade
    just export

# Re-export pinned requirements.txt for all components without upgrading
export:
    uv export --package deis-bin    --no-hashes --no-dev --no-emit-workspace --quiet > bin/requirements.txt
    uv export --package deis-ingest --no-hashes --no-dev --no-emit-workspace --quiet > ingest/requirements.txt
    uv export --package deis-web    --no-hashes --no-dev --no-emit-workspace --quiet > web/requirements.txt

# "dir/*" alone is a bare glob and, like any shell glob, never matches a
# dotfile - found via a real leftover ".!<pid>!unpack.log" (an SMB
# oplock-break artifact from an interrupted process) that `just clean`
# had been silently leaving behind in logs/ every time. "dir/.[!.]*
# dir/..?*" is the standard portable idiom for "every dotfile except
# literal . and .." - added alongside the plain glob everywhere this
# recipe empties a directory by glob rather than removing it outright
# (rm -rf on the directory itself, like downloader/log below, already
# recurses into dotfiles with no such gap).

# Delete downloader state, log files, and controller's web page
clean:
    rm -f downloader/conf/aria2.session
    rm -f downloader/conf/nginx.conf
    rm -f downloader/conf/privoxy*
    rm -f downloader/conf/torrc
    rm -f downloader/data/* downloader/data/.[!.]* downloader/data/..?*
    rm -rf downloader/log
    rm -f logs/* logs/.[!.]* logs/..?*
    rm -f controller/www/index.html

dist-clean: clean docker-clean
    rm -rf extracted/* extracted/.[!.]* extracted/..?*
    rm -rf files/* files/.[!.]* files/..?*
    rm -rf status/* status/.[!.]* status/..?*
    rm -f .jupyter/serverconfig/jupyterlabapputilsextensionannouncements.json
    rm -rf .jupyter/lab/workspaces/* .jupyter/lab/workspaces/.[!.]* .jupyter/lab/workspaces/..?* .jupyter/migrated
    rm -rf notebook/.ipynb_checkpoints
    rm -rf .venv
    # docker-clean stops aria2 gracefully, which flushes a fresh (empty)
    # session file on exit - remove it again now that it's actually down.
    rm -f downloader/conf/aria2.session

docker-clean:
    docker compose --profile deis down || true
    docker compose --profile deis rm || true
    docker rm deis-setup-1 || true
    docker volume rm deis_elasticsearch || true
    docker images -a | grep -E '^deis-' | cut -f1 -d\  | xargs docker rmi || true

docker-clean-all:
    docker rmi reuteras/container-notebook || true
    docker rmi gotenberg/gotenberg || true

docker-stop-download:
    docker stop deis-downloader-1 deis-controller-1 || true

docker-remove-prepare-containers:
    docker rm deis-unpack-1 deis-downloader-1 deis-deis-1 deis-setup-1 deis-ingest-1 deis-controller-1 || true

# Creates the venv itself (mirrors the old $(virtualenv) file-target).
_venv-dir:
    test -d {{virtualenv}} || uv sync --dev
    source {{virtualenv}}/bin/activate && python3 -m pip -q install -U pip

venv: _venv-dir requires

ingest: _venv-dir
    source {{virtualenv}}/bin/activate && python3 -m pip -q install -r ingest/requirements.txt

python-bin: _venv-dir
    source {{virtualenv}}/bin/activate && python3 -m pip -q install -r bin/requirements.txt

python-bin-arm64: _venv-dir
    source {{virtualenv}}/bin/activate && CPPFLAGS="-I/opt/homebrew/opt/sqlite/include" python3 -m pip -q install -r bin/requirements.txt

requires: _venv-dir
    source {{virtualenv}}/bin/activate && python3 -m pip -q install -r bin/requirements.txt
    source {{virtualenv}}/bin/activate && python3 -m pip -q install -r ingest/requirements.txt
    source {{virtualenv}}/bin/activate && python3 -m pip -q install -r web/requirements.txt

progress: python-bin
    {{virtualenv}}/bin/python3 bin/progress.py

test: _venv-dir
    uv run ruff check .
    uv run ruff format --check .
    uv run pytest
