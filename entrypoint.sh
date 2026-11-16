#!/bin/sh
set -eu

: "${PUID:=10001}"
: "${PGID:=10001}"
: "${TZ:=UTC}"
export PUID PGID TZ

case "$PUID" in
    ''|*[!0-9]*)
        echo "PUID must be a numeric user ID" >&2
        exit 1
        ;;
esac
case "$PGID" in
    ''|*[!0-9]*)
        echo "PGID must be a numeric group ID" >&2
        exit 1
        ;;
esac

chown -R "$PUID:$PGID" /config
exec su-exec "$PUID:$PGID" "$@"
