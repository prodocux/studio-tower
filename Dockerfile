FROM grafana/mcp-grafana:latest AS grafana-mcp

FROM python:3.12-slim as base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Official grafana/mcp-grafana binary (hackathon Grafana track MCP server)
COPY --from=grafana-mcp /app/mcp-grafana /usr/local/bin/mcp-grafana
RUN chmod 0755 /usr/local/bin/mcp-grafana

# Install dependencies
COPY pyproject.toml ./
COPY README.md ./
COPY backend/ ./backend/
COPY scripts/docker-start.sh /app/docker-start.sh

RUN pip install --no-cache-dir . \
    && chmod 0755 /app/docker-start.sh

# Create non-root user for security
RUN groupadd -r studiotower && useradd -r -g studiotower -d /app studiotower && \
    mkdir -p /app/data && chown -R studiotower:studiotower /app

USER studiotower

EXPOSE 8080

CMD ["/app/docker-start.sh"]
