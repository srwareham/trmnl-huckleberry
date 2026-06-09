FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app


COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py template.html README.md ./
COPY assets/ ./assets/

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

# Default: push-only service. Pass --run-webserver to also start the local web UI.
ENTRYPOINT ["python", "main.py"]
