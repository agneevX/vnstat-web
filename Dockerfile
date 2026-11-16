FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUID=10001 \
    PGID=10001 \
    TZ=UTC

WORKDIR /app
RUN apk add --no-cache su-exec tzdata && mkdir /config
COPY server.py index.html entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

EXPOSE 8080
VOLUME ["/config"]

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "/app/server.py", "--config", "/config/config.toml"]
