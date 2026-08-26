# MCP server — production image. Always served over HTTP so an agent can connect
# to it as a separate service; stdio is a dev-only transport and needs no container.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (layer cache).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# Run as a non-root user.
RUN useradd -m appuser
USER appuser

# Bind to every interface — 127.0.0.1 (the .env default) is unreachable from
# another container. Override MCP_PORT if the platform injects its own port.
ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

EXPOSE 8000

# Endpoint: http://<host>:<port>/mcp
CMD ["python", "-m", "app"]
