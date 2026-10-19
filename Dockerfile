FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md THIRD_PARTY_NOTICES.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY gateway ./gateway
COPY scripts ./scripts
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
