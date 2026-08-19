"""
Raw archive browsing for bv-web: lists a camera's raw recordings -
what bv-download actually writes to disk, before any trip-grouping or
bv-export processing - with each recording's downloaded thumbnail(s)
so a long archive is easier to scan visually, without needing
bv-export to have run first.

Deliberately thin, the same way trips.py is thin relative to what it
wraps: this goes through a camera's own CameraAdapter (see
adapters/registry.py and docs/CAMERA_ADAPTERS.md) - the same adapter
bv-ls already resolves via CameraConfig.adapter - rather than adding
any new disk-scanning logic of its own, just a browsing-friendly
wrapper around Recording plus the day-grouping this page's UI needs.
The one exception is find_recording(), which calls
CameraAdapter.find_recording() - a targeted single-recording lookup
(BlackVueAdapter delegates to ArchiveReader.read_recording(); see that
method's own docstring) specifically because the thumbnail grid and
the video player's range requests each resolve one recording per HTTP
request, and a full archive scan on every one of those would be far
too slow on a large archive.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import itertools
import re
import time
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from pathlib import Path

from ..adapters import registry
from ..adapters.base import CameraAdapter
from ..adapters.telemetry_bridge import read_recording_gps
from ..archive import Asset
from ..archive import Recording
from ..archive import RecordingId
from ..archive import recording_is_photo
from ..core.camera_config import DEFAULT_ADAPTER_ID
from ..generate.media import MediaToolError
from ..generate.media import extract_video_thumbnail
from ..generate.media import load_or_compute_duration
from ..generate.scene import DescriptionEvent
from ..generate.scene import SceneOptions
from ..generate.scene import extract_description_events
from ..generate.scene import extract_description_section
from ..generate.speech import SpeechSegment
from ..generate.subtitles import format_srt
from ..lexicaltimeparser import TimeInterval
from ..telemetry.gps_reader import GpsFix

# (display label, video asset, thumbnail asset), in the order the
# detail page's video tabs and the list page's per-direction badges
# should show them.
_DIRECTIONS = (
    ("Front", Asset.FRONT, Asset.FRONT_THUMBNAIL),
    ("Rear", Asset.REAR, Asset.REAR_THUMBNAIL),
    ("Interior", Asset.INTERIOR, Asset.INTERIOR_THUMBNAIL),
)

_THUMBNAIL_ASSET_BY_DIRECTION = {
    "front": Asset.FRONT_THUMBNAIL,
    "rear": Asset.REAR_THUMBNAIL,
    "interior": Asset.INTERIOR_THUMBNAIL,
}

# (display label, asset), for the detail page's non-video sidecar
# download links - GPS/G-sensor logs, not video and not a thumbnail.
_SIDECARS = (
    ("GPS log", Asset.GPS),
    ("G-sensor log", Asset.GSENSOR),
)

# (display label, asset), for the detail page's scene/OCR text panel
# (task #681) - the two Asset types bv-generate --describe-scene/
# bv-scribe write, same pair blackvue/search.py's own "scene" group
# already searches (see TEXT_SEARCH_ASSETS there). No diarized
# equivalent exists for scene text the way transcript/translation
# have one, so unlike TEXT_SEARCH_ASSETS this is just the two.
_SCENE_TEXTS = (
    ("Front", Asset.SCENE_DESCRIPTION),
    ("Rear", Asset.SCENE_DESCRIPTION_REAR),
)

# Substring a "## Zoomed sign reads" bullet line's read text is
# checked against (case-insensitively) to drop it from scene_summary
# below - see that property's own docstring for why "not legible" is
# noise for a human-readable summary even though it's worth keeping in
# the raw file scene_texts still shows in full.
_NOT_LEGIBLE = "not legible"

# Matches the "[t=40.6s] " prefix zoom_into_signs() writes ahead of
# every bullet's own label/read text (see generate/scene.py's own
# f"- [t={timestamp:.1f}s] {det['label']}: {read_text...}" line) - a
# real per-frame float second value, always with exactly one decimal
# digit as written, but this pattern doesn't require that in case a
# future writer changes the precision.
_SIGN_READ_TIMESTAMP_RE = re.compile(r"^\[t=(?P<seconds>\d+(?:\.\d+)?)s\]\s*(?P<rest>.*)$")


@dataclass(frozen=True)
class SignRead:
    """One parsed '## Zoomed sign reads' bullet - see
    _parse_sign_reads()'s own docstring for how these get built.
    `timestamp_seconds` is the real per-frame float second value
    zoom_into_signs() sampled this detection at (see generate/scene.py's
    `_extract_full_res_frames()`); `text` is everything after the
    "[t=...s] " prefix, e.g. "blue road sign with white text: 227
    DALARO 259 HUDDINGE JORDBRO 500" - the label/read content only, no
    raw bracket notation left in it."""

    timestamp_seconds: float
    text: str

    @property
    def display_text(self) -> str:
        """The natural-language phrasing scene_summary's on-page list
        and the Read-aloud TTS narration both use instead of the raw
        "[t=60.1s]" bracket notation. Christer: "instead of trying to
        say "[t=60.1s]" it would be much better to say "At 60 seconds"
        rounded of to closest second" - saying a bracket/equals-sign
        notation aloud came out as literal, awkward speech ("bracket t
        equals sixty point one s bracket"), and nobody reading the page
        needs sub-second precision on where a sign was noticed either.
        round() here follows Python's banker's-rounding (round-half-
        to-even), which is an acceptable trade-off since this is a
        display convenience, not a precise timestamp.

        Follow-up, once recordings ran past the one-minute mark and
        "At 90 seconds" started showing up: Christer - "When naming the
        sign timestamps it ok to include minutes to if timestamp > 60
        secods." Past 59 seconds this now speaks/shows whole minutes
        plus any remaining seconds ("At 1 minute 30 seconds", "At 2
        minutes"  when the remainder is exactly 0) instead of a bare
        seconds count someone would have to do the math on themselves;
        under a minute it's unchanged ("At 45 seconds")."""

        total_seconds = round(self.timestamp_seconds)
        minutes, seconds = divmod(total_seconds, 60)

        if minutes == 0:
            time_phrase = f"{seconds} second" + ("" if seconds == 1 else "s")
        else:
            minute_phrase = f"{minutes} minute" + ("" if minutes == 1 else "s")
            if seconds == 0:
                time_phrase = minute_phrase
            else:
                second_phrase = f"{seconds} second" + ("" if seconds == 1 else "s")
                time_phrase = f"{minute_phrase} {second_phrase}"

        return f"At {time_phrase}, {self.text}"


# Prefix applied to rear-camera description/sign text by
# _label_rear_view() below - see that function's own docstring for
# why.
_REAR_VIEW_PREFIX = "Rear view: "


def _label_rear_view(text: str, direction: str) -> str:
    """Prefix `text` with "Rear view: " when `direction` is the rear
    camera, so a rear-facing description or sign read reads as
    describing what's behind the vehicle rather than blending in with
    front-camera narration - relevant because the recording detail
    page only ever shows one video player (normally the front one; see
    that template's own "No Front video ... showing X instead" hint),
    so a viewer reading or listening to the *rear* camera's own
    description/Read-aloud has no video of what's actually behind them
    to anchor the text against.

    Christer, after flagging a probable front/rear content mix-up
    earlier in this file's lag-correction work ("I gues Bielen is the
    rear camera, maybe it should be mentioned in that case"): "If its
    rear camera frames, it would be nice if the description sad
    'behind is/are' 'rear view'." A short, sentence-leading tag reads
    naturally before any sentence structure ("Rear view: A red car
    approaches."), unlike "Behind is/are ..." which only grammatically
    fits some of them.

    A no-op for any direction other than "rear" (case-insensitive, the
    same lowercase matching description_srt()/sign_read_srt() already
    use) and for empty text - nothing to label."""

    if not text or direction.lower() != "rear":
        return text
    return f"{_REAR_VIEW_PREFIX}{text}"


def _parse_sign_reads(text: str) -> list[SignRead]:
    """Pull the '## Zoomed sign reads' bullet lines (see
    generate/scene.py's zoom_into_signs()) out of a scene.txt/
    rear.scene.txt body, keeping only the ones that actually read
    something - a "not legible" line means the detection pipeline
    found a real sign/plate but couldn't read it, which is exactly the
    kind of line both scene_summary and scene.srt (see app.py's
    /archive/.../scene.srt route) want to drop. Returns [] if the
    section isn't present at all (task="ocr"-without-zoom-signs, or
    zoom_signs never found anything to crop).

    A single bullet's read text can itself span multiple raw lines -
    zoom_into_signs() writes `f"- [t=...] {label}: {read_text}"` with
    read_text taken verbatim from the model, and a multi-row sign (e.g.
    several destinations stacked on one board) comes back as a read
    with embedded newlines, e.g.:

        - [t=40.6s] blue road sign with white text: 227 DALARO
        259 HUDDINGE
        JORDBRO
        500

    An earlier version of this function only kept the "- [t=...]" line
    itself and silently dropped every continuation line below it -
    Christer caught this from a real scene.txt where "259 HUDDINGE" /
    "JORDBRO" / "500" vanished from the summary entirely (see
    WORKING_CONTEXT.md). Any non-bullet, non-blank line encountered
    while inside the section is now folded into the read currently
    being built, joined with a space, until the next "- " bullet (or
    the section ends) closes it off.

    Stops at the next '#' heading or the disclaimer footer's '---'
    divider, whichever comes first - describe_scene() always appends
    DISCLAIMER right after this section, and it doesn't start with '#'
    so a naive "stop at the next heading" scan would otherwise swallow
    it as if it were more bullet lines.

    Each flushed bullet's "[t=X.Ys] " prefix is split off into its own
    float via _SIGN_READ_TIMESTAMP_RE - if a line somehow lacks the
    prefix (a hand-edited file, or a future writer that changes the
    format), the whole content is kept as `text` with `timestamp_seconds`
    defaulting to 0.0 rather than dropping the read entirely.
    """

    lines = text.splitlines()
    in_section = False
    reads: list[SignRead] = []
    current: str | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None:
            content = current.strip()
            if content and _NOT_LEGIBLE not in content.lower():
                match = _SIGN_READ_TIMESTAMP_RE.match(content)
                if match:
                    reads.append(
                        SignRead(
                            timestamp_seconds=float(match.group("seconds")),
                            text=match.group("rest").strip(),
                        )
                    )
                else:
                    reads.append(SignRead(timestamp_seconds=0.0, text=content))
            current = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") and "zoomed sign reads" in stripped.lower():
            in_section = True
            continue
        if in_section and (stripped.startswith("#") or stripped.startswith("---")):
            _flush()
            break
        if not in_section:
            continue
        if stripped.startswith("- "):
            _flush()
            current = stripped[2:].strip()
        elif stripped and current is not None:
            current += " " + stripped
    else:
        _flush()

    return reads


def _extract_legible_sign_reads(text: str) -> list[str]:
    """scene_summary's own display-ready wrapper around
    _parse_sign_reads() - same legible-reads list as before, just each
    entry now reads as SignRead.display_text ("At 60 seconds, ...")
    instead of the raw "[t=60.1s] ..." bracket notation. See
    _parse_sign_reads() for the actual parsing/folding logic and
    SignRead.display_text for the wording rationale."""

    return [read.display_text for read in _parse_sign_reads(text)]


# How long each sign-read cue stays on screen in the generated .srt.
# A single detection is an instant, not a range - zoom_into_signs()
# samples one frame per read, so there's no real "end" time to draw
# on the way an actual transcript segment has one. 3 seconds is long
# enough to read a short sign/plate string comfortably without
# lingering awkwardly once the video has moved well past it.
_SIGN_READ_CUE_SECONDS = 3.0


def build_sign_read_srt(text: str) -> str | None:
    """Build a downloadable .srt from a scene.txt/rear.scene.txt
    body's '## Zoomed sign reads' section - see
    ArchiveRecording.sign_read_srt()'s own docstring for the feature's
    backstory and why this is the only part of scene description
    generation with real per-frame timestamps to build cues from at
    all.

    Each SignRead.timestamp_seconds becomes a cue's start time;
    SIGN_READ_CUE_SECONDS gives it a fixed on-screen duration rather
    than guessing one. Cues never overlap even when two reads share
    (or nearly share) the same source timestamp - each cue's start is
    clamped to at least the previous cue's end via a running `cursor`,
    the same non-overlapping-via-running-cursor approach
    generate/subtitles.py's own transcript segments don't need (Whisper
    segments already come with non-overlapping start/end pairs) but a
    single-instant detection does.

    Reuses generate/subtitles.py's own SpeechSegment/format_srt rather
    than inventing a second SRT formatter - same HH:MM:SS,mmm/numbered-
    cue convention bv-generate's own --srt output and the ElevenLabs
    with-timestamps download already use, so any .srt this codebase
    produces looks the same regardless of source.

    None if there are no legible reads to build cues from at all (see
    _parse_sign_reads()) - the caller turns that into a 404 rather
    than downloading an empty, cue-less .srt file.
    """

    reads = _parse_sign_reads(text)
    if not reads:
        return None

    segments = []
    cursor = 0.0
    for read in reads:
        start = max(read.timestamp_seconds, cursor)
        end = start + _SIGN_READ_CUE_SECONDS
        segments.append(SpeechSegment(start=start, end=end, text=read.text))
        cursor = end

    return format_srt(tuple(segments))


# Target chunk size for description.srt's cues - same 90-char budget
# archive_recording_detail.html's own client-side buildSrtCues() uses
# for the ElevenLabs-narration SRT, so a "how long is a cue" reader
# expectation stays consistent across every .srt this app produces.
_DESCRIPTION_CUE_MAX_CHARS = 90


def _chunk_description_text(text: str) -> list[str]:
    """Split a scene description paragraph into short, caption-sized
    chunks (~_DESCRIPTION_CUE_MAX_CHARS each) for build_description_srt()
    to space evenly across the recording's real duration - see that
    function's own docstring for why there's no per-sentence timing to
    chunk against here, unlike build_sign_read_srt()'s per-frame reads.

    Prefers to break at the end of a sentence ('.', '!', '?') within
    the chunk window, but only once at least 20 characters in - a
    period 3 characters into the remaining text would produce a
    flood of tiny one-clause cues instead of readable ones. Falls back
    to the last word boundary in the window, and finally a hard cut at
    the window's own edge if no space exists at all (never expected
    from real prose, but keeps this from looping forever on
    pathological input). Every branch consumes at least one character
    of `remaining` per iteration, so this always terminates.

    Ported from (not sharing code with) archive_recording_detail.html's
    own client-side buildSrtCues() - that one chunks against
    ElevenLabs' per-character alignment data (JS, browser-side,
    audio-timed); this one chunks plain text server-side with no
    alignment data to lean on at all, since describe_scene()'s main
    narrative pass has no internal timestamps whatsoever (see
    ArchiveRecording.description_srt()'s own docstring)."""

    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _DESCRIPTION_CUE_MAX_CHARS:
            chunks.append(remaining)
            break

        window = remaining[:_DESCRIPTION_CUE_MAX_CHARS]
        break_at = None
        for punct in ".!?":
            idx = window.rfind(punct)
            if idx >= 20:
                break_at = idx + 1
                break
        if break_at is None:
            space_idx = window.rfind(" ")
            break_at = space_idx if space_idx > 0 else _DESCRIPTION_CUE_MAX_CHARS

        chunks.append(remaining[:break_at].strip())
        remaining = remaining[break_at:].strip()

    return chunks


def build_description_srt(description: str, duration_seconds: float) -> str | None:
    """Build a downloadable .srt for a recording's main '## Description'
    paragraph, timed against the *real video's* actual length rather
    than any narration's speech timing - meant for importing alongside
    the dashcam footage itself in an editor (Christer uses Filmora),
    distinct from the ElevenLabs-narration .srt (archive_recording_
    detail.html's own client-side download), which is synced to how
    fast the chosen TTS voice happens to read the text aloud, not to
    the video's own timeline. Christer, right after getting
    build_sign_read_srt() working: "Could i also get a srt file that
    is synced with the video of 3minutes" (see WORKING_CONTEXT.md).

    describe_scene()'s main pass has no internal timing at all - it
    feeds the whole video to the model as one multi-frame inference
    call and gets back one holistic paragraph, unlike zoom_into_signs()'s
    per-frame sign reads - so there's no real per-sentence sync info to
    build cues from. Instead, this splits the description into short
    chunks (_chunk_description_text()) and spaces them evenly across
    `duration_seconds`, so the captions progress through the clip at a
    steady pace rather than appearing all at once. `duration_seconds`
    is expected to be the recording's own real elapsed time - see
    ArchiveRecording.description_srt(), which sources it from
    generate/media.py's load_or_compute_duration() (the same
    .duration.txt asset the rest of this codebase already treats as
    the source of truth for a recording's real length, self-healing
    via ffprobe if it hasn't been computed yet) rather than probing the
    video a second, web-layer-specific way.

    None if there's no text to chunk or no usable duration - the
    caller turns that into a 404 rather than downloading an empty or
    all-cues-at-t=0 .srt file.
    """

    chunks = _chunk_description_text(description)
    if not chunks or duration_seconds <= 0:
        return None

    cue_seconds = duration_seconds / len(chunks)
    segments = tuple(
        SpeechSegment(
            start=index * cue_seconds,
            end=(index + 1) * cue_seconds,
            text=chunk,
        )
        for index, chunk in enumerate(chunks)
    )

    return format_srt(segments)


def _rescale_events_to_duration(
    events: list[DescriptionEvent], duration_seconds: float
) -> list[DescriptionEvent]:
    """Stretch/compress every event's timestamp so the latest one lands
    at (or right up against) the recording's real end, preserving each
    event's relative position in the sequence rather than its raw
    number.

    Why this exists: Christer, after the bullet-parsing fix, pasted a
    real .srt where every cue but the last sat inside the clip's first
    six seconds and the final cue then stretched all the way to
    00:03:00 to cover the rest - "Look at the timestamps, they are not
    synced with video, the are synced when the where detected." He was
    right. Traced to describe_scene()'s frame sampling: fps=1.0 capped
    at max_frames=16 means a 3-minute clip is shown to the model as
    only 16 frames spread ~11 seconds apart, and DESCRIBE_PROMPT never
    tells the model the clip's real duration or that spacing. The
    "- [t=X.Ys]" values it writes aren't measurements of real elapsed
    video time at all - they're the model's own narrative pacing
    between the frames it was shown, in the order it saw them, which
    is why they came back small and monotonically increasing instead
    of spanning the actual clip.

    What IS reliable is the *order* the events were reported in - the
    model does see the frames in chronological sequence, it just has
    no grounding for how much real time separates them. So rather than
    trust the raw numbers (which produces exactly the bunched-up-then-
    one-giant-cue result Christer saw) or discard them (losing the
    real-timestamp feature's whole point), this maps them onto the
    real timeline by simple proportion: multiply every timestamp by a
    scale factor chosen so the largest timestamp in the batch lands at
    `duration_seconds * n / (n + 1)` (n = event count), not at
    `duration_seconds` itself. If the model's last event was already
    close to the real end, this is close to a no-op; if everything was
    compressed into the first few seconds like Christer's example,
    this spreads the same relative spacing back out across the whole
    clip. It's a proportional approximation, not a real fix for the
    model's lack of temporal grounding - two events that were actually
    5 seconds apart and two that were actually 50 seconds apart will
    get the same treatment if the model reported them with similar-
    looking gaps - but it turns an unusable result (everything crammed
    at the start, nothing synced at all after the first few seconds)
    into a usable approximation (spread proportionally across the real
    timeline), which is what was asked for.

    The n/(n+1) target (rather than duration_seconds outright) exists
    for a specific reason: build_description_srt_from_events() (the
    caller) always extends the *last* event's cue all the way to
    duration_seconds regardless of where that event's own timestamp
    falls - that's how a real, accurately-timed last event already
    gets its caption held through to the end of the clip. If this
    function mapped the largest timestamp exactly onto
    duration_seconds, that final cue's start and end would land on the
    same instant, collapse to zero length, and silently drop its text
    (the exact bug the previous fix in this same feature went out of
    its way to stop happening for a different cause). Reserving
    roughly one event's worth of trailing room leaves that final cue
    real, visible screen time to extend into, the same as it would for
    already-accurate input.

    Left unchanged (returned as-is) when there's nothing to scale by:
    zero events, or every timestamp at or below 0.0 (nothing positive
    to anchor the proportion on - rescaling would require dividing by
    a non-positive number). build_description_srt_from_events()'s own
    clamp/merge logic still runs afterward either way, so a degenerate
    case here doesn't produce a broken .srt, just an unscaled one."""

    if not events:
        return events

    max_timestamp = max(event.timestamp_seconds for event in events)
    if max_timestamp <= 0:
        return events

    target_max = duration_seconds * len(events) / (len(events) + 1)
    scale = target_max / max_timestamp
    return [
        DescriptionEvent(event.timestamp_seconds * scale, event.text)
        for event in events
    ]


# Multiplier applied to the raw `duration_seconds / max_frames` average
# frame-sampling interval in _apply_frame_sampling_lag() below - see
# that function's own docstring for the two real-world data points
# this was calibrated against. 1.0 (one interval) matched Christer's
# first report almost exactly; his second report, on a different
# ~3-minute clip, needed roughly 1.5 intervals' worth of extra shift.
# Kept as its own named constant, not folded into the one-line formula,
# so a third data point can update just this number without touching
# the surrounding logic.
_FRAME_SAMPLING_LAG_MULTIPLIER = 1.5


# Position-dependent correction added on top of the flat
# duration/max_frames*_FRAME_SAMPLING_LAG_MULTIPLIER offset above, by
# _apply_frame_sampling_lag() below. RESET to flat/no-op (2026-08-19).
#
# This used to hold an 8-knot curve fit to a single real clip
# (recording 20220927_132155_E front), zigzagging from -24.72s to
# +9.16s across that one clip's events - see this file's git history
# (and WORKING_CONTEXT.md's "Curve-based lag correction" entry) for
# the full derivation if it's ever needed again. Christer, introducing
# the frame-viewer that made it possible to check that curve against
# real extracted frames: "frame 6 in srt talks about the bus but in
# zoomed frames it talk about the license plate" / "I dont se any red
# bus" - a real frame plainly not matching its shown cue. Rather than
# patch one more knot onto a curve already known to be a single, noisy
# clip's shape, Christer's call: "otherwise we scratch every trim
# except red bus which is very accurate and start from zero" - keep
# only the one point he's fully confident in (the bus event, real
# correction -10.60s, "The red bus was my focus and are most correct
# of them all"), discard the rest, and rebuild from scratch using the
# frame-viewer's own calibration log (frame_calibration.jsonl, see
# app.py's archive_recording_frames route) as real data accumulates -
# a faster, per-frame way to gather trustworthy corrections than
# hand-retiming a whole .srt file.
#
# The bus point itself isn't re-encoded here: its clip-relative
# position (was 4/9 - fourth of nine events in that one clip) doesn't
# transfer to a future clip with a different event count, the same
# structural gap this curve's own history already flagged ("expect
# this table to need more real per-clip data points before its shape
# firms up"). Flat/zero until enough new calibration-log data exists
# to fit a curve that isn't extrapolated from a single clip's shape.
_LAG_CORRECTION_CURVE: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),
    (1.0, 0.0),
)

# Position-dependent correction for sign/plate reads - mirrors
# _LAG_CORRECTION_CURVE above, applied via _apply_sign_lag(). RESET to
# flat/no-op alongside it (2026-08-19) - none of its three knots
# (BESIKTA/plate/SHURGARD) was the one point Christer named as
# "very accurate" (that was the bus, a description event, not a sign
# read), so none of them survives the "scratch every trim except red
# bus" reset either. See git history / WORKING_CONTEXT.md's
# "Curve-based lag correction" entry for the discarded values if
# useful context for a future recalibration.
_SIGN_LAG_CORRECTION_CURVE: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),
    (1.0, 0.0),
)


def _interpolate_correction_curve(
    fraction: float, curve: tuple[tuple[float, float], ...]
) -> float:
    """Piecewise-linear lookup into a (position, correction_seconds)
    curve like _LAG_CORRECTION_CURVE or _SIGN_LAG_CORRECTION_CURVE
    above, for an event at the given normalized position (0.0 = first,
    1.0 = last) within its own sequence. Extrapolates flat (holds the
    nearest known value) for a position beyond either end of the
    curve's own range, rather than guessing at a slope past real data;
    interpolates linearly between the two nearest known knots
    otherwise."""

    fraction = min(max(fraction, 0.0), 1.0)

    if fraction <= curve[0][0]:
        return curve[0][1]
    if fraction >= curve[-1][0]:
        return curve[-1][1]

    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x0 <= fraction <= x1:
            span = x1 - x0
            if span <= 0:
                return y0
            weight = (fraction - x0) / span
            return y0 + weight * (y1 - y0)

    return curve[-1][1]  # unreachable given the clamp above


def _apply_sign_lag(signs: list[DescriptionEvent]) -> list[DescriptionEvent]:
    """Shift each sign/plate read's real per-frame timestamp by a
    position-dependent correction (see _SIGN_LAG_CORRECTION_CURVE
    above) - the sign-read counterpart to _apply_frame_sampling_lag()'s
    description-event treatment, using a sign read's own index among
    this recording's sign reads (not the description events') as the
    normalized position.

    A single sign read (or none) is returned unchanged: with only one
    point, there's no meaningful "position" to interpolate from, and
    guessing which of the curve's very different knot values would
    apply is worse than leaving it untouched."""

    count = len(signs)
    if count <= 1:
        return signs

    shifted_signs = []
    for index, sign in enumerate(signs):
        fraction = index / (count - 1)
        extra = _interpolate_correction_curve(fraction, _SIGN_LAG_CORRECTION_CURVE)
        shifted_signs.append(DescriptionEvent(sign.timestamp_seconds + extra, sign.text))
    return shifted_signs


def _nominal_frame_timestamps(
    duration_seconds: float, max_frames: int
) -> list[float]:
    """Approximate the real video timestamps describe_scene()'s own
    video-sampling step looked at - max_frames frames spread evenly
    across the clip, the same average-interval assumption
    _apply_frame_sampling_lag()'s base_offset is itself built on (see
    that function's own docstring). bv-web has no record of the real
    sampled frame indices (that's a generation-time-only detail, not
    persisted anywhere - describe_scene() hands the whole video to
    qwen_vl_utils, which does its own internal frame selection), so
    this is the same kind of necessary approximation the lag curves
    already are, not an exact reconstruction.

    Used to power the description-frame viewer (see app.py's
    archive_recording_frames route) - Christer: "do you think i can
    see the describe frames and help matching them," wanting to
    visually compare a real extracted frame against the description
    text near it, rather than reconstructing the moment from full
    video playback. Frame 0 lands at t=0.0; frame i lands at
    `duration * i / max_frames` for i in 0..max_frames-1 - close
    enough for a human to recognize "yes, that's the moment," which is
    all this needs to be useful, unlike the lag-correction curves that
    need to be numerically precise."""

    if duration_seconds <= 0 or max_frames <= 0:
        return []
    return [duration_seconds * i / max_frames for i in range(max_frames)]


def _apply_frame_sampling_lag(
    events: list[DescriptionEvent], duration_seconds: float
) -> list[DescriptionEvent]:
    """Shift every (already-rescaled) description event later by roughly
    one and a half frame-sampling intervals, then further by a
    position-dependent correction (see _LAG_CORRECTION_CURVE above),
    to correct a systematic early bias _rescale_events_to_duration()
    alone can't fix.

    Christer, testing the rescaled timestamps against a real clip: "put
    description 11 seconds later." Root cause is the same frame
    sampling _rescale_events_to_duration()'s own docstring already
    explains: describe_scene() only ever shows the model
    SceneOptions.max_frames (16 by default) frames spread evenly across
    the whole clip, so for a ~3-minute recording those frames sit
    roughly `duration / 16` ~= 11 seconds apart - which is close to the
    11 seconds Christer first observed. The model describes what it
    sees in a given sampled frame, but that frame is itself already
    partway through the interval it represents - proportional
    rescaling alone corrects the *spread* of timestamps across the
    clip, not this per-frame lag, since every event ends up anchored
    slightly earlier than the real moment it describes, by about the
    same amount.

    First fix landed with a 1x multiplier (one full average interval),
    matching that first report closely enough. Christer then tested
    against a second, different ~3-minute clip after this shipped and
    found the description events - not just the one he originally
    flagged, "most/all cues" - still consistently landing early by
    roughly another 5 seconds on top of the already-applied offset.
    Same clip length, same nominal `duration/max_frames` interval
    (~11.25s), but a meaningfully different amount of *additional* lag
    needed - confirming what this function's docstring already
    admitted: the "one interval" model is itself an approximation of
    the model's own narrative pacing, which varies per generation, not
    a value derivable exactly from clip length alone. Bumped
    `_FRAME_SAMPLING_LAG_MULTIPLIER` from 1.0 to 1.5 to split the
    difference across both real data points rather than refitting only
    to the newest one and potentially overshooting the first clip
    Christer already confirmed was correct.

    Christer then sent back a third data point of a different kind: the
    actual corrected .srt for a real clip, retimed by hand against the
    video rather than just a one-line report. Matched by content
    against bv-web's own output, the per-event errors turned out to
    zigzag from -24.72s to +9.16s within that single clip - proving a
    single flat multiplier, however well-tuned, structurally cannot
    track this: adding enough offset to fix the worst-lagging event
    would overshoot the events that were already accurate or even
    running early. Asked directly whether to keep chasing a single
    number anyway: "Yes do your best to find a formula, even if i zig
    zag" - so `_LAG_CORRECTION_CURVE` now adds a second,
    position-dependent term on top of the flat one, interpolated
    through those real per-event errors instead of smoothed into
    something tidier. Still fundamentally the same approximation this
    whole feature has always been - real per-clip narrative pacing
    isn't knowable in advance - just a more detailed one, fit from more
    real data than the flat multiplier alone had.

    bv-web has no record of what SceneOptions were actually used to
    generate a given scene.txt (that's a generation-time-only
    parameter, not persisted anywhere), so this assumes the default
    max_frames - the same approximation-not-exact-fix spirit as the
    rescale itself.

    Capped at _rescale_events_to_duration()'s own `target_max` (the
    `duration_seconds * n / (n+1)` reserved-trailing-room ceiling) so
    the shift can never push the last event's timestamp far enough to
    collapse its own cue to zero length and drop its text - the exact
    failure mode two earlier fixes in this same feature already went
    out of their way to prevent, for different root causes. Only a
    ceiling, not a floor: `_LAG_CORRECTION_CURVE` can and does go
    negative enough that an event's final timestamp lands earlier than
    even its pre-offset rescaled position (the real "traffic flows"
    data point above needed exactly this) - the downstream sort/
    collapse/merge-forward logic in build_description_srt_from_events()
    already handles out-of-order or negative timestamps safely, the
    same as it does for a raw negative model timestamp.

    Only description events get this treatment; sign reads get their
    own, separately-calibrated correction via _apply_sign_lag() (see
    _SIGN_LAG_CORRECTION_CURVE above) - the original assumption that
    sign reads' real per-frame timestamps needed no correction at all
    turned out not to fully hold either, once real corrected data
    existed to check it against.

    `_LAG_CORRECTION_CURVE` itself was reset to flat/zero on
    2026-08-19 - see that constant's own comment for why - so this
    function's position-dependent term is currently a no-op; only
    `base_offset` (the flat multiplier) still applies. The zigzag
    history above is kept as a record of what didn't generalize past
    one clip, not a description of the curve's current shape."""

    if not events or duration_seconds <= 0:
        return events

    max_frames = SceneOptions().max_frames
    if max_frames <= 0:
        return events

    base_offset = duration_seconds / max_frames * _FRAME_SAMPLING_LAG_MULTIPLIER
    cap = duration_seconds * len(events) / (len(events) + 1)
    event_count = len(events)
    shifted_events = []
    for index, event in enumerate(events):
        fraction = index / (event_count - 1) if event_count > 1 else 0.0
        extra = _interpolate_correction_curve(fraction, _LAG_CORRECTION_CURVE)
        shifted_events.append(
            DescriptionEvent(
                min(event.timestamp_seconds + base_offset + extra, cap),
                event.text,
            )
        )
    return shifted_events


# How a description/sign cue's on-screen/spoken *display* window is
# derived from its own (already lag-corrected) real event timestamp -
# replaces the earlier separate lead-time-shift + reading-time-trim
# design (2026-08-19). Christer, after using the frame-viewer this
# lead/trim design fed into to compare real frames against their
# shown cues: "I want the description to pop up a couple of seconds
# before the video then last a couple of seconds after, unless there
# is something more happening. And yes we also need to consider how
# long time each aloud takes."
#
# _CUE_LEAD_SECONDS/_CUE_TRAIL_SECONDS are that "couple of seconds"
# before and after, read literally as 2.0 each - replacing the old
# _DESCRIPTION_LEAD_TIME_SECONDS (1.5, lead-only, picked from a
# "1 to 2 seconds" range) and the old _trim_cue_to_reading_time()'s
# symmetric-centering behavior (which had no guaranteed minimum trail
# at all - a cue could shrink to well under the real event's moment if
# the gap to the next cue was short). "Unless there is something more
# happening" is the speaking-duration extension below: the couple-of-
# seconds trail is a floor, not a ceiling, extended further whenever
# the cue's own text needs more time to actually say.
#
# _CUE_SPEAKING_CHARS_PER_SECOND estimates how long ElevenLabs will
# actually take to speak the cue's text - lowered from the old
# _CUE_READING_CHARS_PER_SECOND's 15.0 (a silent on-screen-caption
# *reading*-speed guess) to a more conservative 12.0, since the
# overlapping-Read-aloud-audio bug fixed earlier in this project
# (see WORKING_CONTEXT.md's "Fixed overlapping Read-aloud cue audio")
# already showed real ElevenLabs clips can run longer than a
# reading-pace estimate implies. PADDING_SECONDS/MIN_SECONDS carry
# over unchanged from the old constants (small breathing room beyond
# the raw estimate; a floor so a very short bullet still gets a
# comfortable minimum).
_CUE_LEAD_SECONDS = 2.0
_CUE_TRAIL_SECONDS = 2.0
_CUE_SPEAKING_CHARS_PER_SECOND = 12.0
_CUE_READING_PADDING_SECONDS = 1.0
_CUE_READING_MIN_SECONDS = 2.0


def _cue_display_window(
    real_start: float, real_end: float, prev_display_end: float, text: str
) -> tuple[float, float]:
    """The actual on-screen/spoken window for one cue, given its real
    (already lag-corrected) [real_start, real_end) span - real_start is
    this event's own timestamp after cursor-clamping, real_end is the
    next real event's start (or duration_seconds for the last cue) -
    same untrimmed values build_description_srt_from_events()'s own
    overlap/collapse-detection cursor already tracks, so this is purely
    a display-time decision layered on top, not a second source of
    truth about when things "really" happen.

    Starts _CUE_LEAD_SECONDS before real_start (Christer: "pop up a
    couple of seconds before"), floored at 0.0 (nothing earlier than
    the clip's own start) and never before `prev_display_end` - the
    previous cue's own display window, tracked by the caller across
    iterations - so a lead-time pull-back can never make two cues'
    display windows visually overlap.

    Ends at least _CUE_TRAIL_SECONDS after real_start (Christer: "then
    last a couple of seconds after"), extended further when the text
    needs more than that to actually say - Christer's immediate
    follow-up, "unless there is something more happening... we also
    need to consider how long time each aloud takes": a longer cue
    gets a longer trail rather than being cut off at a fixed couple of
    seconds. Capped at `real_end` either way - the trail can make a
    cue linger, but never into the next cue's own real moment, the
    same non-overlap guarantee the old design's next-event cap
    provided."""

    lead_start = max(0.0, real_start - _CUE_LEAD_SECONDS)
    display_start = max(lead_start, prev_display_end)

    speaking_duration = max(
        _CUE_READING_MIN_SECONDS,
        len(text) / _CUE_SPEAKING_CHARS_PER_SECOND + _CUE_READING_PADDING_SECONDS,
    )
    natural_end = real_start + _CUE_TRAIL_SECONDS
    speaking_end = display_start + speaking_duration
    display_end = min(max(natural_end, speaking_end), real_end)
    display_end = max(display_end, display_start)

    return display_start, display_end


def build_description_srt_from_events(
    events: list[DescriptionEvent],
    duration_seconds: float,
    *,
    signs: list[SignRead] | None = None,
) -> str | None:
    """Build a downloadable .srt for a recording's '## Description'
    events using their *real* per-event timestamps, once DESCRIBE_PROMPT
    started asking the model for a bulleted, timestamped description
    (see generate/scene.py's DescriptionEvent/extract_description_events())
    instead of one holistic paragraph. This is what
    ArchiveRecording.description_srt() actually wants: Christer, right
    after getting the evenly-spaced version of this feature working:
    "It would have been nice to both say and subtitle 'To the left,
    there's a red bus passing alongside the vehicle' at the same time
    you can see the red buss pass" - evenly-spaced chunking could never
    deliver that, since it has no idea when in the clip anything
    actually happens; real per-event timestamps do.

    `signs`, if given, are this recording's zoomed-in sign/plate reads
    (see build_sign_read_srt()/_parse_sign_reads()) merged onto the
    same timeline as the description events, so one .srt covers
    everything with a real per-frame or per-event timestamp instead of
    two separate downloads. Christer: "I would also like the signs be
    included both in the srt and the read aloud." Unlike description
    events, sign reads are NOT rescaled - zoom_into_signs() samples
    real frames at known seconds (see generate/scene.py's
    _extract_full_res_frames()), so their timestamps are already
    accurate video time, not the model's own narrative pacing the way
    description events are (see _rescale_events_to_duration()'s
    docstring). They DO still get a small position-dependent
    correction via _apply_sign_lag() (see _SIGN_LAG_CORRECTION_CURVE's
    own comment for the real data behind it and why it's a much
    smaller, more linear correction than the description-event one) -
    real per-frame source timestamps turned out not to be perfectly
    accurate video time either, just closer to it than description
    events. Each sign read is converted to a DescriptionEvent (same
    timestamp_seconds/text shape) purely so it can flow through the
    same sort/clamp/merge pipeline below as an ordinary event - its
    text is the plain sign/plate read ("BIELEŃ"), not the "At N
    seconds, ..." phrasing display_text uses for the on-page list and
    read-aloud narration, since a synced caption already conveys
    timing by when it appears on screen.

    Before anything else, every *description* event's timestamp is
    rescaled by _rescale_events_to_duration() so the latest one lines
    up with the recording's real end - see that function's own
    docstring for why: the model's raw timestamps reflect its own
    narrative pacing between the handful of frames it was actually
    shown, not real elapsed video time, so used verbatim they come
    back bunched into the first few seconds of a much longer clip.

    Each (now-rescaled) event's real [start, end) span still runs from
    its own timestamp to the next event's (or `duration_seconds` for
    the last one) - `cursor` and the overlap/collapse guards below
    track this real span exactly as before. What's actually written to
    the .srt is a separate *display* window computed by
    _cue_display_window() from that real span: pop up a couple of
    seconds before the real moment, stay at least a couple of seconds
    after it, longer if the text needs more time to say (see that
    function's own docstring) - not the real span's entire, possibly
    long and uneventful, duration. Christer, right after the rescale
    fix first spread events out across the whole clip: "The timstamps
    are better, the should probably be trimmed in the begining and and
    the end, so more centered at not that long"; later, after using
    the frame-viewer this fed into to spot-check real cues against real
    frames: "I want the description to pop up a couple of seconds
    before the video then last a couple of seconds after, unless there
    is something more happening... we also need to consider how long
    time each aloud takes" - the current lead/trail/speaking-duration
    design directly implements that second request, superseding the
    original center-trim.

    Both the start and the next event's timestamp are clamped to
    `[cursor, duration_seconds]` - `cursor` (the previous cue's own
    untrimmed end) guards against out-of-order or duplicate timestamps
    the same way build_sign_read_srt()'s cursor does, and the
    `duration_seconds` cap guards against any remaining rounding
    overshoot past the recording's real length.

    A cue that collapses to zero or negative length after clamping
    doesn't just lose its text: Christer, after the first real-world
    fix to this feature's parsing, pasted a real response whose very
    first bullet had a negative timestamp ("[t=-0.3s]") that clamped
    to the same instant as the following bullet - under the original
    drop-it-silently behavior that sentence would have vanished from
    the .srt entirely (while still showing up fine in the plain-prose
    description elsewhere, since that path doesn't clamp anything).
    So a collapsed cue's text is instead prepended onto whichever cue
    ends up starting next, as long as there *is* a next one - the
    described moment still gets shown, just sharing a caption with the
    next real cue rather than getting a broken zero-length entry of
    its own. The one case this does NOT apply to is the very last
    event in the list collapsing (typically a timestamp past
    `duration_seconds` with nothing left in the video to attach it
    to) - there's no following cue to merge into, so it's dropped, the
    same as before. If every cue collapses this way (pathological
    input), returns None so the caller can fall back to
    build_description_srt()'s evenly-spaced chunking instead of
    downloading something broken.

    Each real, non-collapsed cue's *display* window is computed by
    _cue_display_window() from its own real [start, end) span, tracked
    across iterations via `display_cursor` so one cue's lead-time
    pull-back can never visually overlap the previous cue's own
    display window - see that function's own docstring for the
    lead/trail/speaking-duration design.

    None if there are no events (and no signs) or no usable duration -
    same contract as build_description_srt()."""

    sign_events = [
        DescriptionEvent(read.timestamp_seconds, read.text) for read in (signs or [])
    ]
    sign_events = _apply_sign_lag(sign_events)
    if (not events and not sign_events) or duration_seconds <= 0:
        return None

    events = _rescale_events_to_duration(events, duration_seconds)
    events = _apply_frame_sampling_lag(events, duration_seconds)
    ordered = sorted(events + sign_events, key=lambda event: event.timestamp_seconds)
    segments: list[SpeechSegment] = []
    cursor = 0.0
    display_cursor = 0.0
    pending_text: list[str] = []
    for index, event in enumerate(ordered):
        start = max(min(event.timestamp_seconds, duration_seconds), cursor)
        if index + 1 < len(ordered):
            next_start = min(ordered[index + 1].timestamp_seconds, duration_seconds)
        else:
            next_start = duration_seconds
        end = max(next_start, start)
        text = " ".join([*pending_text, event.text]) if pending_text else event.text
        if end > start:
            display_start, display_end = _cue_display_window(start, end, display_cursor, text)
            segments.append(SpeechSegment(start=display_start, end=display_end, text=text))
            cursor = end
            display_cursor = display_end
            pending_text = []
        else:
            # Collapsed - carry the text forward instead of dropping
            # it, unless this is the last event (nothing left to carry
            # it into, see docstring).
            pending_text.append(event.text)

    if not segments:
        return None

    return format_srt(tuple(segments))


# RecordingId.kind's single-letter codes - see recording_id.py's own
# docstring on "A" (observed on real hardware, meaning unconfirmed).
_KIND_LABELS = {
    "N": "Normal",
    "E": "Event",
    "M": "Manual",
    "P": "Parking",
    "A": "Unknown",
}


@dataclass(frozen=True)
class ArchiveRecording:
    """One raw archive recording, wrapped for the browsing UI."""

    camera_id: str
    recording: Recording

    @property
    def id(self) -> str:
        """The recording's own id string (e.g. "20260715_140212_N") -
        already a fixed, filesystem-safe, URL-safe format, same as
        RecordingId.parse() requires on the way in."""

        return self.recording.id.value

    @property
    def timestamp(self) -> datetime:
        return self.recording.id.timestamp

    @property
    def kind_label(self) -> str:
        return _KIND_LABELS.get(self.recording.id.kind, self.recording.id.kind)

    @property
    def size(self) -> int:
        return self.recording.size

    @property
    def size_label(self) -> str:
        """Human-readable size (e.g. "482M") - a small self-contained
        formatter rather than importing cli/bv_ls.py's format_size()
        into web/, which would pull a CLI module into bv-web for one
        function."""

        value = float(self.size)
        for unit in ("B", "K", "M", "G", "T"):
            if value < 1024 or unit == "T":
                return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
            value /= 1024
        raise AssertionError("unreachable")  # loop always returns on "T"

    @property
    def thumbnail_direction(self) -> str | None:
        """Lowercase direction name ("front"/"rear"/"interior")
        matching the first available thumbnail (front preferred, then
        rear, then interior) - what the thumbnail-serving route's URL
        expects. None if this recording has no thumbnail at all -
        e.g. an older archive predating the thumbnail sidecar-probing
        fix (see core/blackvue_camera.py's
        _probe_missing_thumbnails()), or a camera/firmware that
        doesn't serve .thm files.

        Also returns "front" whenever thumbnail_path("front") would
        itself return something even without a real `*_THUMBNAIL`
        sidecar - a photo recording (served as its own thumbnail) or
        any recording with a FRONT video at all (a frame-grab can
        always be generated on demand - see thumbnail_path()'s own
        docstring). This must stay in sync with thumbnail_path()'s
        fallback chain: this property is what the grid template
        checks to decide whether to render an <img> at all
        (archive_recording_list.html), so if thumbnail_path() can
        find something for "front" but this returns None for it, the
        grid would wrongly show "No thumbnail" over a recording that
        actually has one - exactly the bug this docstring update
        fixed (thumbnail_path()'s own photo fallback existed for a
        while with no matching branch here, so photo recordings never
        actually showed a thumbnail in the grid despite the code
        appearing to support it)."""

        for label, _, thumbnail_asset in _DIRECTIONS:
            if self.recording.has(thumbnail_asset):
                return label.lower()
        if recording_is_photo(self.recording):
            return "front"
        if self.recording.file(Asset.FRONT) is not None:
            return "front"
        return None

    @property
    def videos(self) -> list[tuple[str, str]]:
        """(direction label, filename) pairs for every video this
        recording actually has, front/rear/interior order - what the
        detail page's video tabs/download links and the list page's
        per-direction badges are built from."""

        result = []
        for label, video_asset, _ in _DIRECTIONS:
            asset_file = self.recording.file(video_asset)
            if asset_file is not None:
                result.append((label, asset_file.name))
        return result

    def video_path(self, direction: str) -> Path | None:
        """Resolve a direction name ("front"/"rear"/"interior") to its
        real video file's path, or None if this recording has no video
        for that direction. Used by the description-frame viewer (see
        app.py's archive_recording_frames route and
        _nominal_frame_timestamps() below) to extract the actual video
        frames the description model was shown, at their approximate
        sample times - Christer, on trying to fine-tune
        _LAG_CORRECTION_CURVE's timing further by hand: "do you think i
        can see the describe frames and help matching them" - letting
        him compare a real frame against the description text next to
        it, rather than reconstructing the moment from full video
        playback."""

        for label, video_asset, _ in _DIRECTIONS:
            if label.lower() == direction.lower():
                asset_file = self.recording.file(video_asset)
                return asset_file.path if asset_file is not None else None
        return None

    @property
    def source_filename(self) -> str | None:
        """This recording's real, on-disk FRONT filename, or None if
        it's already id-derived (a BlackVue archive's own filenames
        are synthesized from the recording id, e.g.
        "20260715_133255_NF.mp4" for id "20260715_133255_N" - showing
        it there would just repeat the Recording id already shown
        right next to it) or it has no FRONT asset at all.

        For a FolderAdapter/GoProAdapter archive the real filename is
        genuinely different information - a GoPro's own
        "GH010023.MP4" or an arbitrary folder video's own name - useful
        for spotting a same-id collision (two different files that
        happened to resolve to the same synthesized id) and for
        recognizing a specific file Christer already knows by name.
        Same "is the real filename worth showing at all" predicate
        cli/bv_ls.py's own _source_column_needed() already uses per-
        archive; this is the same check applied per-recording, since
        the grid has no equivalent whole-archive gate to hang off of.
        """

        asset_file = self.recording.file(Asset.FRONT)
        if asset_file is None:
            return None
        if asset_file.path.name.startswith(str(self.recording.id)):
            return None
        return asset_file.name

    @property
    def sidecars(self) -> list[tuple[str, str]]:
        """(display label, filename) pairs for this recording's
        non-video, non-thumbnail sidecar files (GPS/g-sensor logs) -
        the detail page's other download links."""

        result = []
        for label, asset in _SIDECARS:
            asset_file = self.recording.file(asset)
            if asset_file is not None:
                result.append((label, asset_file.name))
        return result

    @property
    def has_video(self) -> bool:
        """False if this recording has no video at all - possible
        even with a thumbnail present, since the two download
        separately (the thumbnail is small and downloads fast; the
        video is much bigger and can fail/lag behind, or the camera
        may have rotated the video off its SD card via loop recording
        before bv-download ever got to it - see WORKING_CONTEXT.md).
        The grid still shows the thumbnail in this case (it's useful
        information on its own), but archive_recording_list.html
        overlays a red cross on it using this flag, since a thumbnail
        alone isn't something the detail page can actually play."""

        return bool(self.videos)

    @property
    def has_gps(self) -> bool:
        return self.recording.has(Asset.GPS)

    @property
    def gps_path(self) -> Path | None:
        """Path to this recording's .gps sidecar file, or None if it
        doesn't have one (see has_gps) - what the archive detail
        page's "Show start location" link reads via
        first_valid_gps_fix() below."""

        asset_file = self.recording.file(Asset.GPS)
        return asset_file.path if asset_file else None

    @property
    def has_gsensor(self) -> bool:
        return self.recording.has(Asset.GSENSOR)

    @property
    def scene_texts(self) -> list[tuple[str, str]]:
        """(direction label, text) pairs for whichever scene/OCR
        description(s) this recording actually has (task #681 - "the
        only way to read a scene description is opening the file
        directly on disk"). Empty list if neither exists - the vast
        majority of recordings, unless bv-generate --describe-scene or
        bv-scribe has run against this camera's archive.

        A read failure (permissions, a file that vanished between the
        directory scan and this read, a mounted archive going away
        mid-request - the same real failure modes ArchiveRecording's
        other read paths already tolerate) is surfaced as a bracketed
        placeholder message rather than raising and taking down the
        whole detail page over one unreadable text file - the video/
        GPS/other panels on this page are independently useful even if
        this one can't render.
        """

        result = []
        for label, asset in _SCENE_TEXTS:
            asset_file = self.recording.file(asset)
            if asset_file is None:
                continue
            try:
                text = asset_file.path.read_text(encoding="utf-8")
            except OSError as exc:
                text = f"[could not read {asset_file.name}: {exc}]"
            result.append((label, text))
        return result

    @property
    def scene_summary(self) -> list[tuple[str, str, list[str]]]:
        """(direction label, description, legible sign reads) triples -
        a cleaner read of whatever scene_texts already has, for
        someone who just wants "what happened + what signs said"
        without wading through the raw on-screen-text dump or the
        "not legible" clutter in the zoomed-sign-reads section.
        Christer, after seeing how much of a real scene.txt is "not
        legible" noise for his bv-search use case: "maybe i just want
        a report on the scene files for human reading" -> "like a
        trip-summary but per recording, could be shown when you look
        at a video... only freshly generated and not a new file" (see
        WORKING_CONTEXT.md). Computed live from the same files
        scene_texts reads on every call - no new asset file is ever
        written, and unlike --trip-summary this doesn't call the
        vision model again, it just re-parses text already on disk.

        Skips any direction where neither a description nor a single
        legible sign read was found - e.g. a rear file generated
        alongside --camera both, whose forced OCR-only pass has no
        '## Description' section at all and may have nothing legible
        in it either, so there'd be nothing worth showing.

        A rear-camera description is prefixed via _label_rear_view()
        the same way description_srt() labels its own rear cues - see
        that function's own docstring for why (this page shows only
        one video, so rear text needs to say it's rear text on its
        own). The per-item sign-read list is left unprefixed: it's
        already grouped under this same direction's own "Rear" section
        header just above it, so repeating the label on every bullet
        would just be noise.
        """

        result = []
        for label, text in self.scene_texts:
            description = extract_description_section(text)
            description = _label_rear_view(description, label)
            legible_reads = _extract_legible_sign_reads(text)
            if description or legible_reads:
                result.append((label, description, legible_reads))
        return result

    def sign_read_srt(self, direction: str) -> str | None:
        """A downloadable .srt built from this recording's '## Zoomed
        sign reads' - the one part of scene description generation
        that has real per-frame timestamps at all (describe_scene()'s
        own main narrative pass feeds the whole video as one inference
        call and has no internal timing whatsoever - only
        zoom_into_signs() samples individual frames at known seconds).
        Christer, right after getting the synced ElevenLabs .srt
        working: "Does the scene detection ever have the timestamps
        for the description, then i would like a scene.srt file to"
        (see WORKING_CONTEXT.md) - meant to be imported alongside the
        recording's own video in an editor (he uses Filmora) so a
        sign-read caption pops up at the exact video moment it was
        actually detected, the same way the ElevenLabs .srt already
        does for read-aloud narration.

        `direction` is "front" or "rear" (lowercase, matching the
        thumbnail-route/_THUMBNAIL_ASSET_BY_DIRECTION convention
        elsewhere in this module) - scene_texts' own labels are
        capitalized ("Front"/"Rear"), so this does the same
        .lower() comparison _resolve_camera_target() and friends use
        rather than requiring an exact-case match from the URL.

        None if this recording has no scene text for that direction
        at all, or the section exists but has no legible reads left
        after filtering "not legible" ones out - the route above turns
        either case into a 404 rather than a link to an empty file.
        """

        for label, text in self.scene_texts:
            if label.lower() == direction.lower():
                return build_sign_read_srt(text)
        return None

    def description_srt(self, direction: str) -> str | None:
        """A downloadable .srt for this recording's main '## Description'
        text, timed against the recording's own real elapsed time
        rather than any TTS narration's speech timing. Christer,
        right after getting the sign-read .srt above: "Could i also
        get a srt file that is synced with the video of 3minutes" -
        and then, once that shipped as evenly-spaced chunking (the
        best available option at the time, since describe_scene()'s
        description pass had no internal timing at all): "It would
        have been nice to both say and subtitle 'To the left, there's
        a red bus passing alongside the vehicle' at the same time you
        can see the red buss pass." That's what this now does for real:
        DESCRIBE_PROMPT asks the model for a bulleted, per-event-
        timestamped description (see generate/scene.py's
        DescriptionEvent/extract_description_events()), so a recording
        generated after that change has real per-event sync points to
        build cues from - build_description_srt_from_events() is tried
        first. Christer, same message: "please keep the old output" -
        an older scene.txt written before this change (or a still
        photo, which has no timeline to timestamp against at all) has
        no events to find, so extract_description_events() returns []
        and this falls straight back to the original evenly-spaced
        build_description_srt() behavior, completely unchanged from
        before. Nothing needs regenerating for existing archives to
        keep working exactly as they did; only a freshly (re-)generated
        scene.txt gets the real-timestamp upgrade.

        Also passes this direction's zoomed-in sign/plate reads (see
        _parse_sign_reads()) into build_description_srt_from_events()'s
        `signs` param - Christer: "I would also like the signs be
        included both in the srt and the read aloud." Those keep their
        own real per-frame timestamps rather than joining the
        description events' pool that gets rescaled (see that
        function's own docstring for why description timestamps need
        rescaling and sign-read ones don't). A recording with legible
        signs but no '## Description' events at all still gets a real,
        signs-only .srt out of this - the events-based path is tried
        whenever there's *either* events or signs, not just events.

        Sources the recording's real length from generate/media.py's
        load_or_compute_duration() - the same .duration.txt asset
        every other "how long did this recording actually last" answer
        in this codebase already uses (self-healing: reads the cached
        file if bv-generate --get-duration has already run, otherwise
        probes it once via ffprobe and writes the cache for next time)
        - deliberately reusing that existing generation-pipeline
        machinery rather than adding a second, web-layer-specific way
        to probe a video's length. Both the events-based and evenly-
        spaced builders use this same duration as their upper bound.

        `direction` follows the same lowercase "front"/"rear" matching
        against scene_texts' capitalized labels as sign_read_srt().

        None if this direction has no '## Description' text at all, or
        no duration could be determined (no front/rear video to probe,
        or the probe itself failed) - the route above turns either
        case into a 404 rather than a link to an empty or all-cues-at-
        t=0 .srt file.
        """

        raw_text = None
        for label, text in self.scene_texts:
            if label.lower() == direction.lower():
                raw_text = text
                break
        if raw_text is None:
            return None

        duration_seconds = load_or_compute_duration(self.recording)
        if not duration_seconds:
            return None
        duration_seconds = float(duration_seconds)

        events = extract_description_events(raw_text)
        signs = _parse_sign_reads(raw_text)
        if events or signs:
            events = [
                DescriptionEvent(event.timestamp_seconds, _label_rear_view(event.text, direction))
                for event in events
            ]
            signs = [
                SignRead(sign.timestamp_seconds, _label_rear_view(sign.text, direction))
                for sign in signs
            ]
            srt = build_description_srt_from_events(events, duration_seconds, signs=signs)
            if srt is not None:
                return srt

        description = extract_description_section(raw_text)
        if not description:
            return None

        return build_description_srt(_label_rear_view(description, direction), duration_seconds)

    @property
    def known_filenames(self) -> frozenset[str]:
        """Every real filename this recording actually owns - the
        allow-list the file-serving/thumbnail routes check a
        requested filename against before ever touching the
        filesystem, same pattern as trips.py's
        TripAssets.known_filenames."""

        return frozenset(
            asset_file.name for asset_file in self.recording.assets.values()
        )

    def file_path(self, filename: str) -> Path | None:
        """Resolve an already-allow-listed filename (see
        known_filenames) to its real path, or None if it isn't
        actually one of this recording's own files."""

        for asset_file in self.recording.assets.values():
            if asset_file.name == filename:
                return asset_file.path
        return None

    def thumbnail_path(
        self, direction: str, *, archive_root: Path | None = None
    ) -> Path | None:
        """Resolve a direction name ("front"/"rear"/"interior") to
        its thumbnail file's path, or None if this recording has no
        thumbnail for that direction.

        Four-step fallback chain for "front" (see thumbnail_direction's
        own docstring - it must stay in sync with this):

        1. A real `*_THUMBNAIL` sidecar (every direction, not just
           front) - the fast, common case for a BlackVue archive.
        2. The generated Asset.THUMBNAIL asset (<id>.thumb.jpg) - a
           normal, permanent archive asset like .aac/.duration.txt,
           written either by `bv-generate --thumbnail` ahead of time or
           by step 4 below on an earlier view of this same recording.
           Unlike the old design (see WORKING_CONTEXT.md, "archive-
           browser thumbnails"), this is not a separate app-level cache
           keyed by a source-file fingerprint - it's discovered the
           same way every other generated asset is, via each adapter's
           `asset_suffix_table` (`generated_assets_for()`).
        3. The recording's own FRONT file when it's a photo (see
           archive/photo.py) - the photo itself already *is* a small,
           real preview image, so serving it directly is a real
           thumbnail, no ffmpeg needed.
        4. A fresh frame-grab from the FRONT video, written straight to
           the same permanent `archive_root / f"{id}.thumb.jpg"` path
           step 2 checks - the on-demand fallback for a recording
           bv-generate --thumbnail hasn't reached yet. Only attempted
           when `archive_root` is given - callers with no archive root
           available (existing tests, any future non-web caller) get
           steps 1-3 only, same as before this fallback existed. A
           generation failure (ffmpeg missing, a corrupt or truncated
           video) is swallowed, not raised - one recording's thumbnail
           failing to generate shouldn't take down the whole grid, the
           same "never break the whole page over one recording" posture
           scene_texts already takes for a bad scene.txt read.
        """

        asset = _THUMBNAIL_ASSET_BY_DIRECTION.get(direction)
        if asset is None:
            return None
        asset_file = self.recording.file(asset)
        if asset_file is not None:
            return asset_file.path
        if direction != "front":
            return None
        generated = self.recording.file(Asset.THUMBNAIL)
        if generated is not None:
            return generated.path
        if recording_is_photo(self.recording):
            front = self.recording.file(Asset.FRONT)
            return front.path if front is not None else None
        front = self.recording.file(Asset.FRONT)
        if front is None or archive_root is None:
            return None
        destination = archive_root / f"{self.recording.id}.thumb.jpg"
        try:
            extract_video_thumbnail(front.path, destination)
        except MediaToolError:
            return None
        return destination


def first_valid_gps_fix(adapter: CameraAdapter, recording: Recording) -> GpsFix | None:
    """Return the first fix for `recording` (read via `adapter`, per
    its manifest's gps_source_asset - see adapters/telemetry_bridge.py)
    that has a real position - the location "at the start" of the
    recording, for the archive detail page's "Show start and stop
    location" link. None if there's no valid fix at all (e.g. the
    camera hadn't acquired a GPS signal yet when the clip started -
    common for the first recording after the car's been parked
    somewhere without sky view); a GPS source existing at all (see
    ArchiveRecording.has_gps or telemetry_bridge.recording_has_gps())
    is a separate, weaker guarantee than this actually finding a fix.

    "Valid" matches GpsFix.valid's own definition (a real position
    per the sentence's mode indicator) plus a defensive check that
    latitude/longitude both parsed - read_gps() already skips
    malformed sentences entirely, but a $GPRMC sentence can in
    principle report a valid mode with an unparsed coordinate field,
    so this doesn't assume the two always travel together.
    """

    for fix in read_recording_gps(adapter, recording):
        if fix.valid and fix.latitude is not None and fix.longitude is not None:
            return fix
    return None


def last_valid_gps_fix(adapter: CameraAdapter, recording: Recording) -> GpsFix | None:
    """Return the last fix for `recording` that has a real position -
    the location "at the end" of the recording, for the archive
    detail page's "Show start and stop location" link. Mirrors
    first_valid_gps_fix() exactly, just walking the fixes in reverse -
    see its own docstring for what "valid" means. read_recording_gps()
    already returns fixes in chronological order, so reversed() alone
    is enough - no separate sort needed.
    """

    for fix in reversed(read_recording_gps(adapter, recording)):
        if fix.valid and fix.latitude is not None and fix.longitude is not None:
            return fix
    return None


def kind_options() -> list[tuple[str, str]]:
    """(kind letter, display label) pairs in canonical N/E/M/P/A order
    - what the archive browser's mode-filter checkboxes are built
    from."""

    return [(letter, _KIND_LABELS[letter]) for letter in ("N", "E", "M", "P", "A")]


def filter_recordings(
    recordings: list[ArchiveRecording],
    *,
    modes: Collection[str] | None = None,
    time_interval: TimeInterval | None = None,
    videos_only: bool = False,
) -> list[ArchiveRecording]:
    """Filter an already-scanned recording list by kind letter(s),
    a lexical timestamp range, and/or whether a video actually
    downloaded - the same TimeInterval bv-ls/bv-export/bv-download/
    bv-generate already filter recordings with (see
    lexicaltimeparser.py's LexicalTimeParser), applied here for the
    archive browser's own filter bar instead of a CLI flag.

    `modes=None` means "no mode filter" (every kind shows), not "show
    nothing" - the same convention an unchecked-by-default checkbox
    row implies. `time_interval=None` likewise means no time filter.
    `videos_only=True` hides recordings with no video at all (see
    ArchiveRecording.has_video's docstring on why a recording can have
    a thumbnail but no video) - the "Show only with videos" checkbox
    Christer asked for after the red-cross overlay made those
    recordings visible but still cluttering the grid. Order is
    preserved from the input list (already newest-first from
    scan_archive()), so this can run before or after group_by_day()
    depending on what a caller needs.
    """

    def matches(recording: ArchiveRecording) -> bool:
        if modes is not None and recording.recording.id.kind not in modes:
            return False
        if time_interval is not None and recording.id not in time_interval:
            return False
        if videos_only and not recording.has_video:
            return False
        return True

    return [recording for recording in recordings if matches(recording)]


def scan_archive(
    archive_path: Path, camera_id: str, adapter_id: str = DEFAULT_ADAPTER_ID
) -> list[ArchiveRecording]:
    """Return every recording in a camera's raw archive, newest
    first.

    `adapter_id` selects which CameraAdapter scans `archive_path` (see
    adapters/registry.py) - defaults to "blackvue"
    (DEFAULT_ADAPTER_ID), same as bv-ls. Callers pass the resolved
    camera's own CameraConfig.adapter (see app.py's
    archive_recording_list() route).

    A missing archive directory (e.g. bv-download has never run for
    this camera yet) is treated as zero recordings, not an error -
    the same convention trips.py's scan_trips() uses for a missing
    --target.
    """

    if not archive_path.is_dir():
        return []

    archive = registry.get_adapter(adapter_id).open_archive(archive_path)

    return sorted(
        (
            ArchiveRecording(camera_id=camera_id, recording=recording)
            for recording in archive.recordings
        ),
        key=lambda item: item.recording.id,
        reverse=True,
    )


def find_recording(
    archive_path: Path,
    camera_id: str,
    recording_id: str,
    adapter_id: str = DEFAULT_ADAPTER_ID,
) -> ArchiveRecording | None:
    """Resolve a single recording id within a camera's archive, or
    None if it doesn't exist.

    Uses CameraAdapter.find_recording() - a targeted lookup for just
    this one recording's own files - rather than scan_archive()'s
    full-archive read. This matters a lot here specifically: the
    thumbnail grid calls this once per recording shown on the page,
    and the video player's file-serving route calls it again for
    every HTTP range request while a browser seeks/buffers. Doing
    either of those via a full scan_archive() (which stat()s every
    file across the whole archive on every single call) would make an
    N-recording page load O(N^2), and would make video playback feel
    like it hangs - dozens of range requests, each re-scanning a
    potentially large archive from scratch. See
    CameraAdapter.find_recording()'s own docstring (base.py) for how
    each adapter meets that bar - or doesn't; FolderAdapter's own
    find_recording() falls back to a full rescan, which is a real,
    accepted cost for that kind of archive today, not a regression
    from before this adapter existed (nothing served folder-adapter
    cameras through bv-web at all until this wiring).
    """

    parsed_id = RecordingId.parse(recording_id)
    if parsed_id is None or parsed_id.value != recording_id:
        return None

    if not archive_path.is_dir():
        return None

    recording = registry.get_adapter(adapter_id).find_recording(archive_path, parsed_id)
    if recording is None:
        return None

    return ArchiveRecording(camera_id=camera_id, recording=recording)


class ArchiveRecordingCache:
    """Caches find_recording() results briefly, per (camera_id,
    recording_id) pair - mirrors trips.py's TripCache (see its own
    docstring for the full reasoning) for the same underlying problem
    on the archive-browser side.

    find_recording() is already a targeted single-recording lookup,
    not a full scan_archive() (see its own docstring), but a single
    page view still fires it several times in a burst: once for the
    detail page itself, once for its thumbnail, and then again for
    every HTTP range request while a video plays. Each of those redoes
    the same handful of filesystem calls against the same recording.
    On a LAN where bv-web's Docker host is the NAS rather than the
    machine actually watching the video, that repeated per-request
    cost is what shows up as felt lag on playback and thumbnail loads
    - the same story that motivated TripCache for trip playback.

    A short TTL (default 2 seconds, same as TripCache) keeps this from
    masking a recording bv-download is still in the middle of writing
    - a request just outside the window re-checks the real filesystem.
    Misses are deliberately NOT cached, matching TripCache, so a bad
    id or a recording that hasn't finished downloading yet is
    re-checked on the very next request rather than held as "not
    found" for the TTL.
    """

    def __init__(self, ttl_seconds: float = 2.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[tuple[str, str], tuple[ArchiveRecording, float]] = {}

    def get(
        self,
        archive_path: Path,
        camera_id: str,
        recording_id: str,
        adapter_id: str = DEFAULT_ADAPTER_ID,
    ) -> ArchiveRecording | None:
        now = time.monotonic()
        key = (camera_id, recording_id)

        cached = self._entries.get(key)
        if cached is not None:
            recording, expires_at = cached
            if now < expires_at:
                return recording

        recording = find_recording(archive_path, camera_id, recording_id, adapter_id)
        if recording is not None:
            self._entries[key] = (recording, now + self._ttl_seconds)
        else:
            self._entries.pop(key, None)
        return recording


def group_by_day(
    recordings: list[ArchiveRecording],
) -> list[tuple[date, list[ArchiveRecording]]]:
    """Group already newest-first recordings into (day, recordings)
    pairs, still newest-day-first. Relies on the input already being
    sorted by timestamp descending (scan_archive()'s own contract) -
    itertools.groupby only groups consecutive equal keys, which is
    exactly what a sorted list gives us for free."""

    return [
        (day, list(group))
        for day, group in itertools.groupby(
            recordings, key=lambda item: item.timestamp.date()
        )
    ]
