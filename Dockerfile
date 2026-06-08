FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app


COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py template.html README.md ./
COPY assets/ ./assets/

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

# Run uvicorn directly so reload=True (dev-only) in __main__ is bypassed
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
