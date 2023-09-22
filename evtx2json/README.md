# evtx2json

Build

```bash
docker build --tag evtx2json .
```

Run

```bash
docker run --rm -v ./evtx:/evtx evtx2json evtx_dump --format json /evtx/file.evtx > json/file.json
```

