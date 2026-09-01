FROM python:3.13-slim

# uv, straight from its published image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Dependency layer first: only the lockfiles, so this (slow) layer is reused
# on every build where the dependencies haven't changed. --no-install-project
# skips the app itself; --no-dev drops the test/eval group.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Application layer: copied after deps so editing code doesn't bust the cache.
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# main.py's `if __name__ == "__main__": uvicorn.run(app, host="0.0.0.0",
# port=int(os.environ.get("PORT", 8080)))` starts the server — this image
# never invokes uvicorn directly. Run as a module (`-m src.main`, not
# `src/main.py`) so main.py's relative imports (`from .agent import ...`)
# resolve.
CMD ["uv", "run", "python", "-m", "src.main"]
