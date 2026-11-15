# vnstat-web
Web viewer for vnstat

## Docker

Pull and run the multi-architecture image:

```sh
docker pull ghcr.io/agneevx/vnstat-web:latest
docker run -d --name vnstat-web \
  -p 8080:8080 \
  -v vnstat-web-data:/data \
  -v /path/to/vnstat.db:/data/vnstat.db:ro \
  ghcr.io/agneevx/vnstat-web:latest
```

Open http://localhost:8080. The `/data` volume stores `config.toml`; configure
the database path in Settings if it differs from `/data/vnstat.db`. The image
supports `linux/amd64` and `linux/arm64`. GitHub Actions publishes `latest` from
the default branch and version tags from `v*` tags.
