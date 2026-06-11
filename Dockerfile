# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Install uv from official image — pinned digest recommended for production
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# Copy dependency manifest first for layer caching
COPY pyproject.toml ./

# Install dependencies into the system Python (no venv needed in container)
RUN uv pip install --system --no-cache -r pyproject.toml

# Copy application modules
COPY bot.py schema_mapper.py registry.py signals.py \
     ninja_auth.py db.py poller.py web.py ./
COPY templates/ ./templates/

# Ensure stdout/stderr is unbuffered for clean Docker/journal logs
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
