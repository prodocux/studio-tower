#!/bin/sh
# Cloud Run entrypoint: official grafana/mcp-grafana sidecar + FastAPI.
# Hosted mcp.grafana.com is OAuth-browser only; Cloud Run uses grafana/mcp-grafana
# against GRAFANA_BASE_URL with a Grafana service account token.

TOKEN="${GRAFANA_SERVICE_ACCOUNT_TOKEN:-${GRAFANA_CLOUD_API_KEY:-}}"
if [ -n "$TOKEN" ] && [ -n "${GRAFANA_BASE_URL:-}" ] && [ -x /usr/local/bin/mcp-grafana ]; then
  export GRAFANA_URL="$GRAFANA_BASE_URL"
  if [ -z "${GRAFANA_SERVICE_ACCOUNT_TOKEN:-}" ]; then
    export GRAFANA_SERVICE_ACCOUNT_TOKEN="$TOKEN"
  fi
  export GRAFANA_MCP_ENDPOINT="http://127.0.0.1:8000/mcp"
  /usr/local/bin/mcp-grafana \
    -t streamable-http \
    --address 127.0.0.1:8000 \
    --log-level warn &
fi

exec uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port "${PORT:-8080}"
