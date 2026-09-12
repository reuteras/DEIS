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

# Check every hand-pinned version against its latest upstream release - spaCy/its
# models, 7-Zip, v2ray-core, the geonamescache snapshot behind bin/cities.tsv (each
# documented in a VENDORED.md), elasticsearch-py/wordcloud pinned in
# notebook/Elastic.ipynb's own first cell, and .env.default's ELASTIC_VERSION. See
# bin/check_vendored.py's own docstring for exactly what this does and doesn't
# cover - Docker image references are Dependabot's job now (.github/dependabot.yml),
# and `just update` above is the separate mechanism for ordinary uv-managed deps.
#
# "|| true": the script itself exits 1 when an update is available (real signal,
# useful for `uv run python3 bin/check_vendored.py list` in a script/CI check),
# but that turns into just's own "error: recipe ... failed" here, which reads as
# something broke rather than "here's what's outdated" - `just` is for interactive/
# manual use, so this recipe always exits 0 and leaves the real exit code to
# whatever calls the script directly.
check-vendored:
    uv run python3 bin/check_vendored.py list || true

# Mechanically apply an available update for one vendored item - fetches the new
# artifact(s), computes/verifies their sha256, and edits the exact pinned line(s);
# never touches a VENDORED.md's own prose, which stays a manual edit (the command's
# own output lists exactly what to change by hand). e.g. `just upgrade-vendored 7-zip`
upgrade-vendored name:
    uv run python3 bin/check_vendored.py upgrade {{ name }}

# "dir/*" alone is a bare glob and, like any shell glob, never matches a
# dotfile - found via a real leftover ".!<pid>!unpack.log" (an SMB
# oplock-break artifact from an interrupted process) that `just clean`
# had been silently leaving behind in logs/ every time. "dir/.[!.]*
# dir/..?*" is the standard portable idiom for "every dotfile except
# literal . and ..". -rf, not -f: the first cut of this fix used -f
# (matching the pre-existing plain lines below, which really were
# files-only), but downloader/data/.torcheck (item 42's TOR preflight
# probe directory, deis/done.sh's own dispose-of-.torcheck comment) is a
# real dotfile *directory* the dotfile-inclusive glob now also matches -
# rm -f fails loudly on a directory ("is a directory") and, because it's
# one rm invocation for the whole glob, aborts the entire recipe (found
# live: `deis reset` failed on this line, so none of dist-clean's own
# deletions - extracted/, files/, status/, docker-clean - ever ran).
# -rf everywhere this recipe empties a directory by glob is the simplest
# fix that's safe regardless of whether a directory shows up in a given
# sweep, now or later - a plain file is removed exactly the same either
# way. rm -rf on a directory *itself* (downloader/log below) already
# recurses into dotfiles with no such gap.
#
# The dotfile-inclusive glob has a second consequence: downloader/data/,
# extracted/, files/, and status/ each carry a real git-tracked
# ".gitignore" (the standard "ignore everything except this file" trick,
# so an otherwise-empty directory still exists after a fresh checkout) -
# found the same way as the .torcheck bug above, live: a first attempt at
# this fix silently deleted downloader/data/.gitignore before erroring
# out on .torcheck. `git checkout --` restores each one immediately after
# its directory's sweep, rather than trying to exclude ".gitignore" from
# the glob itself (bash's default globbing has no clean "everything
# except this one name" syntax without enabling extglob).

# Delete downloader state, log files, and controller's web page
clean:
    rm -f downloader/conf/aria2.session
    rm -f downloader/conf/nginx.conf
    rm -f downloader/conf/privoxy*
    rm -f downloader/conf/torrc
    rm -rf downloader/data/* downloader/data/.[!.]* downloader/data/..?*
    git checkout -- downloader/data/.gitignore
    rm -rf downloader/log
    rm -rf logs/* logs/.[!.]* logs/..?*
    rm -f controller/www/index.html

dist-clean: clean docker-clean
    rm -rf extracted/* extracted/.[!.]* extracted/..?*
    git checkout -- extracted/.gitignore
    rm -rf files/* files/.[!.]* files/..?*
    git checkout -- files/.gitignore
    rm -rf status/* status/.[!.]* status/..?*
    git checkout -- status/.gitignore
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
