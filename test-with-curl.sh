#!/usr/bin/env -S bash

set -ueo pipefail


PORT="${PORT:-8000}"
URL="http://localhost:${PORT}/mcp"

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"demo","version":"0.0.1"}}}'

NOTIFY='{"jsonrpc": "2.0","method": "notifications/initialized"}'
LIST='{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
CALL='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"say_hello","arguments":{}}}'


HEADERS=(
    -H "Content-Type: application/json; charset=utf-8"
    -H "Accept: application/json, application/x-ndjson, text/event-stream"
)


curl -v -N "$URL" "${HEADERS[@]}" -d "$INIT" -D /tmp/headers.txt
SESSION_ID=$(grep -i "mcp-session-id" /tmp/headers.txt | cut -d' ' -f2 | tr -d '\r')
HEADERS+=(-H "Mcp-Session-Id: $SESSION_ID")

curl -N "$URL" "${HEADERS[@]}" -d "$NOTIFY"
printf "\n---\n"
curl -N "$URL" "${HEADERS[@]}" -d "$LIST"
printf "\n---\n"
curl -N "$URL" "${HEADERS[@]}" -d "$CALL"
