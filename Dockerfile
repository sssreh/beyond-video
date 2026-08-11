# Runs bv-web only - see docs/DEPLOY.md for the full walkthrough of
# deploying this on a Synology NAS via Container Manager.
#
# Used to install the "web" extra only (fastapi/uvicorn/jinja2) on the
# theory that bv-download/bv-generate/bv-export would only ever run as
# separate CLI commands elsewhere, never inside this container. That
# stopped being true once bv-web's own job runner grew triggers for
# those three commands, then bv-scribe too (see WORKING_CONTEXT.md) - a
# job started from the web UI runs *in this container*, so it needs
# the same toolchain bv-cli's own image has: ffmpeg (for real audio
# extraction/duration, not just the pure-Python MP4-box fallback) plus
# the speech/translate extras (faster-whisper/pyannote.audio/
# argostranslate, and torch transitively) and the scene extra
# (transformers/accelerate/qwen-vl-utils, for bv-scribe's scene
# description) - same size/build-time trade-off Dockerfile.cli already
# accepts. This image is now effectively bv-cli's image plus the web
# server, not a separate lightweight thing.
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Only what setuptools needs to build/install the package - not the
# whole repo (tests/, docs/, .git/, ...; see .dockerignore), so an
# unrelated change elsewhere in the repo doesn't bust this layer's
# build cache.
COPY pyproject.toml README.md ./
COPY src/ src/

# scene (transformers/accelerate/qwen-vl-utils) is needed too - bv-web's
# job runner also triggers bv-scribe (--describe-scene) in this same
# container, same reasoning as speech/translate above. Without it,
# bv-scribe fails at runtime with "No module named 'qwen_vl_utils'"
# even though it's listed as a job the web UI can start.
RUN pip install --no-cache-dir ".[web,speech,translate,scene]"

# Where the volumes docker-compose.yml mounts land inside the
# container: the raw camera archive and trip archive (both read-write
# now - bv-web's own job runner writes into both when it triggers
# bv-download/bv-generate/bv-export, not just the archive browser
# reading them), the accounts file (read-write - `bv-web adduser`
# needs to create/update it via `docker exec`), and camera .cfg files
# (read-write - see core/camera_config.py's BEYOND_VIDEO_CONFIG_DIR
# comment). Baked in even though docker-compose's bind mounts would
# create these anyway, so running the image without those volumes
# mounted (e.g. a stray `docker run`) degrades to "zero trips/cameras
# found" rather than erroring on a missing path - the same "missing
# directory reads as empty, not an error" convention used everywhere
# else in this app.
RUN mkdir -p /data/archive /data/trips /data/config /data/camera-config

EXPOSE 19373

# No explicit --users-file here - docker-compose.yml's
# BEYOND_VIDEO_USERS_FILE environment variable (see web/users.py's own
# comment on it) already points default_users_path() at
# /data/config/web-users.cfg, and that same default is what a bare
# `docker-compose run --rm bv-web adduser ...` (no CMD involved -
# `run` replaces it with the given command) needs to land on too. A
# hardcoded flag here only fixed `serve`; the env var fixes both
# subcommands from one place.
ENTRYPOINT ["bv-web"]
CMD ["serve", "/data/trips", "--host", "0.0.0.0", "--port", "19373"]
