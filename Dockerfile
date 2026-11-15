FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --chown=10001:10001 server.py index.html ./
RUN mkdir -p /config && chown 10001:10001 /config

USER 10001:10001
EXPOSE 8080
VOLUME ["/config"]

CMD ["python", "/app/server.py", "--config", "/config/config.toml"]
