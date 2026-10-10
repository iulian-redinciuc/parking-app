# Backend images. Spec: docs/design/deployment.md §2
# Build context = the repo root:  docker build -f backend/Dockerfile --target api .
#   api:    FastAPI + SQLite, core deps only (no PyTorch)
#   vision: camera workers with the ML stack (VISION_EXTRAS, e.g. "vision,openvino" on Intel)
FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN pip install uv

FROM base AS api
# dependencies first (cached layer), then the code and the project itself
RUN cd backend && uv sync --frozen --no-dev --no-install-project
COPY backend/ ./backend/
RUN cd backend && uv sync --frozen --no-dev
USER 1000:1000
CMD ["/app/backend/.venv/bin/parking", "api", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS vision
ARG VISION_EXTRAS=vision
RUN cd backend && uv sync --frozen --no-dev --no-install-project \
    $(echo ",${VISION_EXTRAS}" | sed 's/,/ --extra /g')
COPY backend/ ./backend/
RUN cd backend && uv sync --frozen --no-dev $(echo ",${VISION_EXTRAS}" | sed 's/,/ --extra /g')
# Ultralytics writes its settings under $HOME; the container user has no home of its own
ENV HOME=/tmp YOLO_CONFIG_DIR=/tmp/Ultralytics
# the worker loop's heartbeat, read by the healthcheck in the Compose files (deployment.md §4.2)
ENV HEARTBEAT_FILE=/tmp/heartbeat
USER 1000:1000
ENTRYPOINT ["/app/backend/.venv/bin/parking", "worker"]
