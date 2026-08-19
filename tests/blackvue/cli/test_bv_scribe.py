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


def test_parse_args_max_frames_and_video_total_pixels_defaults():
    """2026-08-19: Christer asked for 64 frames/video (~3s apart) without
    re-triggering the earlier "heavy performance penalty" from raising
    max_frames alone - see SceneOptions' own 2026-08-19 addendum in
    scene.py. --max-frames's default moved 16->64, and --max-pixels/
    --resized-width/--resized-height stopped applying to video at all
    (photo-only now) in favor of the new --video-total-pixels, which is
    the knob that actually keeps max_frames a resolution/frame-count
    tradeoff instead of a pure cost multiplier."""

    args = parse_args(["/some/archive"])

    assert args.max_frames == 64
    assert args.video_total_pixels == 16 * 1092 * 588
    # Still present and still applied to photos - just no longer wired
    # into video's SceneOptions kwargs (see test below).
    assert args.max_pixels == 360 * 420
    assert args.resized_width == 1092
    assert args.resized_height == 588


def test_scene_kwargs_passes_video_total_pixels_not_just_photo_fields():
    """_scene_kwargs() must forward video_total_pixels into
    SceneOptions alongside (not instead of) max_pixels/resized_width/
    resized_height - describe_scene()'s photo branch still reads the
    latter three, only its video branch switched to total_pixels-style
    budgeting (see scene.py's describe_scene() video-branch comment)."""

    args = parse_args(["/some/archive", "--video-total-pixels", "12345"])
    kwargs = bv_scribe._scene_kwargs(args)

    assert kwargs["video_total_pixels"] == 12345
    assert kwargs["max_pixels"] == 360 * 420
    assert kwargs["resized_width"] == 1092
    assert kwargs["resized_height"] == 588
    assert kwargs["max_frames"] == 64


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


def test_run_skips_parking_recordings_entirely(monkeypatch, tmp_path, capsys):
    """Parking-mode recordings are never considered for bv-scribe at
    all - not even attempted - regardless of --overwrite/whatever else.
    See the "Make bv-scribe skip Parking recordings" entry in
    WORKING_CONTEXT.md for why (unlike bv-generate --describe-scene,
    which deliberately does run on them)."""

    normal = _make_recording("20260715_134010_N", tmp_path)
    parking = _make_recording("20260715_140000_P", tmp_path)

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([normal, parking]))

    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append(source)
        return "## Description\nRoutine driving.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)

    args = parse_args([str(tmp_path)])
    exit_code = bv_scribe._run(args)

    assert exit_code == bv_scribe.EXIT_OK
    assert len(calls) == 1
    assert not (tmp_path / "20260715_140000_P.scene.txt").exists()
    assert "skipping 1 parking-mode recording" in capsys.readouterr().out


def test_run_survives_one_recordings_failure_and_reports_it(monkeypatch, tmp_path):
    """A single recording's failure (e.g. a network read error on a
    \\\\NAS\\ archive) must not kill the rest of an hours-long batch -
    see the "Make bv-scribe skip Parking recordings + survive
    per-file failures" entry in WORKING_CONTEXT.md. Confirmed as a
    real bug: the exception used to escape all the way to bv-web's
    JobRunner._spawn(), which reported it as a fatal job error and
    stopped a 902-recording run partway through."""

    bad = _make_recording("20260715_150000_N", tmp_path)
    good = _make_recording("20260715_160000_N", tmp_path)

    monkeypatch.setattr(bv_scribe, "Archive", _FakeArchive([bad, good]))

    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append(source)
        if "150000" in str(source):
            raise OSError("Error reading \\\\nas\\archive\\20260715_150000_NF.mp4")
        return "## Description\nRoutine driving.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)

    say_lines = []
    args = parse_args([str(tmp_path)])
    exit_code = bv_scribe._run(args, say=say_lines.append)

    assert exit_code == bv_scribe.EXIT_HAD_ERRORS
    assert len(calls) == 2  # both recordings attempted - the failure didn't stop the batch
    assert (tmp_path / "20260715_160000_N.scene.txt").exists()
    assert not (tmp_path / "20260715_150000_N.scene.txt").exists()
    assert any("1 recording(s) failed" in line for line in say_lines)
    assert any("20260715_150000_N" in line and "Error reading" in line for line in say_lines)


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


# ---------------------------------------------------------------------------
# Start/finished timing lines - same pattern as bv-search's own _run() (see
# test_bv_search.py). bv-scribe is the command Christer originally asked
# for this on (a 902-recording batch with no timing output at all - see
# WORKING_CONTEXT.md) - _run() now reports when it started and how long it
# took, on every exit path, covering both archive mode and --raw mode
# (see _run_dispatch()'s own docstring for why they share one wrapper).
# ---------------------------------------------------------------------------


def test_run_prints_started_and_finished_lines_around_an_archive_batch(tmp_path):
    args = parse_args([str(tmp_path)])
    messages = []

    bv_scribe._run(args, say=messages.append, warn=messages.append)

    assert any(m.startswith("bv-scribe: started ") for m in messages)
    assert any(
        m.startswith("bv-scribe: finished ") and m.rstrip().endswith("s)")
        for m in messages
    )
    started_index = next(
        i for i, m in enumerate(messages) if m.startswith("bv-scribe: started ")
    )
    finished_index = next(
        i for i, m in enumerate(messages) if m.startswith("bv-scribe: finished ")
    )
    assert started_index < finished_index


def test_run_prints_started_and_finished_lines_around_a_raw_batch(monkeypatch, tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")

    def fake_describe_scene(source, **kwargs):
        return "## Description\nSome view.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_scribe, "describe_scene", fake_describe_scene)

    args = parse_args([str(tmp_path), "--raw"])
    messages = []

    bv_scribe._run(args, say=messages.append, warn=messages.append)

    assert any(m.startswith("bv-scribe: started ") for m in messages)
    assert any(m.startswith("bv-scribe: finished ") for m in messages)


def test_run_prints_finished_line_even_when_the_time_parser_rejects_input(tmp_path):
    args = parse_args([str(tmp_path), "--timestamp", "abc"])
    messages = []

    exit_code = bv_scribe._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_scribe.EXIT_ARGS_ERROR
    assert any(m.startswith("bv-scribe: started ") for m in messages)
    assert any(m.startswith("bv-scribe: finished ") for m in messages)
