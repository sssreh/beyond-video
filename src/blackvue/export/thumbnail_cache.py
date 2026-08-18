"""On-demand video-frame thumbnail generation + cache.

For an archive-browser recording with no camera-native `*_THUMBNAIL`
sidecar and no `recording_is_photo()` match (see web/archive_browser.py's
`ArchiveRecording.thumbnail_path()`) - true for every FolderAdapter/
GoProAdapter video today, since both adapters' manifests declare
`"thumbnails": "generated"` but had no actual generator behind that
capability until this module existed (see CAMERA_ADAPTERS.md).

Mirrors hevc_preview.py's own cache pattern: a digest-of-resolved-path
plus mtime/size cache key (so a re-downloaded or re-encoded source
never serves a stale thumbnail), an atomic per-call temp-file rename
(so two overlapping requests racing to generate the same thumbnail
can't leave a corrupted file behind), and `enforce_cache_size_cap()`
right after every fresh write.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from ..generate.cache_utils import enforce_cache_size_cap
from .media import extract_video_thumbnail

# Thumbnails are tiny (320px-wide JPEGs) compared to the HEVC preview
# cache's full re-encoded videos, so a much smaller cap is plenty even
# for a large archive - a few thousand thumbnails easily fits.
_MAX_CACHE_BYTES = 200 * 1024 * 1024  # 200 MiB


def load_or_generate_thumbnail(source: Path, cache_dir: Path) -> Path:
    """Return a path to a small JPEG frame-grab of `source`, generating
    (and caching under `cache_dir`) one on first use and reusing that
    copy on every later call.

    Raises MediaToolError (propagated from extract_video_thumbnail())
    if ffmpeg itself fails or isn't installed - callers (see
    ArchiveRecording.thumbnail_path()) are expected to catch this and
    fall back to no thumbnail at all, the same "never break the whole
    page over one recording" posture the archive browser already takes
    for scene-text reads and other per-recording extras.
    """

    stat = source.stat()
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
    cache_path = cache_dir / f"{digest}-{stat.st_mtime_ns}-{stat.st_size}.jpg"

    if cache_path.is_file():
        return cache_path

    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_name(f"{cache_path.stem}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        extract_video_thumbnail(source, tmp_path)
        os.replace(tmp_path, cache_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    enforce_cache_size_cap(cache_dir, _MAX_CACHE_BYTES)
    return cache_path
