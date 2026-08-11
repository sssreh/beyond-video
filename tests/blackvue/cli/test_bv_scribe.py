import threading
from pathlib import Path

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.cli import bv_scribe
from blackvue.cli.bv_scribe import main
from blackvue.cli.bv_scribe import parse_args
from blackvue.core.camera_config import CameraConfig
from blackvue.core.camera_config import config_path
from blackvue.core.camera_config import save_camera_config
from blackvue.generate import SCENE_DEFAULT_MODEL
from blackvue.generate.media import MediaToolError


def test_main_resolves_a_camera_id_to_its_configured_target(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()

    config_dir = tmp_path / "config"
    save_camera_config(
        config_path(config_dir, "Kirby"),
        CameraConfig(id="Kirby", name="Kirby", archive=archive),
    )

    exit_code = main(["Kirby", "--config-dir", str(config_dir)])

    out = capsys.readouterr().out

    assert exit_code == 0
    assert str(archive) in out
    assert "no recordings found" in out


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


def test_parse_args_camera_defaults_to_front():
    args = parse_args(["/some/archive"])

    assert args.camera == "front"


def test_parse_args_raw_defaults_to_off_and_disables_crop():
    args = parse_args(["/some/archive"])
    assert args.raw is False
    assert args.crop_top == 0.0378
    assert args.crop_bottom == 0.0344

    raw_args = parse_args(["/some/path", "--raw"])
    assert raw_args.raw is True
    assert raw_args.crop_top == 0.0
    assert raw_args.crop_bottom == 0.0


def test_parse_args_raw_explicit_crop_overrides_disabled_default():
    args = parse_args(["/some/path", "--raw", "--crop-top", "0.05", "--crop-bottom", "0.02"])

    assert args.crop_top == 0.05
    assert args.crop_bottom == 0.02


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


def test_interactive_requires_the_main_thread(monkeypatch):
    """A background thread (bv-web's job runner always runs jobs on
    one) must never be treated as interactive even if the whole
    process happens to have a real terminal attached - isatty() is
    process-wide, not per-thread, so without this check a bv-web job
    would call input() and hang forever. See _interactive()'s own
    docstring and the "Fix _interactive() false positive hanging
    bv-web jobs" entry in WORKING_CONTEXT.md."""

    class FakeTTY:
        def isatty(self):
            return True

    monkeypatch.setattr(bv_scribe.sys, "stdin", FakeTTY())
    monkeypatch.setattr(bv_scribe.sys, "stdout", FakeTTY())

    assert bv_scribe._interactive() is True

    result = {}

    def check():
        result["value"] = bv_scribe._interactive()

    thread = threading.Thread(target=check)
    thread.start()
    thread.join()

    assert result["value"] is False


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


def _make_front_rear_recording(recording_id: str, tmp_path: Path, *, front=True, rear=True):
    recording = Recording(id=RecordingId(recording_id))
    if front:
        front_video = tmp_path / f"{recording_id}F.mp4"
        front_video.write_bytes(b"x")
        recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=front_video)
    if rear:
        rear_video = tmp_path / f"{recording_id}R.mp4"
        rear_video.write_bytes(b"x")
        recording.assets[Asset.REAR] = AssetFile(asset=Asset.REAR, path=rear_video)
    return recording


def test_run_camera_rear_writes_rear_scene_file(monkeypatch, tmp_path):
    recording = _make_front_rear_recording("20260715_210000_N", tmp_path)

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([recording]))

    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append((source, kwargs))
        return "## Description\nRear view.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)

    args = parse_args([str(tmp_path), "--camera", "rear"])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert len(calls) == 1
    assert calls[0][1]["task"] == "both"  # full treatment, not OCR-only
    assert not (tmp_path / "20260715_210000_N.scene.txt").exists()
    assert (tmp_path / "20260715_210000_N.rear.scene.txt").exists()


def test_run_camera_both_writes_front_and_rear_bonus(monkeypatch, tmp_path):
    recording = _make_front_rear_recording("20260715_220000_N", tmp_path)

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([recording]))

    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append((source, kwargs))
        return "## Description\nSome view.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)

    args = parse_args([str(tmp_path), "--camera", "both"])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert len(calls) == 2
    assert calls[0][1]["task"] == "both"
    assert calls[1][1]["task"] == "ocr"
    assert (tmp_path / "20260715_220000_N.scene.txt").exists()
    assert (tmp_path / "20260715_220000_N.rear.scene.txt").exists()


def test_run_camera_rear_errors_without_rear_video(monkeypatch, tmp_path):
    recording = _make_front_rear_recording("20260715_230000_N", tmp_path, rear=False)

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([recording]))
    monkeypatch.setattr(bv_scribe, "describe_scene", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not describe - no rear video for --camera rear")
    ))

    args = parse_args([str(tmp_path), "--camera", "rear"])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_HAD_ERRORS
    assert not (tmp_path / "20260715_230000_N.rear.scene.txt").exists()


def test_run_raw_single_file_writes_next_to_video(monkeypatch, tmp_path):
    video = tmp_path / "clip1.mp4"
    video.write_bytes(b"x")

    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append((source, kwargs))
        return "## Description\nA road.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)

    args = parse_args([str(video), "--raw"])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert len(calls) == 1
    assert calls[0][1]["crop_top"] == 0.0
    assert calls[0][1]["crop_bottom"] == 0.0
    dest = tmp_path / "clip1.scene.txt"
    assert dest.exists()
    assert "A road." in dest.read_text(encoding="utf-8")


def test_run_raw_directory_processes_every_video_ignores_non_video(monkeypatch, tmp_path):
    for name in ("b.mp4", "a.mov", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")

    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append(source)
        return "## Description\nSome scene.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)

    args = parse_args([str(tmp_path), "--raw"])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert [c.name for c in calls] == ["a.mov", "b.mp4"]  # sorted, non-video skipped
    assert (tmp_path / "a.scene.txt").exists()
    assert (tmp_path / "b.scene.txt").exists()
    assert not (tmp_path / "notes.scene.txt").exists()


def test_run_raw_rejects_timestamp_selection(tmp_path):
    args = parse_args([str(tmp_path), "--raw", "--timestamp", "20260101"])

    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_ARGS_ERROR


def test_run_raw_rejects_camera_flag(tmp_path):
    args = parse_args([str(tmp_path), "--raw", "--camera", "rear"])

    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_ARGS_ERROR


def test_run_raw_trip_summary_writes_to_directory(monkeypatch, tmp_path):
    for name in ("a.mp4", "b.mp4"):
        (tmp_path / name).write_bytes(b"x")

    def fake_describe_scene(source, **kwargs):
        return f"## Description\nScene for {source.stem}.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)
    monkeypatch.setattr(
        bv_scribe, "summarize_trip",
        lambda segments, **kwargs: "Raw trip narrative.",
    )

    args = parse_args([str(tmp_path), "--raw", "--trip-summary"])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    summary = (tmp_path / "trip_summary.txt").read_text(encoding="utf-8")
    assert "Raw trip narrative." in summary
