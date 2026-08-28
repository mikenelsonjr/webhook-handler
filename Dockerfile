# Slim, not alpine: alpine uses musl, so grpcio has no wheel and gets built
# from source — minutes of compile instead of seconds of download.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# --- dependency layer ---------------------------------------------------------
# Install against a stub package so this layer is keyed on pyproject.toml alone.
# Editing application code then rebuilds in seconds instead of re-resolving and
# re-downloading the dependency tree every time.
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p ingest && touch ingest/__init__.py \
    && pip install --upgrade pip \
    && pip install ".[gcp]"

# --- application layer --------------------------------------------------------
COPY ingest/ ./ingest/
COPY main.py ./
# Reinstall the real package over the stub. --no-deps because the dependency
# layer above already resolved everything.
RUN pip install --no-deps .

# Never run as root. Cloud Run does not require it, but this image gets cloned
# into places that are less forgiving.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

# Cloud Run injects PORT; 8080 is the local default. `exec` so uvicorn is PID 1
# and receives SIGTERM directly — otherwise shutdown waits for the 10s kill
# timeout on every deploy.
ENV PORT=8080
EXPOSE 8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --no-access-log
