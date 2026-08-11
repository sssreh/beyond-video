from pathlib import Path

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.cli import bv_scribe
from blackvue.cli.bv_scribe import parse_args
from blackvue.generate import SCENE_DEFAULT_MODEL
from blackvue.generate.media import MediaToolError


def test_parse_args_defaults():
    args = parse_args(["/some/archive"])

    assert args.path == "/some/archive"
    assert args.model == SCENE_DEFAULT_MODEL
    assert args.task == "both"
    assert args.zoom_signs is True
    assert args.zoom_plate_confidence_check is True
    assert args.trip_summary is False
    assert args.cpu is False


def test_parse_args_path_defaults_to_cwd():
    args = parse_args([])

    assert args.path == "."


def test_parse_args_no_zoom_signs_flag():
    args = parse_args(["/some/archive", "--no-zoom-signs"])

    assert args.zoom_signs is False


def test_parse_args_no_zoom_plate_confidence_check_flag():
    args = parse_args(["/some/archive", "--no-zoom-plate-confidence-check"])

    assert args.zoom_plate_confidence_check is False


def test_parse_args_trip_summary_flag():
    args = parse_args(["/some/archive", "--trip-summary"])

    assert args.trip_summary is True


def _make_recording(recording_id: str, tmp_path: Path) -> Recording:
    recording = Recording(id=RecordingId(recording_id))
    video = tmp_path / f"{recording_id}F.mp4"
    video.write_bytes(b"x")
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=video)
    return recording


class _FakeArchive:
    def __init__(self, recordings):
        self.recordings = recordings

    def __call__(self, path):
        return self


def test_run_writes_scene_file_per_recording(monkeypatch, tmp_path):
    recording = _make_recording("20260715_134010_N", tmp_path)

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([recording]))

    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append((source, kwargs))
        return "## Description\nRoutine driving.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)

    args = parse_args([str(tmp_path)])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert len(calls) == 1
    written = (tmp_path / "20260715_134010_N.scene.txt").read_text(encoding="utf-8")
    assert "Routine driving." in written


def test_run_skips_existing_without_overwrite(monkeypatch, tmp_path):
    recording = _make_recording("20260715_150000_N", tmp_path)
    existing = tmp_path / "20260715_150000_N.scene.txt"
    existing.write_text("already described")

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([recording]))
    monkeypatch.setattr(bv_scribe, "_interactive", lambda: False)
    monkeypatch.setattr(bv_scribe, "describe_scene", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not describe an already-existing file without --overwrite")
    ))

    args = parse_args([str(tmp_path)])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert existing.read_text() == "already described"


def test_run_dry_run_writes_nothing(monkeypatch, tmp_path):
    recording = _make_recording("20260715_160000_N", tmp_path)

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([recording]))
    monkeypatch.setattr(bv_scribe, "describe_scene", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("dry-run should not call describe_scene")
    ))

    args = parse_args([str(tmp_path), "--dry-run"])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert not (tmp_path / "20260715_160000_N.scene.txt").exists()


def test_run_no_recordings_in_range(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([]))

    args = parse_args([str(tmp_path)])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert "no recordings found" in capsys.readouterr().out


def test_run_propagates_media_tool_error_as_had_error(monkeypatch, tmp_path):
    recording = _make_recording("20260715_170000_N", tmp_path)

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([recording]))

    def fake_describe_scene(source, **kwargs):
        raise MediaToolError("out of VRAM")

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)

    args = parse_args([str(tmp_path)])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_HAD_ERRORS
    assert not (tmp_path / "20260715_170000_N.scene.txt").exists()


def test_run_trip_summary_needs_two_or_more_recordings(monkeypatch, tmp_path, capsys):
    recording = _make_recording("20260715_180000_N", tmp_path)

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([recording]))
    monkeypatch.setattr(
        bv_scribe, "describe_scene",
        lambda *a, **k: "## Description\nOne recording only.",
    )
    monkeypatch.setattr(
        bv_scribe, "summarize_trip",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not summarize with <2 recordings")),
    )

    args = parse_args([str(tmp_path), "--trip-summary"])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert "needs 2+" in capsys.readouterr().out
    assert not (tmp_path / "trip_summary.txt").exists()


def test_run_trip_summary_synthesizes_across_recordings(monkeypatch, tmp_path):
    recording_a = _make_recording("20260715_190000_N", tmp_path)
    recording_b = _make_recording("20260715_200000_N", tmp_path)

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([recording_a, recording_b]))

    def fake_describe_scene(source, **kwargs):
        stem = source.stem[:-1]  # strip trailing "F"
        return f"## Description\nSegment {stem}.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)

    summarize_calls = []

    def fake_summarize_trip(segments, **kwargs):
        summarize_calls.append(segments)
        return "The trip went smoothly overall."

    monkeypatch.setattr(bv_scribe, "summarize_trip", fake_summarize_trip)

    args = parse_args([str(tmp_path), "--trip-summary"])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert len(summarize_calls) == 1
    assert len(summarize_calls[0]) == 2
    summary_text = (tmp_path / "trip_summary.txt").read_text(encoding="utf-8")
    assert "trip went smoothly" in summary_text
