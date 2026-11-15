FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --chown=10001:10001 server.py index.html ./
RUN mkdir -p /data && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]

CMD ["python", "/app/server.py", "--host", "0.0.0.0", "--config", "/data/config.toml"]
