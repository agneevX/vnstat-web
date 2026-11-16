# vnstat-web
Web viewer for vnstat

## Docker

Pull and run the multi-architecture image:

```sh
docker pull ghcr.io/agneevx/vnstat-web:latest
docker run -d --name vnstat-web \
  -p 8080:8080 \
  -e PUID=1000 -e PGID=1000 -e TZ=America/New_York \
  -v vnstat-web-config:/config \
  -v /path/to/vnstat.db:/data/vnstat.db:ro \
  ghcr.io/agneevx/vnstat-web:latest
```

Open http://localhost:8080. The `/config` volume stores `config.toml`; configure
the database path in Settings if it differs from `/data/vnstat.db`. `PUID` and
`PGID` set the user and group IDs used by the app and for `/config` ownership at
startup (both default to `10001`). `TZ` sets the container timezone and defaults
to `UTC`. The image supports `linux/amd64` and `linux/arm64`. GitHub Actions
publishes `latest` from the default branch and version tags from `v*` tags.
