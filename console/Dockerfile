# The shops' console. It keeps nothing: everything comes from the shops.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1

# Built from the repository root (shared/ comes along): see deploy/.
COPY console/pyproject.toml console/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY console/ .
# The team's sign-in, one file shared by the three apps.
COPY shared/teamlogin.py shared/teamlogin.html ./

EXPOSE 8195
CMD ["uv", "run", "--no-sync", "uvicorn", "app:app", "--host", "0.0.0.0", \
     "--port", "8195", "--proxy-headers", "--forwarded-allow-ips", "*"]
