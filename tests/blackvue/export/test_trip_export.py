import calendar
import json
import re
import struct
import subprocess
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.export import trip_export as trip_export_module
from blackvue.export.osm_roads import Road
from blackvue.export.trip_export import _align_front_rear_durations
from blackvue.export.trip_export import _concatenate_asset
from blackvue.export.trip_export import _ensure_recording_audio
from blackvue.export.trip_export import _merge_gsensor
from blackvue.export.trip_export import _recording_video_offsets
from blackvue.export.trip_export import _repair_parking_sources
from blackvue.export.trip_export import _replace_with_retry
from blackvue.export.trip_export import _trim_prebuffers
from blackvue.export.trip_export import _video_position_breakpoints
from blackvue.export.trip_export import export_trip
from blackvue.export.trip_export import folder_name_for_trip
from blackvue.generate.media import MediaInfo
from blackvue.generate.media import MediaToolError
from blackvue.generate.speech import SpeechSegment
from blackvue.generate.subtitles import format_lrc
from blackvue.generate.subtitles import format_srt
from blackvue.telemetry.gsensor_reader import read_gsensor
from blackvue.trip.trip import Trip

GSENSOR_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "gsensor"


def _make_video(path, duration_seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "testsrc=size=64x64:rate=10",
            "-t", str(duration_seconds),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _video_size(path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return stream["width"], stream["height"]


def _make_video_with_audio(path, duration_seconds: float = 1.0) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"testsrc=size=64x64:rate=10:duration={duration_seconds}",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_seconds}",
            "-c:v", "libx264",
            "-c:a", "aac",
            "-shortest",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_audio(path, duration_seconds: float = 1.0) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_seconds}",
            "-c:a", "aac",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _video_duration(path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def _has_audio_stream(path) -> bool:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(json.loads(result.stdout)["streams"])


def _audio_stream_duration(path) -> float:
    # Unlike _video_duration() (format-level, which for a mixed-
    # duration container just reports the longest stream), this asks
    # specifically for the audio stream's own duration - the number
    # that actually reveals an audio/video sync gap, since a shorter
    # audio track hiding inside a longer-duration container doesn't
    # change the container's own reported total.
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(result.stdout)["streams"][0]["duration"])


def _gsensor_bytes(*records) -> bytes:
    return b"".join(struct.pack(">Ihhh", ms, x, y, z) for ms, x, y, z in records)


def _ffprobe_can_open(path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", str(path)],
        capture_output=True,
    )
    return result.returncode == 0


def _mp4_box(box_type: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _broken_empty_audio_trak() -> bytes:
    # The confirmed real-world shape (see generate/mp4_repair.py's own
    # docstring, and tests/blackvue/generate/test_mp4_repair.py): zero
    # samples, zero chunks, but a stray stsc entry pointing at chunk 0
    # anyway - exactly what trips ffmpeg's "contradictionary STSC and
    # STCO" check.
    empty_stsz = bytes(12)
    empty_stsc = bytearray(8)
    empty_stsc[4:8] = (1).to_bytes(4, "big")
    empty_stsc += (0).to_bytes(4, "big") + (0).to_bytes(4, "big") + (1).to_bytes(4, "big")
    empty_stco = bytes(8)
    empty_stts = bytearray(8)
    empty_stts[4:8] = (1).to_bytes(4, "big")
    empty_stts += (0).to_bytes(4, "big") + (0).to_bytes(4, "big")
    audio_stbl = (
        _mp4_box(b"stsz", empty_stsz)
        + _mp4_box(b"stsc", bytes(empty_stsc))
        + _mp4_box(b"stco", empty_stco)
        + _mp4_box(b"stts", bytes(empty_stts))
    )
    audio_hdlr = bytearray(12)
    audio_hdlr[8:12] = b"soun"
    return _mp4_box(
        b"trak",
        _mp4_box(
            b"mdia",
            _mp4_box(b"hdlr", bytes(audio_hdlr))
            + _mp4_box(b"minf", _mp4_box(b"stbl", audio_stbl)),
        ),
    )


def _find_top_level_box(data: bytes, box_type: bytes) -> tuple[int, int]:
    idx = 0
    while idx < len(data):
        size = int.from_bytes(data[idx:idx + 4], "big")
        if data[idx + 4:idx + 8] == box_type:
            return idx, idx + size
        if size == 0:
            break
        idx += size
    raise ValueError(f"no {box_type!r} box found")


def _write_broken_parking_video(path: Path, duration_seconds: float = 1.0) -> None:
    """Write a *real*, playable video (via ffmpeg, same as
    `_make_video()`) and then splice in a broken, empty audio track
    matching the confirmed real-world Parking-mode container quirk
    (see `_broken_empty_audio_trak()`) - reproducing the actual bug
    end to end: ffprobe/ffmpeg refuse to open the file at all until
    `load_or_repair_parking_video()` drops the bad trak, but the real
    video content underneath (and any further processing of it, e.g.
    concatenation) is completely unaffected by the repair.

    Real ffmpeg output places 'moov' last, after 'mdat' - confirmed
    directly in this sandbox - so appending a trak to it never moves
    'mdat' and never invalidates the real video trak's own 'stco'
    offsets, the easy case `mp4_repair.py` itself already handles.
    """

    _make_video(path, duration_seconds)
    data = path.read_bytes()
    moov_start, moov_end = _find_top_level_box(data, b"moov")
    new_moov = _mp4_box(b"moov", data[moov_start + 8:moov_end] + _broken_empty_audio_trak())
    path.write_bytes(data[:moov_start] + new_moov + data[moov_end:])


def test_folder_name_for_trip_with_and_without_prefix():
    first = Recording(id=RecordingId("20260715_100000_N"))
    last = Recording(id=RecordingId("20260715_100500_N"))
    trip = Trip((first, last))

    assert folder_name_for_trip(trip, None) == (
        "trip_20260715_100000_20260715_100500"
    )
    assert folder_name_for_trip(trip, "Holiday") == (
        "Holiday_trip_20260715_100000_20260715_100500"
    )


def test_folder_name_for_trip_uses_full_boundary_when_parking_is_included():
    # include_parking defaults to True - trip.label's own existing
    # behavior, unchanged - so a leading Parking recording's start
    # still opens the folder name, matching front.mp4/rear.mp4 which
    # will actually include it.
    leading_parking = Recording(id=RecordingId("20260715_093000_P"))
    driving = Recording(id=RecordingId("20260715_100000_N"))
    trip = Trip((leading_parking, driving))

    assert folder_name_for_trip(trip, None) == (
        "trip_20260715_093000_20260715_100000"
    )
    assert folder_name_for_trip(trip, None, include_parking=True) == (
        "trip_20260715_093000_20260715_100000"
    )


def test_folder_name_for_trip_skips_leading_parking_when_excluded():
    # Christer, on a real export: "the name of the trip includes the
    # start of the parking video, but in the [stitch].mp4 the parking
    # is not included unless we specify --include-parking" -
    # include_parking=False (the default bv-export behavior) must
    # make the folder name match what's actually in front.mp4/rear.mp4
    # instead: the first *non*-Parking recording's own start.
    leading_parking = Recording(id=RecordingId("20260715_093000_P"))
    driving = Recording(id=RecordingId("20260715_100000_N"))
    trip = Trip((leading_parking, driving))

    assert folder_name_for_trip(trip, None, include_parking=False) == (
        "trip_20260715_100000_20260715_100000"
    )


def test_folder_name_for_trip_skips_trailing_parking_when_excluded():
    driving = Recording(id=RecordingId("20260715_100000_N"))
    trailing_parking = Recording(id=RecordingId("20260715_110000_P"))
    trip = Trip((driving, trailing_parking))

    assert folder_name_for_trip(trip, None, include_parking=False) == (
        "trip_20260715_100000_20260715_100000"
    )


def test_folder_name_for_trip_uses_real_duration_of_last_non_parking_recording():
    driving_start = Recording(id=RecordingId("20260715_100000_N"))
    driving_end = Recording(id=RecordingId("20260715_100500_N"))
    trailing_parking = Recording(id=RecordingId("20260715_110000_P"))

    def duration(recording):
        return 42.0 if recording is driving_end else None

    trip = Trip(
        (driving_start, driving_end, trailing_parking),
        recording_duration=duration,
    )

    assert folder_name_for_trip(trip, None, include_parking=False) == (
        "trip_20260715_100000_20260715_100542"
    )


def test_folder_name_for_trip_falls_back_to_full_boundary_when_all_parking():
    # No non-Parking recording exists at all - nothing narrower to
    # fall back to, so the full (Parking-inclusive) boundary is used
    # even with include_parking=False.
    only_parking = Recording(id=RecordingId("20260715_093000_P"))
    trip = Trip((only_parking,))

    assert folder_name_for_trip(trip, None, include_parking=False) == (
        folder_name_for_trip(trip, None, include_parking=True)
    )


def test_export_trip_writes_everything_available(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_reverse_geocode", _fake_geocode
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    front_b = source_dir / "front_b.mp4"
    _make_video(front_a, 1.0)
    _make_video(front_b, 1.0)

    gps_a = source_dir / "a.gps"
    gps_a.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )
    gps_b = source_dir / "b.gps"
    gps_b.write_text(
        "[1700000060000]$GPRMC,120100.00,A,4808.038,N,01132.000,E,"
        "12.00,45.00,010124,,,A*6D\n"
    )

    gsensor_a = source_dir / "a.3gf"
    gsensor_a.write_bytes(_gsensor_bytes((0, 1, 2, 3), (100, 4, 5, 6)))
    gsensor_b = source_dir / "b.3gf"
    gsensor_b.write_bytes(_gsensor_bytes((0, 7, 8, 9)))

    transcript_a = source_dir / "a.transcript.txt"
    transcript_a.write_text("First recording speech.", encoding="utf-8")

    first = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={
            Asset.FRONT: AssetFile(Asset.FRONT, front_a),
            Asset.GPS: AssetFile(Asset.GPS, gps_a),
            Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a),
            Asset.TRANSCRIPT: AssetFile(Asset.TRANSCRIPT, transcript_a),
        },
    )
    second = Recording(
        id=RecordingId("20260720_100100_N"),
        assets={
            Asset.FRONT: AssetFile(Asset.FRONT, front_b),
            Asset.GPS: AssetFile(Asset.GPS, gps_b),
            Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_b),
        },
    )
    trip = Trip((first, second))

    result = export_trip(trip, dest_dir)

    assert result.front_video == dest_dir / "front.mp4"
    assert result.front_video.exists()
    assert result.rear_video is None
    assert result.audio is None

    assert result.gpx == dest_dir / "trip.gpx"
    assert result.gpx.exists()

    assert result.gsensor == dest_dir / "trip.3gf"
    samples = read_gsensor(result.gsensor)
    # First recording's samples keep their own offsets (0, 100ms).
    # The second recording's one sample is now rebased by its real
    # position in the concatenated video (front_a's own real duration,
    # ~1s) rather than the 60s gap between the two recordings' ID
    # timestamps - see _recording_video_offsets()'s own docstring for
    # why: front_a/front_b are both only 1s long, nowhere near the 60s
    # apart their filenames claim, so positioning by ID timestamp would
    # place this sample nearly a minute later than where it actually
    # falls in front.mp4.
    assert samples[0].offset == timedelta(milliseconds=0)
    assert samples[1].offset == timedelta(milliseconds=100)
    expected_offset_seconds = _video_duration(front_a)
    assert abs(samples[2].offset.total_seconds() - expected_offset_seconds) < 0.2
    assert samples[2].offset < timedelta(seconds=2)
    assert (samples[2].x, samples[2].y, samples[2].z) == (7, 8, 9)

    assert result.text == (dest_dir / "transcript.txt",)
    assert "First recording speech." in result.text[0].read_text()

    assert result.trip_info == dest_dir / "trip_info.txt"
    trip_info_text = result.trip_info.read_text(encoding="utf-8")
    assert "Duration:" in trip_info_text
    assert "Distance:" in trip_info_text
    assert "Start location: 1 Fake Street, Fake City" in trip_info_text
    assert "End location: 1 Fake Street, Fake City" in trip_info_text

    assert result.warnings == ()


def test_export_trip_info_start_matches_content_when_leading_parking_excluded(
    tmp_path, monkeypatch,
):
    # Christer: "the name of the trip includes the start of the
    # parking video, but in the [stitch].mp4 the parking is not
    # included unless we specify --include-parking." trip_info.txt's
    # own "Started:" line is the same category of claim as the folder
    # name - it must match front.mp4's real content too, not the
    # trip's full (Parking-inclusive) detected boundary.
    monkeypatch.setattr(
        trip_export_module, "load_or_reverse_geocode", _fake_geocode
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    parking_front = source_dir / "parking_front.mp4"
    driving_front = source_dir / "driving_front.mp4"
    _make_video(parking_front, 1.0)
    _make_video(driving_front, 1.0)

    leading_parking = Recording(
        id=RecordingId("20260720_093000_P"),
        assets={Asset.FRONT: AssetFile(Asset.FRONT, parking_front)},
    )
    driving = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={Asset.FRONT: AssetFile(Asset.FRONT, driving_front)},
    )
    trip = Trip((leading_parking, driving))

    # include_parking defaults to False, matching bv-export's own CLI
    # default - the Parking recording is left out of front.mp4.
    result = export_trip(trip, dest_dir)

    assert result.front_video.exists()
    assert round(_video_duration(result.front_video)) == 1  # driving only

    trip_info_text = result.trip_info.read_text(encoding="utf-8")
    assert "Started: 2026-07-20 10:00:00" in trip_info_text
    assert "2026-07-20 09:30:00" not in trip_info_text


def test_export_trip_concatenates_front_rear_audio_independently(
    tmp_path, monkeypatch
):
    # front/rear/audio concatenation now run concurrently (see
    # export_trip()'s comment) - the property that actually matters
    # for correctness, not the threading itself, is that one of them
    # failing doesn't block or lose the other two.
    def _selective_concat(sources, destination, *, video_only=False):
        if destination.name == "front.mp4":
            raise MediaToolError("simulated front failure")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.suffix == ".mp4":
            # A real (tiny) video, not just placeholder bytes - rear
            # ends up the sole video export_trip() probes for subtitle
            # padding once front fails, and a fake file would fail
            # that probe too, adding an unrelated second warning this
            # test isn't about.
            _make_video(destination, 1.0)
        else:
            destination.write_bytes(b"fake-audio")

    monkeypatch.setattr(
        trip_export_module, "concatenate_media", _selective_concat
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    # Real, valid media - not just placeholder bytes. _concatenate_asset()
    # now probes every source with check_readable() before handing
    # anything to concatenate_media() at all (see the corrupted-source
    # skip fix in trip_export.py), so garbage bytes would get dropped
    # right there and never even reach the monkeypatched
    # _selective_concat() this test is actually about.
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    audio_a = source_dir / "audio_a.aac"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)
    _make_audio(audio_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.AUDIO: AssetFile(Asset.AUDIO, audio_a),
            },
        ),
    ))

    result = export_trip(trip, dest_dir)

    assert result.front_video is None
    assert result.rear_video == dest_dir / "rear.mp4"
    assert result.audio == dest_dir / "audio.aac"
    assert result.rear_video.exists()
    assert result.audio.exists()
    assert len(result.warnings) == 1


def test_ensure_recording_audio_extracts_from_video_when_missing(tmp_path):
    # Christer: "Or should it be extracted by bv-export?" - yes, the
    # same recording video-only, no Asset.AUDIO entry should end up
    # with a real <recording>.aac sitting next to its video, and the
    # in-memory Recording should reflect it immediately.
    front = tmp_path / "front.mp4"
    _make_video_with_audio(front, 1.0)

    recording_id = RecordingId("20260101_000000_N")
    recording = Recording(
        id=recording_id,
        assets={Asset.FRONT: AssetFile(Asset.FRONT, front)},
    )
    trip = Trip((recording,))
    warnings: list[str] = []

    _ensure_recording_audio(trip, warnings, None)

    expected = tmp_path / "20260101_000000_N.aac"
    assert expected.exists()
    assert recording.has(Asset.AUDIO)
    assert recording.file(Asset.AUDIO).path == expected
    assert warnings == []


def test_ensure_recording_audio_skips_a_video_with_no_audio_stream(tmp_path):
    # The common "not an error" case: a video with no audio track at
    # all shouldn't be treated as a failure worth warning about - see
    # _ensure_recording_audio()'s own docstring.
    front = tmp_path / "front.mp4"
    _make_video(front, 1.0)

    recording_id = RecordingId("20260101_000000_N")
    recording = Recording(
        id=recording_id,
        assets={Asset.FRONT: AssetFile(Asset.FRONT, front)},
    )
    trip = Trip((recording,))
    warnings: list[str] = []

    _ensure_recording_audio(trip, warnings, None)

    assert not (tmp_path / "20260101_000000_N.aac").exists()
    assert not recording.has(Asset.AUDIO)
    assert warnings == []


def test_ensure_recording_audio_skips_parking_recordings(tmp_path):
    # Matches bv-generate's own _do_extract_audio(), which already
    # refuses to extract audio for Parking recordings - even though
    # this video genuinely has an audio stream, it should never be
    # touched.
    front = tmp_path / "front.mp4"
    _make_video_with_audio(front, 1.0)

    recording_id = RecordingId("20260101_000000_P")
    recording = Recording(
        id=recording_id,
        assets={Asset.FRONT: AssetFile(Asset.FRONT, front)},
    )
    trip = Trip((recording,))
    warnings: list[str] = []

    _ensure_recording_audio(trip, warnings, None)

    assert not (tmp_path / "20260101_000000_P.aac").exists()
    assert not recording.has(Asset.AUDIO)
    assert warnings == []


def test_ensure_recording_audio_skips_a_recording_that_already_has_one(
    tmp_path, monkeypatch
):
    called = []
    monkeypatch.setattr(
        trip_export_module, "extract_audio", lambda *a, **k: called.append(a)
    )

    front = tmp_path / "front.mp4"
    _make_video_with_audio(front, 1.0)
    existing_audio = tmp_path / "existing.aac"
    existing_audio.write_bytes(b"already-here")

    recording = Recording(
        id=RecordingId("20260101_000000_N"),
        assets={
            Asset.FRONT: AssetFile(Asset.FRONT, front),
            Asset.AUDIO: AssetFile(Asset.AUDIO, existing_audio),
        },
    )
    trip = Trip((recording,))
    warnings: list[str] = []

    _ensure_recording_audio(trip, warnings, None)

    assert called == []
    assert recording.file(Asset.AUDIO).path == existing_audio


def test_ensure_recording_audio_warns_on_a_real_extraction_failure(
    tmp_path, monkeypatch
):
    # A recording that DOES have an audio stream (so it gets past the
    # probe_audio_codec() short-circuit) but extract_audio() itself
    # still fails for some other reason (corrupted source, ffmpeg
    # error) - this is the one case that should actually warn.
    from blackvue.generate.media import MediaToolError as MTE

    def _fail(*_a, **_k):
        raise MTE("simulated extraction failure")

    monkeypatch.setattr(trip_export_module, "extract_audio", _fail)

    front = tmp_path / "front.mp4"
    _make_video_with_audio(front, 1.0)

    recording = Recording(
        id=RecordingId("20260101_000000_N"),
        assets={Asset.FRONT: AssetFile(Asset.FRONT, front)},
    )
    trip = Trip((recording,))
    warnings: list[str] = []

    _ensure_recording_audio(trip, warnings, None)

    assert not recording.has(Asset.AUDIO)
    assert len(warnings) == 1
    assert "could not self-heal missing audio" in warnings[0]
    assert "simulated extraction failure" in warnings[0]


def test_ensure_recording_audio_debug_prints_when_healed(tmp_path, capsys):
    front = tmp_path / "front.mp4"
    _make_video_with_audio(front, 1.0)

    recording = Recording(
        id=RecordingId("20260101_000000_N"),
        assets={Asset.FRONT: AssetFile(Asset.FRONT, front)},
    )
    trip = Trip((recording,))
    warnings: list[str] = []

    _ensure_recording_audio(trip, warnings, None, debug=True)

    err = capsys.readouterr().err
    assert "20260101_000000_N" in err
    assert "self-healed" in err


def test_ensure_recording_audio_is_silent_by_default(tmp_path, capsys):
    front = tmp_path / "front.mp4"
    _make_video_with_audio(front, 1.0)

    recording = Recording(
        id=RecordingId("20260101_000000_N"),
        assets={Asset.FRONT: AssetFile(Asset.FRONT, front)},
    )
    trip = Trip((recording,))
    warnings: list[str] = []

    _ensure_recording_audio(trip, warnings, None)

    assert capsys.readouterr().err == ""


def test_export_trip_self_heals_a_recordings_missing_audio(tmp_path):
    # End-to-end: export_trip() itself should produce a trip-level
    # audio.aac from a recording whose video has audio but never had
    # its own <recording>.aac extracted (no earlier `bv-generate
    # --extract-audio` run) - no flag needed.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front = source_dir / "front.mp4"
    _make_video_with_audio(front, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front)},
        ),
    ))

    result = export_trip(trip, dest_dir)

    assert result.audio == dest_dir / "audio.aac"
    assert result.audio.exists()
    assert (source_dir / "20260720_100000_N.aac").exists()
    assert result.warnings == ()


class _StepLog:
    """Minimal TripLog stand-in that just records step()/warning()
    calls in order, for tests that need to check *what* got logged
    (and at what severity) without a real trip.log file on disk. A
    message that reaches warning() is recorded here with the same
    "WARNING: " prefix TripLog.warning() itself adds (see
    trip_log.py), so a test can tell the two apart."""

    def __init__(self):
        self.steps: list[str] = []

    def step(self, message: str, *, elapsed_seconds: float | None = None) -> None:
        self.steps.append(message)

    def warning(self, message: str) -> None:
        self.step(f"WARNING: {message}")


def test_align_front_rear_durations_trims_the_longer_side_and_logs_info(tmp_path):
    # Christer: "front/rear duration differs shouldn't be a warning,
    # just an info" - a successful trim is the expected, routine case
    # (per-camera clock drift), not a problem left unresolved, so it
    # should land in trip.log as a plain step and never surface as a
    # CLI/export-level warning.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front = source_dir / "front.mp4"
    rear = source_dir / "rear.mp4"
    _make_video(front, 10.0)
    _make_video(rear, 2.0)

    recording_id = RecordingId("20260720_100000_N")
    trip = Trip((
        Recording(
            id=recording_id,
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front),
                Asset.REAR: AssetFile(Asset.REAR, rear),
            },
        ),
    ))

    warnings: list[str] = []
    log = _StepLog()
    overrides = _align_front_rear_durations(
        trip, tmp_path / "work", warnings, log=log, include_parking=True
    )

    assert list(overrides.keys()) == [(recording_id, Asset.FRONT)]
    trimmed = overrides[(recording_id, Asset.FRONT)]
    assert trimmed.exists()
    assert _video_duration(trimmed) < 10.0
    assert _video_duration(trimmed) < 3.0
    # Not a warning: nothing added to `warnings`, and the trip.log line
    # went through step(), not warning() (no "WARNING: " prefix).
    assert warnings == []
    assert len(log.steps) == 1
    assert "trimmed front to match rear" in log.steps[0]
    assert not log.steps[0].startswith("WARNING:")


def _truncate_moov_atom(path) -> None:
    """Corrupt an already-written MP4 by cutting it off before its
    trailing moov atom - reproduces the exact "moov atom not found"
    failure Christer hit on a real export (camera power loss mid-write
    is the usual real-world cause)."""

    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 4])


def test_concatenate_asset_skips_a_corrupted_source_and_keeps_the_rest(tmp_path):
    # Christer, on a real export: "ffmpeg concat failed for rear.mp4
    # ... moov atom not found ... 20260731_173318_NR.mp4" - one
    # corrupted recording's rear file took the *entire* rear.mp4 down,
    # discarding otherwise-good footage from every other recording in
    # the trip along with it. _concatenate_asset() now probes each
    # source first and leaves out only the one that's actually broken.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    good = source_dir / "good.mp4"
    corrupted = source_dir / "corrupted.mp4"
    _make_video(good, 1.0)
    _make_video(corrupted, 1.0)
    _truncate_moov_atom(corrupted)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, corrupted)},
        ),
        Recording(
            id=RecordingId("20260720_100100_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, good)},
        ),
    ))

    warnings: list[str] = []
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()
    result = _concatenate_asset(trip, Asset.FRONT, "front.mp4", dest_dir, warnings)

    assert result == dest_dir / "front.mp4"
    assert result.exists()
    assert round(_video_duration(result)) == 1
    assert len(warnings) == 1
    assert "corrupted.mp4" in warnings[0]
    assert "left out" in warnings[0]


def test_concatenate_asset_returns_none_when_every_source_is_corrupted(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    corrupted = source_dir / "corrupted.mp4"
    _make_video(corrupted, 1.0)
    _truncate_moov_atom(corrupted)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, corrupted)},
        ),
    ))

    warnings: list[str] = []
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()
    result = _concatenate_asset(trip, Asset.FRONT, "front.mp4", dest_dir, warnings)

    assert result is None
    assert not (dest_dir / "front.mp4").exists()
    assert len(warnings) == 1


def test_repair_parking_sources_returns_repaired_paths_for_broken_containers(
    tmp_path, monkeypatch,
):
    # Christer, on a real export with --include-parking: front.mp4/
    # rear.mp4 concatenation silently dropped 20230728_105305_PF.mp4/
    # _PR.mp4 with "contradictionary STSC and STCO" - the exact same
    # broken-empty-audio-track container quirk already fixed for
    # bv-web's archive browser (see generate/mp4_repair.py).
    # _repair_parking_sources() is the fix's export-side counterpart:
    # it must find and repair a Parking recording's own broken FRONT/
    # REAR files up front, before anything tries to probe them.
    monkeypatch.setenv("BEYOND_VIDEO_CONFIG_DIR", str(tmp_path / "config"))

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front = source_dir / "20260726_144116_PF.mp4"
    rear = source_dir / "20260726_144116_PR.mp4"
    _write_broken_parking_video(front)
    _write_broken_parking_video(rear)
    assert not _ffprobe_can_open(front)
    assert not _ffprobe_can_open(rear)

    trip = Trip((
        Recording(
            id=RecordingId("20260726_144116_P"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front),
                Asset.REAR: AssetFile(Asset.REAR, rear),
            },
        ),
    ))

    overrides = _repair_parking_sources(trip)

    front_key = (RecordingId("20260726_144116_P"), Asset.FRONT)
    rear_key = (RecordingId("20260726_144116_P"), Asset.REAR)
    assert front_key in overrides
    assert rear_key in overrides
    assert overrides[front_key] != front
    assert overrides[rear_key] != rear
    assert _ffprobe_can_open(overrides[front_key])
    assert _ffprobe_can_open(overrides[rear_key])


def test_repair_parking_sources_skips_non_parking_recordings(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_CONFIG_DIR", str(tmp_path / "config"))

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front = source_dir / "20260726_144116_NF.mp4"
    _make_video(front, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260726_144116_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front)},
        ),
    ))

    assert _repair_parking_sources(trip) == {}


def test_concatenate_asset_uses_repaired_parking_source_instead_of_dropping_it(
    tmp_path, monkeypatch,
):
    # End-to-end version of the bug: a broken Parking source, once
    # repaired by _repair_parking_sources() and threaded through as a
    # duration_override, must actually get concatenated rather than
    # skipped by _concatenate_asset()'s own check_readable() step.
    monkeypatch.setenv("BEYOND_VIDEO_CONFIG_DIR", str(tmp_path / "config"))

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front = source_dir / "20260726_144116_PF.mp4"
    _write_broken_parking_video(front)

    trip = Trip((
        Recording(
            id=RecordingId("20260726_144116_P"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front)},
        ),
    ))

    duration_overrides = _repair_parking_sources(trip)

    warnings: list[str] = []
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()
    result = _concatenate_asset(
        trip, Asset.FRONT, "front.mp4", dest_dir, warnings,
        duration_overrides=duration_overrides,
    )

    assert result == dest_dir / "front.mp4"
    assert result.exists()
    assert warnings == []


def test_align_front_rear_durations_trims_even_a_small_real_difference(tmp_path):
    # Replaces an earlier version of this test, which asserted a 0.2s
    # difference was left alone under a 5-second skip-tolerance.
    # Dropped after a real export came back 8s out of sync overall
    # with no single recording differing by anywhere near 5s - small
    # per-recording differences, each individually "within tolerance,"
    # had simply added up across the whole trip with nothing ever
    # triggering a trim. Christer's call once that surfaced: "Best is
    # to trim every recording" - so any real difference, however
    # small, is now aligned exactly, every time.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front = source_dir / "front.mp4"
    rear = source_dir / "rear.mp4"
    _make_video(front, 3.0)
    _make_video(rear, 3.2)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front),
                Asset.REAR: AssetFile(Asset.REAR, rear),
            },
        ),
    ))

    warnings: list[str] = []
    log = _StepLog()
    overrides = _align_front_rear_durations(
        trip, tmp_path / "work", warnings, log=log, include_parking=True
    )

    assert list(overrides.keys()) == [(RecordingId("20260720_100000_N"), Asset.REAR)]
    # Not a warning (see test_align_front_rear_durations_trims_the_longer_side_and_logs_info) -
    # just a trip.log step.
    assert warnings == []
    assert len(log.steps) == 1
    assert "trimmed rear to match front" in log.steps[0]


def test_align_front_rear_durations_ignores_sub_epsilon_float_noise(tmp_path):
    # Two videos generated identically (same duration, same encode
    # parameters) should probe back as equal, or near enough that the
    # tiny gap is ffprobe's own floating-point rounding rather than a
    # real difference - this shouldn't trigger a pointless trim on
    # every single recording of every export.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front = source_dir / "front.mp4"
    rear = source_dir / "rear.mp4"
    _make_video(front, 3.0)
    _make_video(rear, 3.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front),
                Asset.REAR: AssetFile(Asset.REAR, rear),
            },
        ),
    ))

    warnings: list[str] = []
    overrides = _align_front_rear_durations(
        trip, tmp_path / "work", warnings, log=None, include_parking=True
    )

    assert overrides == {}
    assert warnings == []


def test_align_front_rear_durations_skips_parking_when_not_included(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front = source_dir / "front.mp4"
    rear = source_dir / "rear.mp4"
    _make_video(front, 10.0)
    _make_video(rear, 2.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_P"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front),
                Asset.REAR: AssetFile(Asset.REAR, rear),
            },
        ),
    ))

    warnings: list[str] = []
    overrides = _align_front_rear_durations(
        trip, tmp_path / "work", warnings, log=None, include_parking=False
    )

    # Dropped from the export entirely regardless of any mismatch - no
    # point probing or trimming footage that never reaches
    # front.mp4/rear.mp4.
    assert overrides == {}
    assert warnings == []


def test_align_front_rear_durations_skips_a_recording_missing_one_side(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front = source_dir / "front.mp4"
    _make_video(front, 10.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front)},
        ),
    ))

    warnings: list[str] = []
    overrides = _align_front_rear_durations(
        trip, tmp_path / "work", warnings, log=None, include_parking=True
    )

    assert overrides == {}
    assert warnings == []


def _make_video_with_frequent_keyframes(path, duration_seconds: float) -> None:
    """Like _make_video(), but with a keyframe forced every real
    second instead of libx264's own default GOP - a plain lavfi
    testsrc clip this short only ever gets one keyframe, at the very
    start, and trim_media_head()'s stream-copy input-side seek can
    only land on a real keyframe. Without this, a head trim on a
    clip this short would just seek right back to frame 0 and produce
    the untrimmed video."""

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
            "-t", str(duration_seconds),
            "-g", "10",
            "-force_key_frames", "expr:gte(t,n_forced*1)",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _n_to_m_prebuffer_trip(source_dir):
    """A 2-recording (N -> M) trip built from the real, confirmed
    prebuffer pair in tests/fixtures/gsensor/ (20260802_103513_N /
    20260802_103545_M - see WORKING_CONTEXT.md) - detect_prebuffer_
    seconds() finds a ~5.1s overlap between these two tracks (see
    test_prebuffer.py's own test against this exact pair). FRONT/REAR/
    AUDIO are synthetic (real content doesn't matter for
    _trim_prebuffers(), only the g-sensor tracks do), long enough that
    a ~5.1s head trim leaves a clearly-shorter, still-nonzero result,
    with frequent keyframes so trim_media_head()'s stream-copy seek
    has something to actually land on.
    """

    n_front = source_dir / "n_front.mp4"
    n_rear = source_dir / "n_rear.mp4"
    n_audio = source_dir / "n_audio.aac"
    m_front = source_dir / "m_front.mp4"
    m_rear = source_dir / "m_rear.mp4"
    m_audio = source_dir / "m_audio.aac"

    _make_video_with_frequent_keyframes(n_front, 8.0)
    _make_video_with_frequent_keyframes(n_rear, 8.0)
    _make_audio(n_audio, 8.0)
    _make_video_with_frequent_keyframes(m_front, 20.0)
    _make_video_with_frequent_keyframes(m_rear, 20.0)
    _make_audio(m_audio, 20.0)

    n_id = RecordingId("20260802_103513_N")
    m_id = RecordingId("20260802_103545_M")

    n_recording = Recording(
        id=n_id,
        assets={
            Asset.FRONT: AssetFile(Asset.FRONT, n_front),
            Asset.REAR: AssetFile(Asset.REAR, n_rear),
            Asset.AUDIO: AssetFile(Asset.AUDIO, n_audio),
            Asset.GSENSOR: AssetFile(
                Asset.GSENSOR, GSENSOR_FIXTURES / "20260802_103513_N.3gf"
            ),
        },
    )
    m_recording = Recording(
        id=m_id,
        assets={
            Asset.FRONT: AssetFile(Asset.FRONT, m_front),
            Asset.REAR: AssetFile(Asset.REAR, m_rear),
            Asset.AUDIO: AssetFile(Asset.AUDIO, m_audio),
            Asset.GSENSOR: AssetFile(
                Asset.GSENSOR, GSENSOR_FIXTURES / "20260802_103545_M.3gf"
            ),
        },
    )

    return Trip((n_recording, m_recording)), n_id, m_id


def test_trim_prebuffers_trims_front_rear_audio_and_gsensor(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    trip, n_id, m_id = _n_to_m_prebuffer_trip(source_dir)

    warnings: list[str] = []
    media_overrides, gsensor_overrides, prebuffer_offsets = _trim_prebuffers(
        trip, tmp_path / "work", warnings, log=None
    )

    # Only the M recording gets trimmed - N is the reference, not the
    # thing being cut.
    assert {key[0] for key in media_overrides} == {m_id}
    assert set(media_overrides.keys()) == {
        (m_id, Asset.FRONT), (m_id, Asset.REAR), (m_id, Asset.AUDIO),
    }

    for (_, asset), trimmed_path in media_overrides.items():
        assert trimmed_path.exists()

    m_front_trimmed_duration = _video_duration(media_overrides[(m_id, Asset.FRONT)])
    assert m_front_trimmed_duration < 20.0
    # A ~5.1s detected offset trimmed (snapped to the nearest earlier
    # keyframe, ~1s apart) from a 20s source should land noticeably
    # below 20s but nowhere near fully consumed.
    assert 12.0 < m_front_trimmed_duration < 19.0

    assert set(gsensor_overrides.keys()) == {m_id}
    trimmed_samples = gsensor_overrides[m_id]
    original_samples = read_gsensor(GSENSOR_FIXTURES / "20260802_103545_M.3gf")
    assert len(trimmed_samples) < len(original_samples)
    assert trimmed_samples[0].offset < timedelta(seconds=0.5)

    assert len(warnings) == 1
    assert str(m_id) in warnings[0]
    assert str(n_id) in warnings[0]
    assert "pre-record buffer" in warnings[0]
    assert "front" in warnings[0] and "rear" in warnings[0]
    assert "audio" in warnings[0] and "gsensor" in warnings[0]

    # The same ~5.1s the fixture pair is known to detect (see
    # test_prebuffer.py) - _video_position_breakpoints() needs this to
    # keep map.mp4/subtitle timing lined up with the trimmed video.
    assert set(prebuffer_offsets.keys()) == {m_id}
    assert 5.0 <= prebuffer_offsets[m_id] <= 5.2


def test_trim_prebuffers_leaves_the_preceding_recording_untouched(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    trip, n_id, m_id = _n_to_m_prebuffer_trip(source_dir)

    warnings: list[str] = []
    media_overrides, gsensor_overrides, prebuffer_offsets = _trim_prebuffers(
        trip, tmp_path / "work", warnings, log=None
    )

    assert (n_id, Asset.FRONT) not in media_overrides
    assert (n_id, Asset.REAR) not in media_overrides
    assert (n_id, Asset.AUDIO) not in media_overrides
    assert n_id not in gsensor_overrides


def test_trim_prebuffers_skips_an_event_recording_that_starts_the_trip(tmp_path):
    # Christer: "A trip that starts with an E or M mode should not be
    # trimmed" - there's nothing earlier in the trip to compare
    # against, so a trip whose own first recording is Manual/Event
    # should never be touched, even if a real prebuffer overlap
    # technically exists against some other recording outside this
    # trip.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()

    m_front = source_dir / "m_front.mp4"
    _make_video_with_frequent_keyframes(m_front, 10.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260802_103545_M"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, m_front),
                Asset.GSENSOR: AssetFile(
                    Asset.GSENSOR, GSENSOR_FIXTURES / "20260802_103545_M.3gf"
                ),
            },
        ),
    ))

    warnings: list[str] = []
    media_overrides, gsensor_overrides, prebuffer_offsets = _trim_prebuffers(
        trip, tmp_path / "work", warnings, log=None
    )

    assert media_overrides == {}
    assert gsensor_overrides == {}
    assert warnings == []


def test_trim_prebuffers_never_touches_a_normal_recording(tmp_path):
    # Even if two consecutive Normal recordings' g-sensor tracks
    # happened to correlate strongly (e.g. genuinely identical driving
    # conditions), only an Event/Manual recording is ever a candidate
    # for trimming - kind is checked before detection ever runs.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()

    first_front = source_dir / "first_front.mp4"
    second_front = source_dir / "second_front.mp4"
    _make_video_with_frequent_keyframes(first_front, 8.0)
    _make_video_with_frequent_keyframes(second_front, 8.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260802_103513_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, first_front),
                Asset.GSENSOR: AssetFile(
                    Asset.GSENSOR, GSENSOR_FIXTURES / "20260802_103513_N.3gf"
                ),
            },
        ),
        Recording(
            id=RecordingId("20260802_103545_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, second_front),
                Asset.GSENSOR: AssetFile(
                    Asset.GSENSOR, GSENSOR_FIXTURES / "20260802_103545_M.3gf"
                ),
            },
        ),
    ))

    warnings: list[str] = []
    media_overrides, gsensor_overrides, prebuffer_offsets = _trim_prebuffers(
        trip, tmp_path / "work", warnings, log=None
    )

    assert media_overrides == {}
    assert gsensor_overrides == {}
    assert warnings == []


def test_trim_prebuffers_skips_when_gsensor_data_is_missing(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()

    n_front = source_dir / "n_front.mp4"
    m_front = source_dir / "m_front.mp4"
    _make_video_with_frequent_keyframes(n_front, 8.0)
    _make_video_with_frequent_keyframes(m_front, 20.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260802_103513_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, n_front)},
        ),
        Recording(
            id=RecordingId("20260802_103545_M"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, m_front),
                Asset.GSENSOR: AssetFile(
                    Asset.GSENSOR, GSENSOR_FIXTURES / "20260802_103545_M.3gf"
                ),
            },
        ),
    ))

    warnings: list[str] = []
    media_overrides, gsensor_overrides, prebuffer_offsets = _trim_prebuffers(
        trip, tmp_path / "work", warnings, log=None
    )

    assert media_overrides == {}
    assert gsensor_overrides == {}
    assert warnings == []


def test_trim_prebuffers_skips_when_no_confident_overlap_is_detected(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()

    n_front = source_dir / "n_front.mp4"
    m_front = source_dir / "m_front.mp4"
    _make_video_with_frequent_keyframes(n_front, 8.0)
    _make_video_with_frequent_keyframes(m_front, 8.0)

    # Two unrelated synthetic g-sensor tracks (see test_prebuffer.py's
    # own "unrelated tracks" test) - no real overlap, so
    # detect_prebuffer_seconds() should refuse to guess.
    n_gsensor = source_dir / "n.3gf"
    m_gsensor = source_dir / "m.3gf"
    n_gsensor.write_bytes(
        b"".join(
            struct.pack(">Ihhh", i * 100, (i * 37) % 200, (i * 11) % 200, (i * 53) % 200)
            for i in range(80)
        )
    )
    m_gsensor.write_bytes(
        b"".join(
            struct.pack(">Ihhh", i * 100, (i * 91) % 200, (i * 29) % 200, (i * 7) % 200)
            for i in range(80)
        )
    )

    trip = Trip((
        Recording(
            id=RecordingId("20260802_103513_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, n_front),
                Asset.GSENSOR: AssetFile(Asset.GSENSOR, n_gsensor),
            },
        ),
        Recording(
            id=RecordingId("20260802_103545_M"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, m_front),
                Asset.GSENSOR: AssetFile(Asset.GSENSOR, m_gsensor),
            },
        ),
    ))

    warnings: list[str] = []
    media_overrides, gsensor_overrides, prebuffer_offsets = _trim_prebuffers(
        trip, tmp_path / "work", warnings, log=None
    )

    assert media_overrides == {}
    assert gsensor_overrides == {}
    assert warnings == []


def test_align_front_rear_durations_uses_source_overrides(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    # The recording's own "real" files already match - no alignment
    # would fire against these directly.
    front = source_dir / "front.mp4"
    rear = source_dir / "rear.mp4"
    _make_video(front, 5.0)
    _make_video(rear, 5.0)

    # But an earlier prebuffer trim (source_overrides) left FRONT
    # shorter than REAR - alignment should trim from *these* files,
    # not notice the untouched originals already matched.
    front_override = tmp_path / "front_prebuffer.mp4"
    _make_video(front_override, 2.0)

    recording_id = RecordingId("20260802_103545_M")
    trip = Trip((
        Recording(
            id=recording_id,
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front),
                Asset.REAR: AssetFile(Asset.REAR, rear),
            },
        ),
    ))

    warnings: list[str] = []
    log = _StepLog()
    overrides = _align_front_rear_durations(
        trip, tmp_path / "work", warnings, log=log, include_parking=True,
        source_overrides={(recording_id, Asset.FRONT): front_override},
    )

    assert list(overrides.keys()) == [(recording_id, Asset.REAR)]
    # Not a warning (see test_align_front_rear_durations_trims_the_longer_side_and_logs_info).
    assert warnings == []
    assert "trimmed rear to match front" in log.steps[0]
    trimmed_rear_duration = _video_duration(overrides[(recording_id, Asset.REAR)])
    assert trimmed_rear_duration < 3.0


def test_merge_gsensor_uses_gsensor_overrides_when_present(tmp_path):
    first_id = RecordingId("20260720_100000_N")

    # The recording's own real .3gf file would give offset(0)=0 - the
    # override should be used instead, not this file.
    real_gsensor = tmp_path / "real.3gf"
    real_gsensor.write_bytes(_gsensor_bytes((0, 1, 2, 3)))

    trip = Trip((
        Recording(id=first_id, assets={Asset.GSENSOR: AssetFile(Asset.GSENSOR, real_gsensor)}),
    ))

    from blackvue.telemetry.gsensor_reader import GSensorSample

    override_samples = (GSensorSample(offset=timedelta(seconds=0), x=9, y=9, z=9),)

    samples = _merge_gsensor(
        trip, {first_id: 0.0}, {first_id: override_samples},
    )

    assert samples == override_samples


def test_recording_video_offsets_uses_real_video_duration_not_id_gap(tmp_path):
    # The bug this whole feature fixes: two recordings 60s apart by ID
    # timestamp, but each only 1s of real video - _concatenate_asset()
    # glues them back to back with no gap filler, so the second one's
    # real position in the video is ~1s, not 60s.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front_a = source_dir / "front_a.mp4"
    front_b = source_dir / "front_b.mp4"
    _make_video(front_a, 1.0)
    _make_video(front_b, 1.0)

    first_id = RecordingId("20260720_100000_N")
    second_id = RecordingId("20260720_100100_N")
    trip = Trip((
        Recording(id=first_id, assets={Asset.FRONT: AssetFile(Asset.FRONT, front_a)}),
        Recording(id=second_id, assets={Asset.FRONT: AssetFile(Asset.FRONT, front_b)}),
    ))

    offsets, total = _recording_video_offsets(trip, include_parking=True)

    assert offsets[first_id] == 0.0
    # Real front_a duration, not the 60s ID-timestamp gap.
    assert abs(offsets[second_id] - _video_duration(front_a)) < 0.2
    assert offsets[second_id] < 2.0
    # The trip's own real total video duration - see this function's
    # own docstring for why it's a plain sum of each recording's own
    # individually-probed duration (front_a + front_b here), not
    # anything read off a concatenated file's own container metadata
    # (there is no concatenated file in this test at all).
    assert abs(total - (_video_duration(front_a) + _video_duration(front_b))) < 0.2


def test_recording_video_offsets_uses_trimmed_duration_override(tmp_path):
    # A recording present in duration_overrides (front/rear alignment
    # trimmed its front down) should be positioned by the *trimmed*
    # duration, not the original untrimmed file's own duration -
    # otherwise the offset wouldn't match what actually landed in
    # front.mp4.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front_a = source_dir / "front_a.mp4"
    front_b = source_dir / "front_b.mp4"
    trimmed_a = tmp_path / "trimmed_a.mp4"
    _make_video(front_a, 5.0)
    _make_video(front_b, 1.0)
    _make_video(trimmed_a, 2.0)

    first_id = RecordingId("20260720_100000_N")
    second_id = RecordingId("20260720_100100_N")
    trip = Trip((
        Recording(id=first_id, assets={Asset.FRONT: AssetFile(Asset.FRONT, front_a)}),
        Recording(id=second_id, assets={Asset.FRONT: AssetFile(Asset.FRONT, front_b)}),
    ))

    offsets, total = _recording_video_offsets(
        trip, include_parking=True,
        duration_overrides={(first_id, Asset.FRONT): trimmed_a},
    )

    assert abs(offsets[second_id] - 2.0) < 0.2
    # Total also reflects the trimmed (2.0s) duration for first_id,
    # not front_a's own untrimmed 5.0s.
    assert abs(total - 3.0) < 0.2


def test_recording_video_offsets_skips_parking_when_not_included(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front_a = source_dir / "front_a.mp4"
    front_p = source_dir / "front_p.mp4"
    front_b = source_dir / "front_b.mp4"
    _make_video(front_a, 1.0)
    _make_video(front_p, 3.0)
    _make_video(front_b, 1.0)

    first_id = RecordingId("20260720_100000_N")
    parking_id = RecordingId("20260720_100010_P")
    second_id = RecordingId("20260720_100100_N")
    trip = Trip((
        Recording(id=first_id, assets={Asset.FRONT: AssetFile(Asset.FRONT, front_a)}),
        Recording(id=parking_id, assets={Asset.FRONT: AssetFile(Asset.FRONT, front_p)}),
        Recording(id=second_id, assets={Asset.FRONT: AssetFile(Asset.FRONT, front_b)}),
    ))

    offsets, total = _recording_video_offsets(trip, include_parking=False)

    assert parking_id not in offsets
    # second_id's offset skips right over the parking recording's own
    # 3s duration, since it never reaches front.mp4 at all.
    assert abs(offsets[second_id] - _video_duration(front_a)) < 0.2
    # Total also excludes the excluded parking recording's 3s.
    assert abs(total - (_video_duration(front_a) + _video_duration(front_b))) < 0.2


def test_recording_video_offsets_skips_a_recording_with_no_video(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    front_a = source_dir / "front_a.mp4"
    _make_video(front_a, 1.0)

    first_id = RecordingId("20260720_100000_N")
    gps_only_id = RecordingId("20260720_100100_N")
    trip = Trip((
        Recording(id=first_id, assets={Asset.FRONT: AssetFile(Asset.FRONT, front_a)}),
        Recording(id=gps_only_id, assets={}),
    ))

    offsets, total = _recording_video_offsets(trip, include_parking=True)

    assert first_id in offsets
    assert gps_only_id not in offsets
    # Total reflects only the one recording with a real video.
    assert abs(total - _video_duration(front_a)) < 0.2


def test_video_position_breakpoints_sorted_by_position():
    first_id = RecordingId("20260720_100000_N")
    second_id = RecordingId("20260720_100100_N")
    trip = Trip((
        Recording(id=first_id),
        Recording(id=second_id),
    ))

    breakpoints = _video_position_breakpoints(
        trip, {second_id: 5.0, first_id: 0.0}
    )

    assert breakpoints == (
        (0.0, first_id.timestamp),
        (5.0, second_id.timestamp),
    )


def test_video_position_breakpoints_omits_recordings_without_an_offset():
    first_id = RecordingId("20260720_100000_N")
    second_id = RecordingId("20260720_100100_N")
    trip = Trip((
        Recording(id=first_id),
        Recording(id=second_id),
    ))

    breakpoints = _video_position_breakpoints(trip, {first_id: 0.0})

    assert breakpoints == ((0.0, first_id.timestamp),)


def test_video_position_breakpoints_shifts_a_trimmed_recordings_wallclock_start():
    # A trimmed Manual/Event recording's video frame 0 no longer lines
    # up with its own ID timestamp - it's been moved forward in
    # wall-clock terms by however much prebuffer got cut off the
    # front. Caught on a real export: without this, map.mp4's
    # displayed position for the trimmed recording lagged its own
    # burned-in camera timestamp by close to the trimmed amount.
    first_id = RecordingId("20260802_103513_N")
    second_id = RecordingId("20260802_103545_M")
    trip = Trip((
        Recording(id=first_id),
        Recording(id=second_id),
    ))

    breakpoints = _video_position_breakpoints(
        trip, {first_id: 0.0, second_id: 8.0}, {second_id: 5.1}
    )

    assert breakpoints == (
        (0.0, first_id.timestamp),
        (8.0, second_id.timestamp + timedelta(seconds=5.1)),
    )


def test_video_position_breakpoints_leaves_an_untrimmed_recording_alone():
    # A recording missing from prebuffer_offsets (the overwhelming
    # majority - only a trimmed Event/Manual recording is ever in it)
    # keeps its plain ID timestamp, same as when prebuffer_offsets
    # isn't given at all.
    first_id = RecordingId("20260720_100000_N")
    second_id = RecordingId("20260720_100100_N")
    trip = Trip((
        Recording(id=first_id),
        Recording(id=second_id),
    ))

    breakpoints = _video_position_breakpoints(
        trip, {second_id: 5.0, first_id: 0.0}, {}
    )

    assert breakpoints == (
        (0.0, first_id.timestamp),
        (5.0, second_id.timestamp),
    )


def test_merge_gsensor_positions_by_video_offset_when_available(tmp_path):
    first_id = RecordingId("20260720_100000_N")
    second_id = RecordingId("20260720_100100_N")

    gsensor_a = tmp_path / "a.3gf"
    gsensor_b = tmp_path / "b.3gf"
    gsensor_a.write_bytes(_gsensor_bytes((0, 1, 2, 3)))
    gsensor_b.write_bytes(_gsensor_bytes((0, 4, 5, 6)))

    trip = Trip((
        Recording(id=first_id, assets={Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a)}),
        Recording(id=second_id, assets={Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_b)}),
    ))

    # Real video position (2.5s), deliberately far from the 60s
    # ID-timestamp gap - if this is used, samples[1].offset should
    # reflect it, not the ID gap.
    samples = _merge_gsensor(trip, {first_id: 0.0, second_id: 2.5})

    assert samples[0].offset == timedelta(seconds=0)
    assert samples[1].offset == timedelta(seconds=2.5)


def test_merge_gsensor_falls_back_to_id_timestamp_gap_without_video_offsets(tmp_path):
    # No video at all for this trip (e.g. a GPS/g-sensor-only export) -
    # video_offsets is empty/None, so the old wall-clock-based rebase
    # is still the right fallback.
    first_id = RecordingId("20260720_100000_N")
    second_id = RecordingId("20260720_100100_N")

    gsensor_a = tmp_path / "a.3gf"
    gsensor_b = tmp_path / "b.3gf"
    gsensor_a.write_bytes(_gsensor_bytes((0, 1, 2, 3)))
    gsensor_b.write_bytes(_gsensor_bytes((0, 4, 5, 6)))

    trip = Trip((
        Recording(id=first_id, assets={Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a)}),
        Recording(id=second_id, assets={Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_b)}),
    ))

    samples_no_arg = _merge_gsensor(trip)
    samples_empty_dict = _merge_gsensor(trip, {})

    for samples in (samples_no_arg, samples_empty_dict):
        assert samples[0].offset == timedelta(seconds=0)
        assert samples[1].offset == timedelta(seconds=60)


def test_export_trip_aligns_a_mismatched_front_rear_recording(tmp_path):
    # End-to-end: a corrupted/truncated download (Christer's real
    # case - see WORKING_CONTEXT.md) left one recording's front video
    # much shorter than its rear. export_trip() should trim rear down
    # to match, keeping front.mp4/rear.mp4 in sync for the rest of the
    # trip, rather than silently drifting out of sync from this
    # recording onward. Not a warning, though - Christer: "front/rear
    # duration differs shouldn't be a warning, just an info" - this is
    # the routine, expected, fully-handled case, so it's recorded in
    # trip.log only, never surfaced via result.warnings.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    front = source_dir / "front.mp4"
    rear = source_dir / "rear.mp4"
    _make_video(front, 2.0)
    _make_video(rear, 10.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front),
                Asset.REAR: AssetFile(Asset.REAR, rear),
            },
        ),
    ))

    result = export_trip(trip, dest_dir)

    assert result.warnings == ()
    assert abs(_video_duration(result.front_video) - 2.0) < 0.5
    assert _video_duration(result.rear_video) < 3.0

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    assert "trimmed rear to match front" in log_text
    assert "WARNING" not in log_text


def _parking_trip(source_dir, *, with_audio: bool = False):
    """A 3-recording trip - drive, park, drive - with a real (long)
    Parking-mode video in the middle, for --include-parking's
    skip-and-replace tests below. The middle recording's own video is
    deliberately much longer than the two flanking ones (mimicking a
    real Parking timelapse's own played-back length) so a test can
    tell "still the real Parking footage" (long) apart from "swapped
    for the 3-second transition clip" (short) just from the resulting
    front.mp4's own duration.
    """

    front_a = source_dir / "front_a.mp4"
    front_p = source_dir / "front_p.mp4"
    front_b = source_dir / "front_b.mp4"
    _make_video(front_a, 1.0)
    _make_video(front_p, 6.0)
    _make_video(front_b, 1.0)

    assets_a = {Asset.FRONT: AssetFile(Asset.FRONT, front_a)}
    assets_p = {Asset.FRONT: AssetFile(Asset.FRONT, front_p)}
    assets_b = {Asset.FRONT: AssetFile(Asset.FRONT, front_b)}

    if with_audio:
        audio_a = source_dir / "audio_a.aac"
        audio_p = source_dir / "audio_p.aac"
        audio_b = source_dir / "audio_b.aac"
        _make_audio(audio_a, 1.0)
        _make_audio(audio_p, 6.0)
        _make_audio(audio_b, 1.0)
        assets_a[Asset.AUDIO] = AssetFile(Asset.AUDIO, audio_a)
        assets_p[Asset.AUDIO] = AssetFile(Asset.AUDIO, audio_p)
        assets_b[Asset.AUDIO] = AssetFile(Asset.AUDIO, audio_b)

    first = Recording(id=RecordingId("20260720_100000_N"), assets=assets_a)
    middle = Recording(id=RecordingId("20260720_100010_P"), assets=assets_p)
    last = Recording(id=RecordingId("20260720_100100_N"), assets=assets_b)
    return Trip((first, middle, last))


def test_export_trip_drops_a_mid_trip_parking_recording_entirely(tmp_path):
    # Replaces an earlier version of this test, which asserted the
    # mid-trip Parking recording was swapped for a short synthetic
    # transition clip. That approach was dropped after a real export
    # from Christer's own 4K HEVC dashcam showed the splice corrupting
    # front.mp4/rear.mp4 from that point onward - any two files from
    # separate encoder sessions can carry incompatible MP4-container
    # -level parameter sets, and ffmpeg's concat demuxer (a stream
    # copy, not a re-encode) doesn't validate that before muxing them
    # together. See WORKING_CONTEXT.md for the full root-cause writeup.
    # Christer: "Just skip it altogether" - now a mid-trip Parking
    # recording is simply left out, the same as a leading/trailing one
    # already was.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _parking_trip(source_dir)

    result = export_trip(trip, dest_dir)

    assert result.warnings == ()
    # 1s + 1s, not 1s + 6s + 1s - the real (6s) Parking footage was
    # left out entirely, with nothing substituted in its place.
    assert abs(_video_duration(result.front_video) - 2.0) < 0.5


def test_export_trip_include_parking_keeps_the_real_parking_footage(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _parking_trip(source_dir)

    result = export_trip(trip, dest_dir, include_parking=True)

    assert result.warnings == ()
    # The original, unmodified behavior: every recording's own video,
    # unconditionally - 1s + 6s + 1s, the real Parking footage kept.
    assert abs(_video_duration(result.front_video) - 8.0) < 0.5


def test_export_trip_drops_a_parking_recording_at_the_trip_start(tmp_path):
    # Replaces an earlier version of this test, which asserted a
    # leading Parking recording was always left untouched regardless
    # of `include_parking`. Christer, once mid-trip Parking recordings
    # were also being dropped rather than replaced with a placeholder
    # (see test_export_trip_drops_a_mid_trip_parking_recording_entirely
    # above): "I think they should be dropped ... Usually a N/M/P
    # ta[k]es a couple of minutes to activate parking mode, so we will
    # probably have a good ending any way. In the beginning we might
    # miss a couple of seconds before it exits P mode." - so a leading
    # Parking recording is now dropped too, the same as any other.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_p = source_dir / "front_p.mp4"
    front_b = source_dir / "front_b.mp4"
    _make_video(front_p, 6.0)
    _make_video(front_b, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_P"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front_p)},
        ),
        Recording(
            id=RecordingId("20260720_100100_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front_b)},
        ),
    ))

    result = export_trip(trip, dest_dir)

    assert result.warnings == ()
    # Only front_b (1s) - front_p (6s) dropped entirely.
    assert abs(_video_duration(result.front_video) - 1.0) < 0.5


def test_export_trip_drops_a_parking_recording_at_the_trip_end(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    front_p = source_dir / "front_p.mp4"
    _make_video(front_a, 1.0)
    _make_video(front_p, 6.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front_a)},
        ),
        Recording(
            id=RecordingId("20260720_100100_P"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front_p)},
        ),
    ))

    result = export_trip(trip, dest_dir)

    assert result.warnings == ()
    # Only front_a (1s) - front_p (6s) dropped entirely.
    assert abs(_video_duration(result.front_video) - 1.0) < 0.5


def test_export_trip_drops_a_skipped_parking_recordings_audio_too(tmp_path):
    # Replaces an earlier version of this test, which asserted a
    # matching-length silent clip was swapped in for the skipped
    # recording's own audio.aac contribution, to keep it in sync with
    # the (then-substituted) video. With no video substitute anymore
    # either, there's nothing to keep in sync with - the recording's
    # audio is simply left out too, same as its video.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _parking_trip(source_dir, with_audio=True)

    result = export_trip(trip, dest_dir)

    assert result.warnings == ()
    assert result.audio is not None
    assert result.audio.exists()
    decoded = dest_dir / "audio_decoded.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(result.audio), str(decoded)],
        capture_output=True, text=True, check=True,
    )
    # 1s + 1s, not 1s + 6s + 1s - the middle recording's own audio
    # dropped along with its video, nothing substituted.
    assert abs(_video_duration(decoded) - 2.0) < 0.5


def test_export_trip_skips_missing_assets_cleanly(tmp_path):
    dest_dir = tmp_path / "export"
    trip = Trip((Recording(id=RecordingId("20260720_100000_N")),))

    result = export_trip(trip, dest_dir)

    assert result.front_video is None
    assert result.rear_video is None
    assert result.audio is None
    assert result.gpx is None
    assert result.gsensor is None
    assert result.text == ()
    assert dest_dir.exists()


def _epoch_ms(timestamp: datetime) -> int:
    """Convert a naive datetime (RecordingId.timestamp/GpsFix.timestamp
    - see gps_reader.py's own docstring on why the two are directly
    comparable, both "UTC-equivalent" naive datetimes) into the Unix
    epoch milliseconds a raw .gps file's own [timestamp] bracket would
    encode for it - so a fixture's GPS fix timestamps land at a
    realistic offset from its recording's own filename timestamp,
    rather than some unrelated fixed epoch. That distinction matters
    for anything exercising render_map_video()'s trip-start-anchored
    timeline (see map_video.py) - an arbitrary, unrelated GPS epoch
    would make the trip's real start look wildly earlier/later than
    every GPS fix, not just "GPS data starts a bit into the trip".
    """

    return calendar.timegm(timestamp.timetuple()) * 1000


def _trip_with_two_gps_fixes(source_dir, monkeypatch):
    # Every caller of this fixture builds a trip with real, positioned
    # GPS data - trip_info.txt's reverse-geocoding lookups (see
    # trip_export.py) fire unconditionally whenever that's true,
    # regardless of --map/--stitch-map, so this is mocked out here
    # once rather than separately in every test that uses this
    # fixture - the same reason load_or_fetch_roads is mocked
    # separately by each test that actually needs a real-looking
    # response (roads aren't fetched unconditionally, so that one
    # can't be hoisted the same way).
    monkeypatch.setattr(
        trip_export_module, "load_or_reverse_geocode", _fake_geocode
    )

    first_id = RecordingId("20260720_100000_N")
    second_id = RecordingId("20260720_100010_N")

    gps_a = source_dir / "a.gps"
    gps_a.write_text(
        f"[{_epoch_ms(first_id.timestamp)}]$GPRMC,120000.00,A,4807.038,N,"
        "01131.000,E,10.00,45.00,010124,,,A*6D\n"
    )
    gps_b = source_dir / "b.gps"
    gps_b.write_text(
        f"[{_epoch_ms(second_id.timestamp)}]$GPRMC,120010.00,A,4808.038,N,"
        "01132.000,E,12.00,45.00,010124,,,A*6D\n"
    )

    first = Recording(
        id=first_id,
        assets={Asset.GPS: AssetFile(Asset.GPS, gps_a)},
    )
    second = Recording(
        id=second_id,
        assets={Asset.GPS: AssetFile(Asset.GPS, gps_b)},
    )
    return Trip((first, second))


def _fake_roads(*_args, **_kwargs):
    return (Road(points=((48.07, 11.31), (48.08, 11.32))),)


def _fake_areas(*_args, **_kwargs):
    # load_or_fetch_areas() is called unconditionally alongside
    # load_or_fetch_roads() whenever a map bbox is resolved (see
    # trip_export.py's own bbox/roads/areas helper) - every test that
    # mocks _fake_roads to avoid a real Overpass roads call needs this
    # mocked too, for the same reason, or it hits a real (and in a
    # network-isolated environment, failing) Overpass areas call
    # instead. An empty tuple is a legitimate "no water/green areas
    # here" result, not a failure - render_map_video() already treats
    # that as a no-op.
    return ()


def _fake_geocode(*_args, **_kwargs):
    # A stand-in for load_or_reverse_geocode - trip_info.txt's address
    # lines are generated unconditionally whenever a trip has >=2
    # positioned GPS fixes (see trip_export.py), independent of
    # --map/--stitch-map, so any test whose fixture has real GPS data
    # needs this mocked out the same way load_or_fetch_roads already
    # is - otherwise it's a real, slow, network-dependent call in
    # every such test, not just the ones actually testing geocoding.
    return "1 Fake Street, Fake City"


def test_export_trip_skips_map_by_default(tmp_path, monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise AssertionError("should not fetch roads when render_map=False")

    monkeypatch.setattr(trip_export_module, "load_or_fetch_roads", _refuse)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir, monkeypatch)

    result = export_trip(trip, dest_dir)

    assert result.map is None
    assert not (dest_dir / "map.mp4").exists()


def test_export_trip_render_map_produces_a_video(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_areas", _fake_areas
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir, monkeypatch)

    result = export_trip(trip, dest_dir, render_map=True)

    assert result.map == dest_dir / "map.mp4"
    assert result.map.exists()
    assert result.warnings == ()


def test_export_trip_render_map_zoom_produces_a_separate_file_alongside_map(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_areas", _fake_areas
    )

    calls = []

    def _capture_zoom(fixes, roads, bbox, destination, **kwargs):
        calls.append((destination, kwargs))
        return destination

    monkeypatch.setattr(
        trip_export_module, "render_map_video", _capture_zoom
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir, monkeypatch)

    result = export_trip(trip, dest_dir, render_map=True, map_zoom_meters=75.0)

    # Two separate renders: the static map.mp4 (no zoom_meters) and its
    # own map_zoom_75m.mp4 (zoom_meters=75.0) - not one video reused
    # for both.
    assert len(calls) == 2
    destinations = {destination for destination, _kwargs in calls}
    assert destinations == {
        dest_dir / "map.mp4", dest_dir / "map_zoom_75m.mp4",
    }
    zoom_kwargs = next(
        kwargs for destination, kwargs in calls
        if destination == dest_dir / "map_zoom_75m.mp4"
    )
    assert zoom_kwargs["zoom_meters"] == 75.0
    static_kwargs = next(
        kwargs for destination, kwargs in calls
        if destination == dest_dir / "map.mp4"
    )
    assert static_kwargs["zoom_meters"] is None

    assert result.map == dest_dir / "map.mp4"
    assert result.map_zoom == dest_dir / "map_zoom_75m.mp4"


def test_export_trip_render_map_zoom_alone_skips_the_static_map(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_areas", _fake_areas
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir, monkeypatch)

    result = export_trip(trip, dest_dir, map_zoom_meters=120.0)

    assert result.map is None
    assert not (dest_dir / "map.mp4").exists()
    assert result.map_zoom == dest_dir / "map_zoom_120m.mp4"
    assert result.map_zoom.exists()


def test_export_trip_formats_the_map_zoom_filename_without_a_trailing_zero(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_areas", _fake_areas
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir, monkeypatch)

    result = export_trip(trip, dest_dir, map_zoom_meters=75.5)

    assert result.map_zoom == dest_dir / "map_zoom_75.5m.mp4"


def test_export_trip_render_map_defaults_cache_dir_next_to_destination(
    tmp_path, monkeypatch
):
    captured = []

    def _capture_cache_dir(bbox, cache_dir, **_kwargs):
        captured.append(cache_dir)
        return _fake_roads()

    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _capture_cache_dir
    )
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_areas", _fake_areas
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "target" / "trip_folder"
    trip = _trip_with_two_gps_fixes(source_dir, monkeypatch)

    export_trip(trip, dest_dir, render_map=True)

    assert captured == [dest_dir.parent / ".osm_cache"]


def test_export_trip_render_map_warns_instead_of_failing_on_fetch_error(
    tmp_path, monkeypatch
):
    def _broken(*_args, **_kwargs):
        raise MediaToolError("could not reach the Overpass API")

    monkeypatch.setattr(trip_export_module, "load_or_fetch_roads", _broken)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir, monkeypatch)

    result = export_trip(trip, dest_dir, render_map=True)

    assert result.map is None
    assert len(result.warnings) == 1
    assert "map" in result.warnings[0]
    # The rest of the export still succeeded despite the map failure.
    assert result.gpx is not None


def test_export_trip_render_map_uses_a_custom_icon_when_given(
    tmp_path, monkeypatch
):
    from PIL import Image

    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_areas", _fake_areas
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir, monkeypatch)

    icon_path = tmp_path / "car.png"
    Image.new("RGBA", (16, 16), (0, 0, 255, 255)).save(icon_path)

    result = export_trip(trip, dest_dir, render_map=True, map_icon=icon_path)

    assert result.map == dest_dir / "map.mp4"
    assert result.map.exists()
    assert result.warnings == ()


def test_export_trip_render_map_warns_instead_of_failing_on_a_bad_icon_path(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_areas", _fake_areas
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir, monkeypatch)

    result = export_trip(
        trip,
        dest_dir,
        render_map=True,
        map_icon=tmp_path / "does-not-exist.png",
    )

    assert result.map is None
    assert len(result.warnings) == 1
    assert "map" in result.warnings[0]
    # The rest of the export still succeeded despite the bad icon path.
    assert result.gpx is not None


def _trip_with_gsensor_samples(source_dir):
    gsensor_a = source_dir / "a.3gf"
    gsensor_a.write_bytes(
        _gsensor_bytes((0, 100, -200, 900), (500, -300, 400, 950))
    )
    gsensor_b = source_dir / "b.3gf"
    gsensor_b.write_bytes(_gsensor_bytes((0, 200, 100, 980)))

    first = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a)},
    )
    second = Recording(
        id=RecordingId("20260720_100010_N"),
        assets={Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_b)},
    )
    return Trip((first, second))


def test_export_trip_skips_gsensor_video_by_default(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    result = export_trip(trip, dest_dir)

    assert result.gsensor_video is None
    assert not (dest_dir / "gsensor.mp4").exists()


def test_export_trip_render_gsensor_produces_a_video(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    result = export_trip(trip, dest_dir, render_gsensor=True)

    assert result.gsensor_video == dest_dir / "gsensor.mp4"
    assert result.gsensor_video.exists()
    assert result.warnings == ()


def test_export_trip_render_gsensor_debug_prints_phase_timing_to_stderr(
    tmp_path, capsys
):
    # Matches the existing concatenation/map/stitch pattern - Christer
    # noticed gsensor rendering was the one phase --debug said nothing
    # about.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    export_trip(trip, dest_dir, render_gsensor=True, debug=True)

    err = capsys.readouterr().err
    assert "bv-export: gsensor phase took" in err


def test_export_trip_render_gsensor_is_silent_by_default(tmp_path, capsys):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    export_trip(trip, dest_dir, render_gsensor=True)

    assert capsys.readouterr().err == ""


def test_export_trip_gsensor_graph_z_defaults_to_false(tmp_path, monkeypatch):
    # Christer: "Z is just not useful, unless you hit a giant pothole,
    # but then the video probably got that and the reaction of the
    # driver" - see gsensor_graph_render.py's own module docstring.
    # Confirms export_trip()'s own default (gsensor_graph_z=False)
    # actually reaches render_gsensor_graph_video(), not just that
    # gsensor_graph_video.py's own default does the right thing in
    # isolation.
    captured = {}
    original = trip_export_module.render_gsensor_graph_video

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "render_gsensor_graph_video", _capture
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    export_trip(trip, dest_dir, render_gsensor_graph=True)

    assert captured["show_z"] is False


def test_export_trip_gsensor_graph_z_forwarded_when_true(tmp_path, monkeypatch):
    captured = {}
    original = trip_export_module.render_gsensor_graph_video

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "render_gsensor_graph_video", _capture
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    export_trip(trip, dest_dir, render_gsensor_graph=True, gsensor_graph_z=True)

    assert captured["show_z"] is True


def test_export_trip_render_gsensor_logs_elapsed_seconds_to_trip_log(tmp_path):
    # trip.log records this regardless of --debug (see export_trip()'s
    # own docstring) - unlike the stderr print above, which only
    # happens under --debug.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    export_trip(trip, dest_dir, render_gsensor=True)

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    match = re.search(r"rendered gsensor\.mp4 \((\d+\.\d)s\)", log_text)
    assert match is not None


def test_export_trip_render_gsensor_warns_instead_of_failing_on_encode_error(
    tmp_path, monkeypatch
):
    def _broken(*_args, **_kwargs):
        raise MediaToolError("ffmpeg not found on PATH")

    monkeypatch.setattr(
        trip_export_module, "render_gsensor_video", _broken
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    result = export_trip(trip, dest_dir, render_gsensor=True)

    assert result.gsensor_video is None
    assert len(result.warnings) == 1
    assert "gsensor video" in result.warnings[0]
    # The rest of the export still succeeded despite the failure.
    assert result.gsensor is not None


def _trip_with_front_and_rear(source_dir):
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    return Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
            },
        ),
    ))


def test_export_trip_skips_stitch_by_default(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir)

    assert result.stitch is None
    assert not (dest_dir / "stitch.mp4").exists()


def test_export_trip_stitch_layout_produces_a_video(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    assert result.stitch == dest_dir / "stitch.mp4"
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_scale_and_max_dimensions_are_forwarded(
    tmp_path, monkeypatch
):
    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    export_trip(
        trip, dest_dir, stitch_layout="side_by_side",
        stitch_scale=50.0, stitch_max_width=1920, stitch_max_height=1080,
    )

    assert captured["scale"] == 50.0
    assert captured["max_width"] == 1920
    assert captured["max_height"] == 1080


def test_export_trip_stitch_muxes_this_trips_own_concatenated_audio(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    audio_a = source_dir / "audio_a.aac"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)
    _make_audio(audio_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.AUDIO: AssetFile(Asset.AUDIO, audio_a),
            },
        ),
    ))

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    assert result.audio == dest_dir / "audio.aac"
    assert result.stitch == dest_dir / "stitch.mp4"
    assert _has_audio_stream(result.stitch)
    assert result.warnings == ()


def test_export_trip_stitch_has_no_audio_when_the_trip_has_none(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    assert result.audio is None
    assert not _has_audio_stream(result.stitch)


def test_export_trip_stitch_rearview_mirror_produces_a_video(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout="rearview_mirror")

    assert result.stitch == dest_dir / "stitch.mp4"
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_mirror_size_is_forwarded(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror", stitch_mirror_size=40.0,
    )

    assert captured["mirror_size"] == 40.0


def test_export_trip_stitch_mirror_radius_is_forwarded(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror", stitch_mirror_radius=50.0,
    )

    assert captured["mirror_radius"] == 50.0


def test_export_trip_stitch_mirror_zoom_is_forwarded(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror", stitch_mirror_zoom=40.0,
    )

    assert captured["mirror_zoom"] == 40.0


def test_export_trip_stitch_mirror_pan_is_forwarded(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror",
        stitch_mirror_pan_x=-25.0, stitch_mirror_pan_y=60.0,
    )

    assert captured["mirror_pan_x"] == -25.0
    assert captured["mirror_pan_y"] == 60.0


def test_export_trip_stitch_mirror_icon_is_forwarded(tmp_path, monkeypatch):
    from PIL import Image
    from PIL import ImageDraw

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    icon_path = tmp_path / "mirror.png"
    image = Image.new("RGB", (40, 40), (0, 0, 0))
    ImageDraw.Draw(image).rectangle((10, 10, 29, 29), fill=(255, 255, 255))
    image.save(icon_path)

    export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror", stitch_mirror_icon=icon_path,
    )

    assert captured["mirror_icon"] == icon_path


def test_export_trip_stitch_mirror_icon_warns_instead_of_failing_on_a_bad_icon_path(
    tmp_path
):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror",
        stitch_mirror_icon=tmp_path / "does-not-exist.png",
    )

    # Unlike a bad --map-icon (which fails the map entirely), a bad
    # --stitch-mirror-icon still produces a full stitch.mp4 - it just
    # falls back to the plain procedural inset instead of the photo
    # composite. See stitch.py's own is_mirror/mirror_icon handling.
    assert result.stitch == dest_dir / "stitch.mp4"
    assert result.stitch.exists()
    assert len(result.warnings) == 1
    assert "mirror icon" in result.warnings[0]


def _trip_with_front_rear_and_gps_shape(
    source_dir, monkeypatch, *, east_west: bool
):
    # See _trip_with_two_gps_fixes()'s own comment - same reason.
    monkeypatch.setattr(
        trip_export_module, "load_or_reverse_geocode", _fake_geocode
    )

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    gps_a = source_dir / "a.gps"
    gps_a.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )
    gps_b = source_dir / "b.gps"
    # A large step on the axis this trip should run along, a tiny one
    # on the other - same real-world-shape idea test_stitch.py's own
    # pick_stitch_layout() tests use, just expressed as raw NMEA
    # sentences here since export_trip() reads GPS from recordings,
    # not from pre-built GpsFix objects.
    if east_west:
        gps_b.write_text(
            "[1700000010000]$GPRMC,120010.00,A,4807.238,N,01141.000,E,"
            "12.00,45.00,010124,,,A*6D\n"
        )
    else:
        gps_b.write_text(
            "[1700000010000]$GPRMC,120010.00,A,4907.038,N,01131.010,E,"
            "12.00,45.00,010124,,,A*6D\n"
        )

    return Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.GPS: AssetFile(Asset.GPS, gps_a),
            },
        ),
        Recording(
            id=RecordingId("20260720_100010_N"),
            assets={Asset.GPS: AssetFile(Asset.GPS, gps_b)},
        ),
    ))


def test_export_trip_stitch_auto_layout_picks_side_by_side_for_east_west(
    tmp_path, monkeypatch
):
    from blackvue.export.stitch import AUTO_LAYOUT

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps_shape(
        source_dir, monkeypatch, east_west=True
    )

    result = export_trip(trip, dest_dir, stitch_layout=AUTO_LAYOUT)

    assert result.warnings == ()
    # side_by_side hstacks - combined width doubles, height unchanged.
    assert _video_size(result.stitch) == (128, 64)


def test_export_trip_stitch_auto_layout_picks_top_down_for_north_south(
    tmp_path, monkeypatch
):
    from blackvue.export.stitch import AUTO_LAYOUT

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps_shape(
        source_dir, monkeypatch, east_west=False
    )

    result = export_trip(trip, dest_dir, stitch_layout=AUTO_LAYOUT)

    assert result.warnings == ()
    # top_down vstacks - combined height doubles, width unchanged.
    assert _video_size(result.stitch) == (64, 128)


def test_export_trip_map_zoom_matches_video_height_for_north_south_trip(
    tmp_path, monkeypatch
):
    # Christer: "Map zoom layout shouldn't be square, it should match
    # the videos height or width depending on layout... just as the
    # other map" - "the other map" being --stitch-map's own panel,
    # which this reuses the exact sizing rule of (see
    # stitch.map_zoom_dimensions()). A north-south trip's shared axis
    # is height, matched exactly to the real front video's own height;
    # the free axis (width) is derived from the trip's shape instead
    # of defaulting to a fixed square.
    monkeypatch.setattr(trip_export_module, "load_or_fetch_roads", _fake_roads)
    monkeypatch.setattr(trip_export_module, "load_or_fetch_areas", _fake_areas)

    calls = []

    def _capture(fixes, roads, bbox, destination, **kwargs):
        calls.append((destination, kwargs))
        return destination

    monkeypatch.setattr(trip_export_module, "render_map_video", _capture)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps_shape(
        source_dir, monkeypatch, east_west=False
    )

    export_trip(trip, dest_dir, render_map=True, map_zoom_meters=50.0)

    zoom_kwargs = next(
        kwargs for destination, kwargs in calls
        if destination == dest_dir / "map_zoom_50m.mp4"
    )
    static_kwargs = next(
        kwargs for destination, kwargs in calls
        if destination == dest_dir / "map.mp4"
    )

    # front.mp4 is a 64x64 testsrc clip (see _make_video()) - the
    # shared axis (height, for this north-south trip) must match that
    # exactly; the free axis (width) is derived from trip geography
    # and clamped to a fraction of it, so it comes out smaller than
    # 64, not equal to it - proof the panel isn't just square-by-
    # coincidence.
    assert zoom_kwargs["height"] == 64
    assert 0 < zoom_kwargs["width"] < 64

    # The static map.mp4 is untouched - Christer's request was
    # specifically about "Map zoom", not the static overview.
    assert "width" not in static_kwargs
    assert "height" not in static_kwargs


def test_export_trip_map_zoom_matches_video_width_for_east_west_trip(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(trip_export_module, "load_or_fetch_roads", _fake_roads)
    monkeypatch.setattr(trip_export_module, "load_or_fetch_areas", _fake_areas)

    calls = []

    def _capture(fixes, roads, bbox, destination, **kwargs):
        calls.append((destination, kwargs))
        return destination

    monkeypatch.setattr(trip_export_module, "render_map_video", _capture)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps_shape(
        source_dir, monkeypatch, east_west=True
    )

    export_trip(trip, dest_dir, render_map=True, map_zoom_meters=50.0)

    zoom_kwargs = next(
        kwargs for destination, kwargs in calls
        if destination == dest_dir / "map_zoom_50m.mp4"
    )

    assert zoom_kwargs["width"] == 64
    assert 0 < zoom_kwargs["height"] < 64


def test_export_trip_front_mp4_keeps_audio_despite_a_video_only_source(
    tmp_path,
):
    # Regression test for the real front.mp4 corruption bug: a
    # BlackVue FRONT recording normally carries its own embedded audio
    # track, but a repaired Parking recording's own front file is
    # video-only (its broken audio track gets dropped entirely by
    # mp4_repair.py - see concatenate_media()'s own docstring for the
    # full story). Concatenating that video-only segment in among
    # ordinary video+audio ones via a plain -c copy corrupted the
    # whole front.mp4's own duration metadata on a real trip, even
    # though the real frame count survived intact. front.mp4 is now
    # always concatenated video-only, then has the trip's own
    # separately-built audio.aac remuxed back in - this confirms that
    # remux actually lands: front.mp4 ends up with a real audio stream
    # even though one of its two source recordings' own front video
    # had none at all.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    audio_a = source_dir / "a.aac"
    _make_video_with_audio(front_a, 1.0)
    _make_video(rear_a, 1.0)
    _make_audio(audio_a, 1.0)

    # Stands in for a repaired Parking recording's own front file:
    # video-only, no audio track at all.
    front_p = source_dir / "front_p.mp4"
    _make_video(front_p, 1.0)

    first_id = RecordingId("20260720_100000_N")
    parking_id = RecordingId("20260720_100010_P")

    trip = Trip((
        Recording(
            id=first_id,
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.AUDIO: AssetFile(Asset.AUDIO, audio_a),
            },
        ),
        Recording(
            id=parking_id,
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front_p)},
        ),
    ))

    result = export_trip(trip, dest_dir, include_parking=True)

    assert result.front_video is not None
    assert _has_audio_stream(result.front_video)


def test_export_trip_audio_stays_in_sync_when_a_middle_recording_has_no_audio(
    tmp_path,
):
    # Regression test for the real desync Christer hit once front.mp4
    # itself was fixed: "audio it not in sync width front, its
    # synching with the parking file." A Parking recording never gets
    # its own Asset.AUDIO (see _ensure_recording_audio()'s own
    # docstring - by design), but its real video still takes up real
    # time in front.mp4's timeline. Left as-is, audio.aac - and
    # therefore front.mp4's remuxed audio track - ends up shorter than
    # the video by exactly that recording's own duration, so
    # everything *after* it plays back against audio recorded for a
    # different moment entirely. A silent recording sits in the
    # middle here, between two real video+audio recordings, standing
    # in for that mid-trip Parking gap.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    audio_a = source_dir / "a.aac"
    _make_video_with_audio(front_a, 1.0)
    _make_video(rear_a, 1.0)
    _make_audio(audio_a, 1.0)

    # Stands in for a repaired Parking recording's own front file:
    # video-only, no Asset.AUDIO at all - and, being Parking, never
    # gets one self-healed either.
    front_p = source_dir / "front_p.mp4"
    _make_video(front_p, 1.0)

    front_b = source_dir / "front_b.mp4"
    rear_b = source_dir / "rear_b.mp4"
    audio_b = source_dir / "b.aac"
    _make_video_with_audio(front_b, 1.0)
    _make_video(rear_b, 1.0)
    _make_audio(audio_b, 1.0)

    first_id = RecordingId("20260720_100000_N")
    parking_id = RecordingId("20260720_100010_P")
    last_id = RecordingId("20260720_100020_N")

    trip = Trip((
        Recording(
            id=first_id,
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.AUDIO: AssetFile(Asset.AUDIO, audio_a),
            },
        ),
        Recording(
            id=parking_id,
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front_p)},
        ),
        Recording(
            id=last_id,
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_b),
                Asset.REAR: AssetFile(Asset.REAR, rear_b),
                Asset.AUDIO: AssetFile(Asset.AUDIO, audio_b),
            },
        ),
    ))

    result = export_trip(trip, dest_dir, include_parking=True)

    assert result.front_video is not None
    video_duration = _video_duration(result.front_video)
    audio_duration = _audio_stream_duration(result.front_video)
    # Before the fix, the remuxed audio track would only span the two
    # real recordings' own audio (~2s) while the video spans all three
    # recordings including the silent gap (~3s) - a full recording's
    # worth of drift for anything after it. Real ffmpeg encoder
    # framing means this won't land on an exact match, but it should
    # be close, not off by an entire extra recording's duration.
    assert abs(video_duration - audio_duration) < 0.5


def test_export_trip_video_duration_uses_summed_sources_not_corrupted_concat_probe(
    tmp_path, monkeypatch
):
    # Christer, on a real trip: front.mp4 reported avg_frame_rate
    # 47375/25573 (~1.85fps) and duration=3682s after concatenating a
    # repaired Parking recording, while rear.mp4 - built from the same
    # underlying footage, confirmed by real frame counts matching
    # almost exactly (6822 vs 6829) - correctly reported ~29.66fps and
    # 230s for the same content. ffmpeg's `-c copy` concat demuxer
    # doesn't harmonize timescales across inputs, so the concatenated
    # file's own container-level duration metadata can end up wrong
    # even though every individual source probes correctly. See
    # _recording_video_offsets()'s own docstring for the full story.
    #
    # This test simulates that corruption directly - real ffmpeg
    # concat of this fixture's own tiny clips is fine, the bug only
    # reproduces with a genuine timescale mismatch not worth
    # manufacturing here - by monkeypatching probe() to return a
    # wildly inflated duration specifically for the concatenated
    # front.mp4, and confirms video_duration_seconds (what feeds
    # map.mp4's own frame_count, and trip.srt/.lrc padding) comes from
    # the reliable per-source sum instead of that corrupted number.
    monkeypatch.setattr(
        trip_export_module, "load_or_reverse_geocode", _fake_geocode
    )
    monkeypatch.setattr(trip_export_module, "load_or_fetch_roads", _fake_roads)
    monkeypatch.setattr(trip_export_module, "load_or_fetch_areas", _fake_areas)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps_shape(
        source_dir, monkeypatch, east_west=False
    )

    real_probe = trip_export_module.probe

    def _fake_probe(path):
        info = real_probe(path)
        if Path(path).name == "front.mp4":
            # Same shape as the real bug: same content, container
            # metadata describing something ~16x longer.
            return MediaInfo(
                duration_seconds=info.duration_seconds * 16,
                frame_rate=info.frame_rate / 16,
            )
        return info

    monkeypatch.setattr(trip_export_module, "probe", _fake_probe)

    calls = []

    def _capture(fixes, roads, bbox, destination, **kwargs):
        calls.append((destination, kwargs))
        return destination

    monkeypatch.setattr(trip_export_module, "render_map_video", _capture)

    export_trip(trip, dest_dir, render_map=True)

    map_kwargs = next(
        kwargs for destination, kwargs in calls
        if destination == dest_dir / "map.mp4"
    )

    # Real front_a duration (~1.0s, per _trip_with_front_rear_and_gps_
    # shape()'s own _make_video(front_a, 1.0)), not the ~16s a naive
    # probe-the-concatenated-file approach would have produced.
    assert map_kwargs["video_duration_seconds"] < 2.0


def test_export_trip_stitch_auto_layout_falls_back_without_gps_data(
    tmp_path
):
    from blackvue.export.stitch import AUTO_LAYOUT

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout=AUTO_LAYOUT)

    assert len(result.warnings) == 1
    assert "no GPS data to auto-pick" in result.warnings[0]
    # Falls back to side_by_side, same as the CLI's own pre-auto-pick
    # default.
    assert _video_size(result.stitch) == (128, 64)


def test_export_trip_stitch_explicit_layout_is_never_overridden_by_auto_pick(
    tmp_path, monkeypatch
):
    # An east-west trip would auto-pick side_by_side - explicitly
    # asking for top_down instead must still be honored exactly, since
    # auto-pick only ever applies to AUTO_LAYOUT itself.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps_shape(
        source_dir, monkeypatch, east_west=True
    )

    result = export_trip(trip, dest_dir, stitch_layout="top_down")

    assert result.warnings == ()
    assert _video_size(result.stitch) == (64, 128)


def test_export_trip_stitch_falls_back_to_front_only_with_no_rear(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_only = source_dir / "front_only.mp4"
    _make_video(front_only, 1.0)
    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front_only)},
        ),
    ))

    result = export_trip(trip, dest_dir, stitch_layout="top_down")

    assert result.stitch == dest_dir / "stitch.mp4"
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_warns_instead_of_failing_on_encode_error(
    tmp_path, monkeypatch
):
    def _broken(*_args, **_kwargs):
        raise MediaToolError("ffmpeg not found on PATH")

    monkeypatch.setattr(trip_export_module, "stitch_cameras", _broken)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    assert result.stitch is None
    assert len(result.warnings) == 1
    assert "stitch" in result.warnings[0]
    # The rest of the export still succeeded despite the stitch failure.
    assert result.front_video is not None


def _trip_with_front_rear_and_gps(source_dir, monkeypatch):
    # See _trip_with_two_gps_fixes()'s own comment - same reason.
    monkeypatch.setattr(
        trip_export_module, "load_or_reverse_geocode", _fake_geocode
    )

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    gps_a = source_dir / "a.gps"
    gps_a.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )
    gps_b = source_dir / "b.gps"
    gps_b.write_text(
        "[1700000010000]$GPRMC,120010.00,A,4808.038,N,01132.000,E,"
        "12.00,45.00,010124,,,A*6D\n"
    )

    return Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.GPS: AssetFile(Asset.GPS, gps_a),
            },
        ),
        Recording(
            id=RecordingId("20260720_100010_N"),
            assets={Asset.GPS: AssetFile(Asset.GPS, gps_b)},
        ),
    ))


def test_export_trip_stitch_map_adds_a_panel_to_stitch_mp4(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_areas", _fake_areas
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps(source_dir, monkeypatch)

    result_plain = export_trip(
        trip, dest_dir / "plain", stitch_layout="side_by_side",
    )
    result_with_map = export_trip(
        trip, dest_dir / "with_map",
        stitch_layout="side_by_side", stitch_map="map",
    )

    assert result_plain.warnings == ()
    assert result_with_map.warnings == ()
    assert result_with_map.stitch.exists()

    plain_size = _video_size(result_plain.stitch)
    with_map_size = _video_size(result_with_map.stitch)
    # Default side for side_by_side is 'down' - width unchanged, height
    # grows to fit the added panel.
    assert with_map_size[0] == plain_size[0]
    assert with_map_size[1] > plain_size[1]


def test_export_trip_stitch_map_side_is_forwarded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_areas", _fake_areas
    )

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps(source_dir, monkeypatch)

    export_trip(
        trip, dest_dir,
        stitch_layout="top_down", stitch_map="map", stitch_map_side="right",
    )

    assert captured["map_mode"] == "map"
    assert captured["map_side"] == "right"
    assert len(captured["map_fixes"]) == 2
    assert len(captured["map_roads"]) == 1


def test_export_trip_stitch_map_size_is_forwarded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_areas", _fake_areas
    )

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps(source_dir, monkeypatch)

    export_trip(
        trip, dest_dir,
        stitch_layout="top_down", stitch_map="map", stitch_map_size=35.0,
    )

    assert captured["map_size"] == 35.0


def test_export_trip_stitch_map_skipped_without_stitch_map_flag(
    tmp_path, monkeypatch
):
    def _refuse(*_args, **_kwargs):
        raise AssertionError(
            "should not fetch roads for stitch when stitch_map isn't given"
        )

    monkeypatch.setattr(trip_export_module, "load_or_fetch_roads", _refuse)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps(source_dir, monkeypatch)

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_gsensor_uses_a_freshly_rendered_file(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    gsensor_a = source_dir / "a.3gf"
    gsensor_a.write_bytes(_gsensor_bytes((0, 100, -50, 900), (100, 90, -40, 950)))
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a),
            },
        ),
    ))

    result = export_trip(
        trip, dest_dir,
        stitch_layout="side_by_side", render_gsensor=True, stitch_gsensor=True,
    )

    assert result.gsensor_video == dest_dir / "gsensor.mp4"
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_gsensor_reuses_a_file_from_an_earlier_run(
    tmp_path
):
    # render_gsensor=False this run - gsensor.mp4 already sitting in
    # the destination folder from some earlier run should still be
    # picked up (bv-export's own keep-existing-files-by-default
    # behavior), not just this run's own fresh render.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()

    _make_video(dest_dir / "gsensor.mp4", 1.0)

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
            },
        ),
    ))

    result = export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
    )

    assert result.gsensor_video is None
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_gsensor_reuse_debug_prints_to_stderr(
    tmp_path, capsys
):
    # Christer: "gsensor file doesn't give any output when the video
    # already exist" - every other phase prints something to stderr
    # under --debug, but the reuse path (render_gsensor=False, an
    # existing gsensor.mp4 already sitting in the destination folder)
    # printed nothing at all, unlike a fresh render's own "gsensor
    # phase took Xs" line.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()

    _make_video(dest_dir / "gsensor.mp4", 1.0)

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
            },
        ),
    ))

    export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
        debug=True,
    )

    err = capsys.readouterr().err
    assert "gsensor.mp4 already exists" in err
    assert "reusing for stitch overlay" in err


def test_export_trip_stitch_gsensor_reuse_is_silent_by_default(tmp_path, capsys):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()

    _make_video(dest_dir / "gsensor.mp4", 1.0)

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
            },
        ),
    ))

    export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
    )

    assert capsys.readouterr().err == ""


def test_export_trip_stitch_gsensor_warns_when_trip_has_no_gsensor_data(
    tmp_path
):
    # This trip's recording has no GSENSOR asset at all - no flag can
    # ever produce a gsensor.mp4 for it, so the warning should say so
    # plainly rather than pointing at --gsensor-video, which would be
    # wrong advice (see the sibling "not yet rendered" test below for
    # the case where that advice is correct).
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
    )

    assert result.stitch.exists()
    assert len(result.warnings) == 1
    assert "no g-sensor data for this trip" in result.warnings[0]
    assert "--gsensor-video" not in result.warnings[0]


def test_export_trip_stitch_gsensor_renders_gsensor_mp4_when_missing(
    tmp_path
):
    # This trip DOES have g-sensor data (a GSENSOR asset), but this
    # run neither rendered it (render_gsensor=False) nor has an
    # earlier run's gsensor.mp4 sitting in the destination folder.
    # Christer: "do you think that same behaviour [--translate implying
    # --transcribe] [should apply] to bv-export like that --stitch-graph
    # should imply --gsensor-graph-video" - --stitch-graph turned out to
    # already self-render, so --stitch-gsensor was brought in line with
    # it: gsensor.mp4 gets rendered fresh right here (and left behind in
    # the destination folder for a later run's own reuse check to find),
    # rather than warning and skipping.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    gsensor_a = source_dir / "a.3gf"
    gsensor_a.write_bytes(_gsensor_bytes((0, 100, -50, 900), (100, 90, -40, 950)))
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a),
            },
        ),
    ))

    result = export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
    )

    assert result.stitch.exists()
    assert (dest_dir / "gsensor.mp4").exists()
    assert result.warnings == ()
    # Rendered for the stitch overlay's own sake, not because
    # --gsensor-video was requested - ExportResult.gsensor_video keeps
    # meaning "the standalone output was produced this run", unchanged.
    assert result.gsensor_video is None


def test_export_trip_stitch_gsensor_debug_prints_when_rendering_on_demand(
    tmp_path, capsys
):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    gsensor_a = source_dir / "a.3gf"
    gsensor_a.write_bytes(_gsensor_bytes((0, 100, -50, 900), (100, 90, -40, 950)))
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a),
            },
        ),
    ))

    export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
        debug=True,
    )

    err = capsys.readouterr().err
    assert "stitch gsensor overlay" in err
    assert "rendered gsensor.mp4" in err


def test_export_trip_stitch_gsensor_options_are_forwarded(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()

    _make_video(dest_dir / "gsensor.mp4", 1.0)

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
            },
        ),
    ))

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="side_by_side", stitch_gsensor=True,
        stitch_gsensor_size=25.0, stitch_gsensor_xy=(5.0, 5.0),
    )

    assert captured["gsensor_video"] == dest_dir / "gsensor.mp4"
    assert captured["gsensor_size"] == 25.0
    assert captured["gsensor_xy"] == (5.0, 5.0)


def _trip_with_front_rear_and_gsensor(source_dir):
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    gsensor_a = source_dir / "a.3gf"
    gsensor_a.write_bytes(
        _gsensor_bytes((0, 100, -200, 900), (500, -300, 400, 950))
    )

    return Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a),
            },
        ),
    ))


def test_export_trip_stitch_graph_z_defaults_to_false(tmp_path, monkeypatch):
    # Same reasoning as test_export_trip_gsensor_graph_z_defaults_to_false
    # above, but for the --stitch-graph panel path (stitch_cameras()'s
    # own `graph_z` kwarg) rather than the standalone gsensor_graph.mp4
    # path - the two share one CLI switch (see bv_export.py), but are
    # two separate call sites inside export_trip() that both need to
    # forward it correctly.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gsensor(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(trip, dest_dir, stitch_layout="side_by_side", stitch_graph=True)

    assert captured["graph_z"] is False


def test_export_trip_stitch_graph_z_forwarded_when_true(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gsensor(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_graph=True,
        gsensor_graph_z=True,
    )

    assert captured["graph_z"] is True


def _trip_with_front_rear_and_subtitles(source_dir):
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    srt_a = source_dir / "a.srt"
    srt_a.write_text(format_srt((SpeechSegment(0.0, 1.0, "hello there"),)))

    return Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_a),
            },
        ),
    ))


def test_export_trip_stitch_subtitles_uses_this_runs_own_trip_srt(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_subtitles(source_dir)

    result = export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_subtitles=True,
    )

    # No separate "render it first" step needed, unlike
    # stitch_gsensor - trip.srt is always written earlier in this same
    # call whenever the trip has transcript data at all.
    assert result.srt == dest_dir / "trip.srt"
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_subtitles_options_are_forwarded(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_subtitles(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="side_by_side", stitch_subtitles=True,
        stitch_subtitles_background=False,
    )

    assert captured["subtitles_path"] == dest_dir / "trip.srt"
    assert captured["subtitles_background"] is False


def test_export_trip_stitch_subtitles_skipped_without_the_flag(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_subtitles(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    # trip.srt still gets written (merge_srt() isn't gated behind
    # stitch_subtitles at all), but it's not passed on to the stitch
    # call without the flag.
    assert result.srt == dest_dir / "trip.srt"
    assert captured["subtitles_path"] is None


def test_export_trip_stitch_subtitles_warns_when_no_transcript_data(
    tmp_path
):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_subtitles=True,
    )

    assert result.srt is None
    assert result.stitch.exists()
    assert len(result.warnings) == 1
    assert "no transcript data" in result.warnings[0]


def test_export_trip_merges_srt_and_lrc_with_rebased_timestamps(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    srt_a = source_dir / "a.srt"
    srt_a.write_text(
        format_srt((SpeechSegment(0.0, 2.0, "first recording"),))
    )
    lrc_a = source_dir / "a.lrc"
    lrc_a.write_text(format_lrc((SpeechSegment(0.0, 0.0, "first recording"),)))

    srt_b = source_dir / "b.srt"
    srt_b.write_text(
        format_srt((SpeechSegment(0.0, 1.0, "second recording"),))
    )
    lrc_b = source_dir / "b.lrc"
    lrc_b.write_text(
        format_lrc((SpeechSegment(0.0, 0.0, "second recording"),))
    )

    first = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={
            Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_a),
            Asset.LYRICS: AssetFile(Asset.LYRICS, lrc_a),
        },
    )
    second = Recording(
        id=RecordingId("20260720_100100_N"),
        assets={
            Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_b),
            Asset.LYRICS: AssetFile(Asset.LYRICS, lrc_b),
        },
    )
    trip = Trip((first, second))

    result = export_trip(trip, dest_dir)

    assert result.srt == dest_dir / "trip.srt"
    srt_text = result.srt.read_text()
    assert "00:00:00,000 --> 00:00:02,000" in srt_text
    assert "first recording" in srt_text
    # Second recording started 60s after the first.
    assert "00:01:00,000 --> 00:01:01,000" in srt_text
    assert "second recording" in srt_text

    assert result.lrc == dest_dir / "trip.lrc"
    lrc_text = result.lrc.read_text()
    assert "[00:00.00] first recording" in lrc_text
    assert "[01:00.00] second recording" in lrc_text


def test_export_trip_skips_srt_lrc_when_no_recording_has_them(tmp_path):
    dest_dir = tmp_path / "export"
    trip = Trip((Recording(id=RecordingId("20260720_100000_N")),))

    result = export_trip(trip, dest_dir)

    assert result.srt is None
    assert result.lrc is None
    assert not (dest_dir / "trip.srt").exists()
    assert not (dest_dir / "trip.lrc").exists()


def test_export_trip_always_writes_a_trip_log(tmp_path):
    dest_dir = tmp_path / "export"
    trip = Trip((Recording(id=RecordingId("20260720_100000_N")),))

    export_trip(trip, dest_dir)

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    assert "=== bv-export trip log:" in log_text
    assert trip.label in log_text
    assert "Started:" in log_text
    assert "Finished:" in log_text


def test_export_trip_writes_the_given_command_line_into_the_trip_log(tmp_path):
    dest_dir = tmp_path / "export"
    trip = Trip((Recording(id=RecordingId("20260720_100000_N")),))

    export_trip(
        trip, dest_dir, command_line="bv-export --target out --stitch"
    )

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    assert "Command: bv-export --target out --stitch" in log_text


def test_export_trip_writes_membership_reasons_into_the_trip_log(tmp_path):
    dest_dir = tmp_path / "export"
    first = Recording(id=RecordingId("20260720_100000_N"))
    second = Recording(id=RecordingId("20260720_100100_N"))
    trip = Trip((first, second))

    reasons = {
        first.id: "first recording in the archive",
        second.id: "continues the trip - gap since ... was 60.0s, within threshold",
    }

    export_trip(trip, dest_dir, reasons=reasons)

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    assert "--- Trip membership ---" in log_text
    assert f"{first.id}: first recording in the archive" in log_text
    assert f"{second.id}: continues the trip" in log_text


def test_export_trip_logs_concatenation_and_gpx_steps(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    _make_video(front_a, 1.0)
    gps_a = source_dir / "a.gps"
    gps_a.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.GPS: AssetFile(Asset.GPS, gps_a),
            },
        ),
    ))

    export_trip(trip, dest_dir)

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    assert "--- Export steps ---" in log_text
    assert "starting concatenation (front/rear/audio)" in log_text
    assert "concatenated front.mp4 from 1 recording(s)" in log_text
    assert "no source recordings for rear.mp4 - skipped" in log_text
    assert "wrote trip.gpx (1 fix(es))" in log_text


def test_export_trip_logs_a_starting_line_before_the_stitch_render(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    _make_video(front_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front_a)},
        ),
    ))

    export_trip(trip, dest_dir, stitch_layout="side_by_side")

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    # A "starting" line lets a hung run be diagnosed by which phase it
    # was in, not just phases that finished (see trip_log.py's own
    # docstring) - it must appear even for a fast test render.
    assert "starting stitch.mp4 render (layout=side_by_side)" in log_text
    assert "rendered stitch.mp4" in log_text


def test_export_trip_pads_srt_lrc_to_match_the_real_video_length(tmp_path):
    # Christer's real-world case: the last stretch of a trip is quiet,
    # so Whisper's segments (and the resulting .srt/.lrc) end well
    # before the video actually does. export_trip() should pad the
    # merged subtitle files out to the concatenated video's real
    # (ffprobe-measured) length.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    video_path = source_dir / "front.mp4"
    _make_video(video_path, duration_seconds=5.0)

    srt_path = source_dir / "a.srt"
    srt_path.write_text(
        format_srt((SpeechSegment(0.0, 1.0, "hello"),))
    )
    lrc_path = source_dir / "a.lrc"
    lrc_path.write_text(
        format_lrc((SpeechSegment(0.0, 0.0, "hello"),))
    )

    recording = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={
            Asset.FRONT: AssetFile(Asset.FRONT, video_path),
            Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_path),
            Asset.LYRICS: AssetFile(Asset.LYRICS, lrc_path),
        },
    )
    trip = Trip((recording,))

    result = export_trip(trip, dest_dir)

    srt_text = result.srt.read_text()
    assert "hello" in srt_text
    # A second, empty cue was appended ending at (approximately) the
    # video's real 5s length - not stopping at 1s where "hello" ended.
    assert "\n2\n" in srt_text
    assert "--> 00:00:05,000" in srt_text

    lrc_text = result.lrc.read_text()
    lines = lrc_text.splitlines()
    assert lines[0] == "[00:00.00] hello"
    assert len(lines) == 2
    assert lines[1].startswith("[00:0")  # padding line near the 5s mark


# _replace_with_retry() - front.mp4's audio-remux swap. Christer hit a
# real "Access is denied" swapping the remuxed temp file into place on
# a real export, even after the earlier cross-drive fix (same volume,
# but something else - most likely antivirus briefly scanning the
# just-written file - held a lock). These use monkeypatched
# Path.replace()/shutil.copyfile() rather than real OS-level locking,
# since a genuine transient Windows file lock isn't reproducible on
# demand here - they exist to pin down _replace_with_retry()'s own
# retry-then-fallback-then-give-up decision logic.


def test_replace_with_retry_succeeds_immediately_when_nothing_is_locked(tmp_path):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new")
    destination.write_text("old")

    _replace_with_retry(source, destination, attempts=3, delay_seconds=0)

    assert not source.exists()
    assert destination.read_text() == "new"


def test_replace_with_retry_retries_past_a_transient_permission_error(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new")
    destination.write_text("old")

    real_replace = Path.replace
    calls = {"count": 0}

    def flaky_replace(self, target):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError("Access is denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    _replace_with_retry(source, destination, attempts=5, delay_seconds=0)

    assert calls["count"] == 3
    assert destination.read_text() == "new"


def test_replace_with_retry_falls_back_to_copy_when_replace_never_succeeds(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new")
    destination.write_text("old")

    def always_denied(self, target):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(Path, "replace", always_denied)

    _replace_with_retry(source, destination, attempts=2, delay_seconds=0)

    # The copy fallback doesn't require deleting the destination first
    # (a plain overwrite, not a rename) - it's what lets this succeed
    # even though every replace() attempt above was denied.
    assert destination.read_text() == "new"
    assert not source.exists()


def test_replace_with_retry_raises_the_original_error_if_even_the_copy_fails(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new")
    destination.write_text("old")

    def always_denied(self, target):
        raise PermissionError("Access is denied")

    def copy_also_denied(src, dst):
        raise PermissionError("Access is denied (copy)")

    monkeypatch.setattr(Path, "replace", always_denied)
    monkeypatch.setattr(trip_export_module.shutil, "copyfile", copy_also_denied)

    with pytest.raises(PermissionError):
        _replace_with_retry(source, destination, attempts=2, delay_seconds=0)

    # Neither side was touched by the failed attempt - the caller's
    # own exception handling decides what to do with the still-intact
    # source and the still-intact original destination.
    assert source.exists()
    assert destination.read_text() == "old"
