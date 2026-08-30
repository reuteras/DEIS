# controller

Dockerized [AriaNg](https://github.com/mayswind/AriaNg) — a static web frontend for controlling an [aria2](https://aria2.github.io/) download daemon (the `www/` files come from an AriaNg release build).

## Usage

```bash
docker build -t deis-controller .
docker run -p 8080:8080 deis-controller
```

Serves the AriaNg UI on port 8080 via nginx (`conf/nginx.conf`). Point it at your aria2 RPC endpoint from the AriaNg settings page once it's loaded.
