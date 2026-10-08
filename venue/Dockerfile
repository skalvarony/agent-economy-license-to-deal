# The venue simulator. Its log of what happened lives in /data.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1

# Built from the repository root (shared/ comes along): see deploy/.
COPY venue/pyproject.toml venue/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY venue/ .
# The team's sign-in, one file shared by the three apps.
COPY shared/teamlogin.py shared/teamlogin.html ./

EXPOSE 8196
CMD ["uv", "run", "--no-sync", "uvicorn", "app:app", "--host", "0.0.0.0", \
     "--port", "8196", "--proxy-headers", "--forwarded-allow-ips", "*"]
