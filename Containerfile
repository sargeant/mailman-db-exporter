FROM ghcr.io/astral-sh/uv:python3.12-alpine

COPY mailman-exporter.py /app/mailman-exporter.py

ENV UV_CACHE_DIR=/tmp/uv-cache

# pre-cache libs needed by script
RUN uv run --script /app/mailman-exporter.py --help

USER nobody

EXPOSE 9934

ENTRYPOINT ["uv", "run", "--script", "/app/mailman-exporter.py"]
