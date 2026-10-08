# One shop. Which one is decided at run time: SHOP=a|b|c picks the catalogue
# under /shops and the database under /data. See deploy/docker-compose.yml.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1

# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN chmod +x docker-entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["./docker-entrypoint.sh"]
