# Two stages, because the dashboard is a build artefact and `web/dist` is deliberately not in
# git. The previous single-stage image copied `web/dist` straight from the build context, which
# worked on a machine that happened to have run `npm run build` and failed on a clean clone —
# exactly the situation any deployment starts from.

# --- stage 1: build the dashboard -----------------------------------------
FROM node:22-slim AS web
WORKDIR /web
# Copy manifests first so the dependency layer is cached across source edits.
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- stage 2: the application --------------------------------------------
FROM python:3.12-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pic ./pic
COPY evaluation ./evaluation
COPY --from=web /web/dist ./web/dist

# No GEMINI_API_KEY is baked in, and none is needed: `auto` falls back to the deterministic
# reasoner, which is what the published benchmark uses and is markedly faster to demo.
ENV PIC_LLM_PROVIDER=auto

EXPOSE 8000
# Shell form on purpose: hosting platforms inject the port to bind, and the exec form would pass
# the literal string "${PORT}" to uvicorn.
CMD ["sh", "-c", "uvicorn pic.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
