import argparse

import pytest

from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath

from blackvue.archive.configuration import RECORD_TIME_SUFFIX
from blackvue.cli import bv_download
from blackvue.cli.bv_download import EXIT_OK
from blackvue.cli.bv_download import EXIT_PARTIAL_FAILURE
from blackvue.cli.bv_download import DotProgress
from blackvue.cli.bv_download import _capture_record_time
from blackvue.cli.bv_download import _capture_record_time_from_sdcard
from blackvue.cli.bv_download import _destination_message
from blackvue.cli.bv_download import _run
from blackvue.cli.bv_download import _summarize_found_kinds
from blackvue.cli.bv_download import describe_recording_files
from blackvue.cli.bv_download import parse_args
from blackvue.cli.bv_download import parse_mode
from blackvue.cli.bv_download import select_by_context
from blackvue.cli.bv_download import select_by_mode
from blackvue.core.camera_config import CameraConfig
from blackvue.core.camera_config import config_path
from blackvue.core.camera_config import save_camera_config
from blackvue.core.endpoint import Endpoint
from blackvue.core.sdcard_camera import SdCardCamera
from blackvue.domain.recording import Recording
from blackvue.domain.vod_entry import VodEntry


def recording(id_: str) -> Recording:
    return Recording(id=id_, entries=[])


def vod_entry(name: str) -> VodEntry:
    """A synthetic VodEntry with a real path - enough for
    describe_recording_files(), which only reads .path.name and
    .is_video, both derived from the filename suffix."""

    return VodEntry(
        timestamp=datetime(2026, 7, 15, 13, 32, 55),
        path=PurePosixPath(name),
        fields={},
    )


class _FakeConfigClient:
    """A minimal stand-in for BlackVueClient - _capture_record_time()
    only ever calls .config() on it."""

    def __init__(self, config_text: str | Exception):
        self._config_text = config_text

    def config(self) -> str:
        if isinstance(self._config_text, Exception):
            raise self._config_text
        return self._config_text


def test_parse_mode_single():
    assert parse_mode("N") == {"N"}


def test_parse_mode_multiple_case_insensitive():
    assert parse_mode("n,p") == {"N", "P"}


def test_parse_mode_all():
    assert parse_mode("all") == {"N", "E", "M", "P", "A"}
    assert parse_mode("All") == {"N", "E", "M", "P", "A"}


def test_parse_mode_accepts_a():
    """"A" is a recording kind observed on real hardware alongside
    N/E/M/P - meaning unknown, but --mode should accept it like any
    other kind letter."""

    assert parse_mode("A") == {"A"}
    assert parse_mode("a,n") == {"A", "N"}


def test_parse_mode_rejects_invalid():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_mode("X")


def test_parse_mode_rejects_empty():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_mode("")


def test_select_by_mode_only_matching_kinds_get_video():
    recordings = [
        recording("20260101_000000_N"),
        recording("20260101_000100_E"),
        recording("20260101_000200_P"),
    ]

    result = list(select_by_mode(recordings, frozenset({"E"})))

    assert result == [
        (recordings[0], False),
        (recordings[1], True),
        (recordings[2], False),
    ]


def test_select_by_context_downloads_event_and_manual():
    n1 = recording("20260101_000000_N")
    n2 = recording("20260101_000100_N")
    e1 = recording("20260101_000200_E")
    n3 = recording("20260101_000300_N")
    m1 = recording("20260101_000400_M")
    p1 = recording("20260101_000500_P")
    n4 = recording("20260101_000600_N")

    result = list(
        select_by_context([n1, n2, e1, n3, m1, p1, n4])
    )

    assert result == [
        (n1, False),
        (n2, True),
        (e1, True),
        (n3, True),
        (m1, True),
        (p1, False),
        (n4, False),
    ]


def test_select_by_context_every_recording_yielded_exactly_once():
    recordings = [
        recording("20260101_000000_N"),
        recording("20260101_000100_E"),
        recording("20260101_000200_M"),
        recording("20260101_000300_P"),
    ]

    result = list(select_by_context(recordings))

    assert [item[0] for item in result] == recordings


def test_select_by_context_trailing_normal_gets_metadata_only():
    recordings = [recording("20260101_000000_N")]

    result = list(select_by_context(recordings))

    assert result == [(recordings[0], False)]


def test_dot_progress_prints_nothing_below_the_interval(capsys):
    progress = DotProgress(interval_bytes=1000)

    progress(999)

    assert capsys.readouterr().out == ""


def test_dot_progress_prints_one_dot_per_interval_crossed(capsys):
    progress = DotProgress(interval_bytes=1000)

    progress(600)
    progress(600)  # crosses 1000 once (accumulated 1200)

    assert capsys.readouterr().out == "."


def test_dot_progress_prints_multiple_dots_for_a_big_jump(capsys):
    progress = DotProgress(interval_bytes=1000)

    progress(3500)  # crosses 1000, 2000, and 3000

    assert capsys.readouterr().out == "..."


def test_dot_progress_accumulates_across_many_small_calls(capsys):
    progress = DotProgress(interval_bytes=1000)

    for _ in range(10):
        progress(150)  # 1500 total after 10 calls

    assert capsys.readouterr().out == "."


def test_capture_record_time_writes_a_snapshot_on_first_run(tmp_path):
    client = _FakeConfigClient("[Tab1]\nRecordTime=3\n")

    _capture_record_time(client, tmp_path, "20260801_095509_N", verbose=False)

    snapshots = sorted(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}"))
    assert len(snapshots) == 1
    assert snapshots[0].name == f"20260801_095509_N{RECORD_TIME_SUFFIX}"
    assert snapshots[0].read_text(encoding="utf-8") == "180\n"


def test_capture_record_time_is_a_noop_when_unchanged(tmp_path):
    client = _FakeConfigClient("[Tab1]\nRecordTime=3\n")

    _capture_record_time(client, tmp_path, "20260801_095509_N", verbose=False)
    _capture_record_time(client, tmp_path, "20260801_120000_N", verbose=False)

    snapshots = sorted(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}"))
    assert len(snapshots) == 1


def test_capture_record_time_writes_again_when_changed(tmp_path):
    _capture_record_time(
        _FakeConfigClient("[Tab1]\nRecordTime=3\n"),
        tmp_path,
        "20260801_095509_N",
        verbose=False,
    )
    _capture_record_time(
        _FakeConfigClient("[Tab1]\nRecordTime=1\n"),
        tmp_path,
        "20260901_000000_N",
        verbose=False,
    )

    snapshots = sorted(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}"))
    assert [s.name for s in snapshots] == [
        f"20260801_095509_N{RECORD_TIME_SUFFIX}",
        f"20260901_000000_N{RECORD_TIME_SUFFIX}",
    ]
    assert snapshots[1].read_text(encoding="utf-8") == "60\n"


def test_capture_record_time_backfills_an_earlier_anchor_when_unchanged(
    tmp_path,
):
    """Christer's real-world case: he downloaded a later batch first
    (anchored at 20260802_162130_N), then went back and downloaded an
    earlier recording (20260802_161928_N) whose RecordTime hadn't
    changed on the camera. The old "only write when the value
    changes" logic skipped the second write entirely, leaving
    20260802_161928_N uncovered by any snapshot - Archive.configuration()
    never applies a snapshot retroactively to recordings before its
    own anchor, so lookups for it fell through to the 300s fallback
    even though the real value was known and unchanged. A second,
    earlier-anchored snapshot must be written even though the value
    didn't change."""

    same_config = "[Tab1]\nRecordTime=3\n"

    _capture_record_time(
        _FakeConfigClient(same_config),
        tmp_path,
        "20260802_162130_N",
        verbose=False,
    )
    _capture_record_time(
        _FakeConfigClient(same_config),
        tmp_path,
        "20260802_161928_N",
        verbose=False,
    )

    snapshots = sorted(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}"))
    assert [s.name for s in snapshots] == [
        f"20260802_161928_N{RECORD_TIME_SUFFIX}",
        f"20260802_162130_N{RECORD_TIME_SUFFIX}",
    ]
    assert snapshots[0].read_text(encoding="utf-8") == "180\n"


def test_capture_record_time_still_noop_when_covered_and_unchanged(tmp_path):
    """The ordinary case this dedup exists for must still hold: a run
    whose earliest recording is already covered by an existing
    snapshot, with an unchanged value, writes nothing new - this is
    just test_capture_record_time_is_a_noop_when_unchanged's scenario
    restated to make the "covered" condition explicit alongside the
    new backfill test above."""

    client = _FakeConfigClient("[Tab1]\nRecordTime=3\n")

    _capture_record_time(client, tmp_path, "20260801_095509_N", verbose=False)
    _capture_record_time(client, tmp_path, "20260801_120000_N", verbose=False)

    snapshots = sorted(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}"))
    assert len(snapshots) == 1


def test_capture_record_time_backfill_message_is_verbose_only(tmp_path, capsys):
    same_config = "[Tab1]\nRecordTime=3\n"

    _capture_record_time(
        _FakeConfigClient(same_config),
        tmp_path,
        "20260802_162130_N",
        verbose=True,
    )
    capsys.readouterr()  # discard the first run's own message

    _capture_record_time(
        _FakeConfigClient(same_config),
        tmp_path,
        "20260802_161928_N",
        verbose=True,
    )

    out = capsys.readouterr().out
    assert "extended" in out
    assert "20260802_161928_N" in out


def test_capture_record_time_never_persists_the_raw_config_text(tmp_path):
    """Only the derived integer may ever land on disk - never the raw
    config.ini text, which also carries Wi-Fi/cloud credentials."""

    client = _FakeConfigClient(
        "[Tab1]\nRecordTime=3\n[Wifi]\nap_ssid=SyntheticCam\nap_pw=SECRET\n"
    )

    _capture_record_time(client, tmp_path, "20260801_095509_N", verbose=False)

    for path in tmp_path.iterdir():
        assert "SyntheticCam" not in path.read_text(encoding="utf-8")
        assert "SECRET" not in path.read_text(encoding="utf-8")


def test_capture_record_time_swallows_config_fetch_errors(tmp_path, capsys):
    client = _FakeConfigClient(RuntimeError("Unable to fetch /Config/config.ini"))

    _capture_record_time(client, tmp_path, "20260801_095509_N", verbose=True)

    assert list(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}")) == []
    assert "RecordTime" in capsys.readouterr().err


def test_capture_record_time_swallows_unparseable_config(tmp_path):
    client = _FakeConfigClient("[Tab1]\nNormalRecord=1\n")

    _capture_record_time(client, tmp_path, "20260801_095509_N", verbose=False)

    assert list(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}")) == []


class _FakeSdCardConfigCamera:
    """A minimal stand-in for SdCardCamera - _capture_record_time_from_
    sdcard() only ever calls .read_config_text() on it."""

    def __init__(self, config_text: str | None):
        self._config_text = config_text

    def read_config_text(self) -> str | None:
        return self._config_text


def test_capture_record_time_from_sdcard_writes_a_snapshot(tmp_path):
    camera = _FakeSdCardConfigCamera("[Tab1]\nRecordTime=3\n")

    _capture_record_time_from_sdcard(
        camera, tmp_path, "20260801_095509_N", verbose=False
    )

    snapshots = sorted(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}"))
    assert len(snapshots) == 1
    assert snapshots[0].read_text(encoding="utf-8") == "180\n"


def test_capture_record_time_from_sdcard_is_a_noop_when_unchanged(tmp_path):
    camera = _FakeSdCardConfigCamera("[Tab1]\nRecordTime=3\n")

    _capture_record_time_from_sdcard(
        camera, tmp_path, "20260801_095509_N", verbose=False
    )
    _capture_record_time_from_sdcard(
        camera, tmp_path, "20260801_120000_N", verbose=False
    )

    snapshots = sorted(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}"))
    assert len(snapshots) == 1


def test_capture_record_time_from_sdcard_skips_when_no_config_ini(
    tmp_path, capsys
):
    camera = _FakeSdCardConfigCamera(None)

    _capture_record_time_from_sdcard(
        camera, tmp_path, "20260801_095509_N", verbose=True
    )

    assert list(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}")) == []
    assert "no config.ini found on the SD card" in capsys.readouterr().err


def test_capture_record_time_from_sdcard_swallows_unparseable_config(
    tmp_path,
):
    camera = _FakeSdCardConfigCamera("[Tab1]\nNormalRecord=1\n")

    _capture_record_time_from_sdcard(
        camera, tmp_path, "20260801_095509_N", verbose=False
    )

    assert list(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}")) == []


def test_dot_progress_finish_adds_newline_only_if_a_dot_was_printed(capsys):
    quiet = DotProgress(interval_bytes=1000)
    quiet.finish()
    assert capsys.readouterr().out == ""

    active = DotProgress(interval_bytes=1000)
    active(1000)
    capsys.readouterr()  # discard the dot itself
    active.finish()
    assert capsys.readouterr().out == "\n"


def test_parse_args_files_defaults_to_false():
    args = parse_args(["mycar", "--dry-run"])

    assert args.files is False


def test_parse_args_files_requires_dry_run(capsys):
    with pytest.raises(SystemExit):
        parse_args(["mycar", "--files"])

    assert "--files requires --dry-run" in capsys.readouterr().err


def test_parse_args_files_with_dry_run_is_accepted():
    args = parse_args(["mycar", "--dry-run", "--files"])

    assert args.files is True


def test_parse_args_id_alone_still_works():
    """The existing bv-config-based flow: id given, no --host/--target
    involved at all."""

    args = parse_args(["mycar"])

    assert args.id == "mycar"
    assert args.host is None
    assert args.target is None


def test_parse_args_requires_id_or_host(capsys):
    with pytest.raises(SystemExit):
        parse_args([])

    assert "either ID, --host, or --sdcard is required" in capsys.readouterr().err


def test_parse_args_host_cannot_combine_with_id(capsys):
    with pytest.raises(SystemExit):
        parse_args(["mycar", "--host", "192.168.0.1", "--target", "/tmp/x"])

    assert "--host cannot be combined with ID" in capsys.readouterr().err


def test_parse_args_host_requires_target(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--host", "192.168.0.1"])

    assert "--host requires --target" in capsys.readouterr().err


def test_parse_args_target_requires_host(capsys):
    with pytest.raises(SystemExit):
        parse_args(["mycar", "--target", "/tmp/x"])

    assert "--target cannot be combined with ID" in capsys.readouterr().err


def test_parse_args_host_and_target_accepted():
    args = parse_args(["--host", "192.168.0.1", "--target", "/tmp/x"])

    assert args.id is None
    assert args.host == "192.168.0.1"
    assert args.target == Path("/tmp/x")


def test_parse_args_sdcard_alone_requires_id_or_target(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--sdcard", "/tmp/sd"])

    assert "--sdcard requires ID or --target" in capsys.readouterr().err


def test_parse_args_sdcard_cannot_combine_with_host(capsys):
    with pytest.raises(SystemExit):
        parse_args(
            ["--host", "192.168.0.1", "--sdcard", "/tmp/sd", "--target", "/tmp/x"]
        )

    assert "--host cannot be combined with --sdcard" in capsys.readouterr().err


def test_parse_args_id_and_sdcard_accepted():
    args = parse_args(["mycar", "--sdcard", "/tmp/sd"])

    assert args.id == "mycar"
    assert args.sdcard == Path("/tmp/sd")
    assert args.target is None


def test_parse_args_sdcard_and_target_accepted_without_id():
    args = parse_args(["--sdcard", "/tmp/sd", "--target", "/tmp/out"])

    assert args.id is None
    assert args.sdcard == Path("/tmp/sd")
    assert args.target == Path("/tmp/out")


def test_parse_args_id_and_target_rejected():
    # --target only ever pairs with --host or a bare --sdcard (no ID) -
    # combining it with ID as well is ambiguous about which
    # destination wins, so it's rejected rather than guessed at.
    with pytest.raises(SystemExit):
        parse_args(["mycar", "--sdcard", "/tmp/sd", "--target", "/tmp/out"])


def test_describe_recording_files_all_downloaded_when_video_wanted():
    """want_video=True mirrors select=None at the real download call
    site - every entry, video or not, is marked to download."""

    rec = recording("20260101_000000_N")
    rec.entries = [
        vod_entry("20260101_000000_NF.mp4"),
        vod_entry("20260101_000000_NR.mp4"),
        vod_entry("20260101_000000_NF.thm"),
        vod_entry("20260101_000000_N.gps"),
    ]

    result = list(describe_recording_files(rec, True))

    assert result == [
        ("20260101_000000_NF.mp4", True),
        ("20260101_000000_NR.mp4", True),
        ("20260101_000000_NF.thm", True),
        ("20260101_000000_N.gps", True),
    ]


def test_describe_recording_files_video_skipped_when_metadata_only():
    """want_video=False mirrors the metadata-only select= lambda at
    the real download call site - only non-video entries download,
    video entries are marked skip."""

    rec = recording("20260101_000000_N")
    rec.entries = [
        vod_entry("20260101_000000_NF.mp4"),
        vod_entry("20260101_000000_NR.mp4"),
        vod_entry("20260101_000000_NF.thm"),
        vod_entry("20260101_000000_N.gps"),
    ]

    result = list(describe_recording_files(rec, False))

    assert result == [
        ("20260101_000000_NF.mp4", False),
        ("20260101_000000_NR.mp4", False),
        ("20260101_000000_NF.thm", True),
        ("20260101_000000_N.gps", True),
    ]


def test_describe_recording_files_empty_recording_yields_nothing():
    rec = recording("20260101_000000_N")

    assert list(describe_recording_files(rec, True)) == []


def test_destination_message_names_the_camera_and_folder():
    message = _destination_message(
        "Kirby", Path("/archive/kirby"), dry_run=False
    )

    assert message == "bv-download: Kirby: downloading into /archive/kirby"


def test_destination_message_dry_run_uses_would_wording():
    # Matches bv-export's own dry-run wording convention ("would ...")
    # rather than claiming a download that isn't actually happening.
    message = _destination_message(
        "Kirby", Path("/archive/kirby"), dry_run=True
    )

    assert message == "bv-download: Kirby: would download into /archive/kirby"


def test_destination_message_works_for_a_host_target_run_too():
    # --host/--target runs use the host address itself as display_name
    # (no configured camera name) - same message shape either way.
    message = _destination_message(
        "10.99.88.1", Path("/tmp/dashcam"), dry_run=False
    )

    assert message == "bv-download: 10.99.88.1: downloading into /tmp/dashcam"


class _FakeDownloadClient:
    """A minimal stand-in for BlackVueClient, only ever asked for its
    .config() by _capture_record_time() - but --host/--target runs
    (used by the tests below) skip that step entirely, so nothing on
    this needs to actually work."""


class _FakeDownloadCamera:
    """A stand-in for BlackVueCamera whose probe_missing_sidecars()/
    download() raise OSError for one chosen recording id, so the
    skip-and-continue behaviour in _run()'s per-recording loop can be
    exercised without a real camera. `fail_on` names the step
    ("probe" or "download") that should raise for `failing_id`."""

    def __init__(
        self,
        recordings: list[Recording],
        *,
        failing_id: str | None = None,
        fail_on: str = "download",
    ):
        self._recordings = recordings
        self._failing_id = failing_id
        self._fail_on = fail_on
        self.downloaded_ids: list[str] = []

    def recordings(self) -> list[Recording]:
        return self._recordings

    def probe_missing_sidecars(self, recording: Recording) -> list[VodEntry]:
        if self._fail_on == "probe" and recording.id == self._failing_id:
            raise OSError("timed out")
        return []

    def download(self, recording, destination, *, select=None, on_bytes=None) -> bool:
        if self._fail_on == "download" and recording.id == self._failing_id:
            raise OSError("timed out")
        self.downloaded_ids.append(recording.id)
        return True


def _host_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    args = parse_args(
        [
            "--host",
            "10.99.88.1",
            "--target",
            str(tmp_path),
            "--yes",
            "--mode",
            "all",
        ]
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_run_skips_a_recording_whose_download_fails_and_continues(
    tmp_path, monkeypatch, capsys
):
    recordings = [
        recording("20260803_161020_N"),
        recording("20260803_161120_N"),
        recording("20260803_161221_N"),
    ]
    camera = _FakeDownloadCamera(
        recordings, failing_id="20260803_161120_N", fail_on="download"
    )

    monkeypatch.setattr(
        bv_download,
        "connect",
        lambda endpoints, timeout: (
            Endpoint(name="host", address="10.99.88.1"),
            _FakeDownloadClient(),
        ),
    )
    monkeypatch.setattr(bv_download, "BlackVueCamera", lambda client: camera)

    exit_code = _run(_host_args(tmp_path))

    # The failing recording is skipped, but its neighbours on either
    # side are still downloaded - one bad recording no longer aborts
    # the whole batch (see EXIT_PARTIAL_FAILURE's own docstring/entry
    # in the exit-code table).
    assert camera.downloaded_ids == [
        "20260803_161020_N",
        "20260803_161221_N",
    ]
    assert exit_code == EXIT_PARTIAL_FAILURE

    err = capsys.readouterr().err
    assert "20260803_161120_N" in err
    assert "download failed" in err
    assert "timed out" in err


def test_run_still_downloads_a_recording_whose_sidecar_probe_fails(
    tmp_path, monkeypatch, capsys
):
    """A sidecar-probe failure (transient WiFi/timeout hitting the
    opportunistic .gps/.3gf/.thm check) must not stop the recording's
    actual video from being attempted - see the comment in _run()'s
    per-recording loop. This matters most for a recording with a
    partially-downloaded video already on disk from an earlier run:
    camera.download()'s own resume logic is what fixes that, and it
    never got a chance to run if a flaky probe skipped the recording
    outright."""

    recordings = [
        recording("20260803_161020_N"),
        recording("20260803_161120_N"),
    ]
    camera = _FakeDownloadCamera(
        recordings, failing_id="20260803_161020_N", fail_on="probe"
    )

    monkeypatch.setattr(
        bv_download,
        "connect",
        lambda endpoints, timeout: (
            Endpoint(name="host", address="10.99.88.1"),
            _FakeDownloadClient(),
        ),
    )
    monkeypatch.setattr(bv_download, "BlackVueCamera", lambda client: camera)

    exit_code = _run(_host_args(tmp_path))

    # Both recordings get downloaded - the failed probe is reported,
    # but doesn't stop the video download from being attempted, and
    # doesn't count as a batch failure on its own.
    assert camera.downloaded_ids == [
        "20260803_161020_N",
        "20260803_161120_N",
    ]
    assert exit_code == EXIT_OK

    err = capsys.readouterr().err
    assert "20260803_161020_N" in err
    assert "couldn't check for sidecar files" in err


def test_run_returns_ok_when_every_recording_succeeds(
    tmp_path, monkeypatch, capsys
):
    recordings = [recording("20260803_161020_N")]
    camera = _FakeDownloadCamera(recordings)

    monkeypatch.setattr(
        bv_download,
        "connect",
        lambda endpoints, timeout: (
            Endpoint(name="host", address="10.99.88.1"),
            _FakeDownloadClient(),
        ),
    )
    monkeypatch.setattr(bv_download, "BlackVueCamera", lambda client: camera)

    exit_code = _run(_host_args(tmp_path))

    assert exit_code == EXIT_OK
    assert "failed and were skipped" not in capsys.readouterr().err


def test_summarize_found_kinds_all_three():
    found = [
        vod_entry("20260803_143738_N.gps"),
        vod_entry("20260803_143738_N.3gf"),
        vod_entry("20260803_143738_NF.thm"),
        vod_entry("20260803_143738_NI.thm"),
        vod_entry("20260803_143738_NR.thm"),
    ]

    # Multiple thumbnail files (one per direction) collapse to a
    # single "thumbnails" label - the point is fewer words on one
    # line, not naming every direction that got one.
    assert _summarize_found_kinds(found) == "gps, 3gf and thumbnails"


def test_summarize_found_kinds_single_kind():
    found = [vod_entry("20260803_143738_N.gps")]

    assert _summarize_found_kinds(found) == "gps"


def test_summarize_found_kinds_two_kinds():
    found = [
        vod_entry("20260803_143738_N.gps"),
        vod_entry("20260803_143738_NF.thm"),
    ]

    assert _summarize_found_kinds(found) == "gps and thumbnails"


def test_summarize_found_kinds_empty():
    assert _summarize_found_kinds([]) == ""


def test_run_verbose_prints_short_kind_summary_not_every_filename(
    tmp_path, monkeypatch, capsys
):
    rec = recording("20260803_143738_N")
    camera = _FakeDownloadCamera([rec])

    def fake_probe(recording_):
        found = [
            vod_entry("20260803_143738_N.gps"),
            vod_entry("20260803_143738_N.3gf"),
            vod_entry("20260803_143738_NF.thm"),
            vod_entry("20260803_143738_NI.thm"),
            vod_entry("20260803_143738_NR.thm"),
        ]
        recording_.entries.extend(found)
        return found

    monkeypatch.setattr(camera, "probe_missing_sidecars", fake_probe)
    monkeypatch.setattr(
        bv_download,
        "connect",
        lambda endpoints, timeout: (
            Endpoint(name="host", address="10.99.88.1"),
            _FakeDownloadClient(),
        ),
    )
    monkeypatch.setattr(bv_download, "BlackVueCamera", lambda client: camera)

    _run(_host_args(tmp_path, verbose=True))

    out = capsys.readouterr().out
    assert (
        "20260803_143738_N: found gps, 3gf and thumbnails for downloading"
        in out
    )
    # The old message named every individual filename - confirm none
    # of the per-direction thumbnail filenames leak back in.
    assert "20260803_143738_NF.thm" not in out
    assert "not listed by the camera's own recording listing" not in out


# ---------------------------------------------------------------------------
# --sdcard: import from a mounted SD card / removable media.
# ---------------------------------------------------------------------------


class _FakeScanSummary:
    def __init__(self, total_files_seen: int, recognized_file_count: int):
        self.total_files_seen = total_files_seen
        self.recognized_file_count = recognized_file_count


class _FakeSdCardCamera:
    """A stand-in for SdCardCamera - mirrors _FakeDownloadCamera's own
    shape but adds scan_summary()/read_config_text(), the two methods
    only the --sdcard path calls."""

    def __init__(
        self,
        recordings: list[Recording],
        *,
        total_files_seen: int | None = None,
        recognized_file_count: int | None = None,
        config_text: str | None = None,
    ):
        self._recordings = recordings
        self._summary = _FakeScanSummary(
            total_files_seen
            if total_files_seen is not None
            else len(recordings),
            recognized_file_count
            if recognized_file_count is not None
            else len(recordings),
        )
        self._config_text = config_text
        self.downloaded_ids: list[str] = []

    def recordings(self) -> list[Recording]:
        return self._recordings

    def scan_summary(self) -> _FakeScanSummary:
        return self._summary

    def probe_missing_sidecars(self, recording: Recording) -> list[VodEntry]:
        return []

    def download(self, recording, destination, *, select=None, on_bytes=None) -> bool:
        self.downloaded_ids.append(recording.id)
        return True

    def read_config_text(self) -> str | None:
        return self._config_text


def _sdcard_args(tmp_path: Path, sdcard: Path, **overrides) -> argparse.Namespace:
    args = parse_args(
        [
            "--sdcard",
            str(sdcard),
            "--target",
            str(tmp_path),
            "--yes",
            "--mode",
            "all",
        ]
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_run_sdcard_zero_match_prints_a_clear_message(
    tmp_path, monkeypatch, capsys
):
    # Christer's own emulated test card scenario: the mounted folder
    # has files, but none of them follow BlackVue's naming convention.
    camera = _FakeSdCardCamera(
        [], total_files_seen=2, recognized_file_count=0
    )
    monkeypatch.setattr(bv_download, "SdCardCamera", lambda root: camera)

    args = _sdcard_args(tmp_path, Path("/fake/sdcard"), id=None)
    exit_code = _run(args)

    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    assert "no BlackVue-named recordings found" in out
    assert "2 file(s) scanned" in out


def test_run_sdcard_bare_run_downloads_and_skips_record_time(
    tmp_path, monkeypatch
):
    # --sdcard + --target with no ID is a bare one-off, same as
    # --host/--target - no RecordTime snapshot should be written.
    rec = recording("20260802_162130_N")
    camera = _FakeSdCardCamera([rec], config_text="[Tab1]\nRecordTime=3\n")
    monkeypatch.setattr(bv_download, "SdCardCamera", lambda root: camera)

    args = _sdcard_args(tmp_path, Path("/fake/sdcard"))
    exit_code = _run(args)

    assert exit_code == EXIT_OK
    assert camera.downloaded_ids == ["20260802_162130_N"]
    assert list(tmp_path.glob(f"*{RECORD_TIME_SUFFIX}")) == []


def test_run_sdcard_with_id_uses_the_configured_archive_as_destination(
    tmp_path, monkeypatch
):
    rec = recording("20260802_162130_N")
    camera = _FakeSdCardCamera([rec], config_text="[Tab1]\nRecordTime=3\n")
    monkeypatch.setattr(bv_download, "SdCardCamera", lambda root: camera)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    archive = tmp_path / "archive"
    save_camera_config(
        config_path(config_dir, "GP"),
        CameraConfig(id="GP", name="GoPro test", archive=archive, endpoints=[]),
    )

    args = parse_args(
        [
            "GP",
            "--sdcard",
            "/fake/sdcard",
            "--config-dir",
            str(config_dir),
            "--yes",
            "--mode",
            "all",
        ]
    )
    exit_code = _run(args)

    assert exit_code == EXIT_OK
    assert camera.downloaded_ids == ["20260802_162130_N"]
    # RecordTime capture runs for an ID-backed --sdcard import, same as
    # a normal network download - a snapshot should land in the real
    # configured archive directory.
    assert list(archive.glob(f"*{RECORD_TIME_SUFFIX}")) != []


def test_run_sdcard_with_id_does_not_require_configured_endpoints(
    tmp_path, monkeypatch
):
    # A camera config used only for --sdcard imports may have zero
    # [[endpoint]] entries - unlike the network path, --sdcard never
    # touches them, so the "no [[endpoint]] entries found" guard must
    # not fire here.
    camera = _FakeSdCardCamera([])
    monkeypatch.setattr(bv_download, "SdCardCamera", lambda root: camera)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    archive = tmp_path / "archive"
    save_camera_config(
        config_path(config_dir, "GP"),
        CameraConfig(id="GP", name="GoPro test", archive=archive, endpoints=[]),
    )

    args = parse_args(
        [
            "GP",
            "--sdcard",
            "/fake/sdcard",
            "--config-dir",
            str(config_dir),
            "--yes",
        ]
    )
    exit_code = _run(args)

    assert exit_code == EXIT_OK
