FROM ghcr.io/astral-sh/uv:0.11.19 AS uv
FROM python:3.13-slim
COPY --from=uv /uv /uvx /bin/
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY gilm ./gilm
RUN useradd --create-home --uid 10001 gilm && mkdir /app/data && chown -R gilm:gilm /app
USER gilm
ENV GILM_DEV_MODE=false GILM_HOST=0.0.0.0 GILM_DATA_DIR=/app/data
EXPOSE 8000
CMD ["uv", "run", "--locked", "--no-dev", "--no-sync", "python", "-m", "gilm", "serve"]
