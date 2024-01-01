SHELL := /bin/bash

virtualenv = .venv

all: venv

clean:
	rm -f downloader/conf/aria2.session
	rm -f downloader/conf/nginx.conf
	rm -f downloader/conf/privoxy*
	rm -f downloader/conf/torrc
	rm -f downloader/data/*
	rm -rf downloader/log
	rm -f logs/*

dist-clean: clean docker-clean
	rm -rf extracted/* files/*
	rm -f .jupyter/serverconfig/jupyterlabapputilsextensionannouncements.json
	rm -rf .jupyter/lab/workspaces/* .jupyter/migrated
	rm -rf notebook/.ipynb_checkpoints
	rm -rf .venv

docker-clean:
	docker compose --profile deis down || true
	docker compose --profile deis rm || true
	docker rm deis-setup-1 || true
	docker volume rm deis_elasticsearch || true
	docker volume rm deis_shasum || true
	docker images -a | grep -E '^deis-' | cut -f1 -d\  | xargs docker rmi || true

docker-clean-all:
	docker rmi reuteras/container-notebook || true
	docker rmi gotenberg/gotenberg || true

docker-stop-download:
	docker stop deis-downloader-1 deis-controller-1 || true

docker-remove-prepare-containers:
	docker rm deis-unpack-1 deis-downloader-1 deis-deis-1 deis-setup-1 deis-ingest-1 deis-controller-1 || true

venv: $(virtualenv) requires

$(virtualenv):
	test -d $(virtualenv) || python3 -m venv $(virtualenv)
	source $(virtualenv)/bin/activate && python3 -m pip -q install -U pip

ingest: $(virtualenv)
	source $(virtualenv)/bin/activate && python3 -m pip -q install -r ingest/requirements.txt

python-bin: $(virtualenv)
	source $(virtualenv)/bin/activate && python3 -m pip -q install -r bin/requirements.txt

python-bin-arm64: $(virtualenv)
	source $(virtualenv)/bin/activate && CPPFLAGS="-I/opt/homebrew/opt/sqlite/include" CPPFLAGS="-I/opt/homebrew/opt/sqlite/include" python3 -m pip -q install -r bin/requirements.txt

requires: $(virtualenv)
	source $(virtualenv)/bin/activate && python3 -m pip -q install -r bin/requirements.txt
	source $(virtualenv)/bin/activate && python3 -m pip -q install -r ingest/requirements.txt
	source $(virtualenv)/bin/activate && python3 -m pip -q install -r web/requirements.txt

progress: python-bin
	$(virtualenv)/bin/python3 bin/progress.py
