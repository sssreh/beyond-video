# Runs bv-web only - see docs/DEPLOY.md for the full walkthrough of
# deploying this on a Synology NAS via Container Manager.
#
# Deliberately installs the "web" extra only (fastapi/uvicorn/jinja2),
# not the full beyond-video dependency set - bv-download/bv-generate/
# bv-export (and their heavier faster-whisper/pyannote.audio/
# argostranslate/torch dependencies) are meant to keep running as
# regular CLI commands on whatever machine actually has the camera
# archive, not inside this container. This image only ever reads the
# trip folders those commands already wrote - see
# blackvue.web.trips.scan_trips().
FROM python:3.13-slim

WORKDIR /app

# Only what setuptools needs to build/install the package - not the
# whole repo (tests/, docs/, .git/, ...; see .dockerignore), so an
# unrelated change elsewhere in the repo doesn't bust this layer's
# build cache.
COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir ".[web]"

# Where the volumes docker-compose.yml mounts land inside the
# container: the trip archive bv-web browses (read-only - this
# increment never writes into it), the accounts file (read-write -
# `bv-web adduser` needs to create/update it via `docker exec`), and
# camera .cfg files (read-write - see core/camera_config.py's
# BEYOND_VIDEO_CONFIG_DIR comment). Baked in even though
# docker-compose's bind mounts would create these anyway, so running
# the image without those volumes mounted (e.g. a stray `docker run`)
# degrades to "zero trips/cameras found" rather than erroring on a
# missing path - the same "missing directory reads as empty, not an
# error" convention used everywhere else in this app.
RUN mkdir -p /data/trips /data/config /data/camera-config

EXPOSE 19373

ENTRYPOINT ["bv-web"]
CMD ["serve", "/data/trips", "--users-file", "/data/config/web-users.cfg", "--host", "0.0.0.0", "--port", "19373"]
