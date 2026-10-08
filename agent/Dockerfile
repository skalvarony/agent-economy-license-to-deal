# The customer's agent. Its key and its record of approvals live in /data.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    AGENT_RUN_DIR=/data PYTHONUNBUFFERED=1

# Built from the repository root (shared/ comes along): see deploy/.
COPY agent/pyproject.toml agent/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY agent/ .
# The team's sign-in, one file shared by the three apps.
COPY shared/teamlogin.py shared/teamlogin.html ./

EXPOSE 8190
# Behind Caddy, the forwarded headers carry the real scheme and host.
CMD ["uv", "run", "--no-sync", "uvicorn", "app:app", "--host", "0.0.0.0", \
     "--port", "8190", "--proxy-headers", "--forwarded-allow-ips", "*"]
