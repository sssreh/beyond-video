import argparse

import pytest

from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath

from blackvue.archive.configuration import RECORD_TIME_SUFFIX
from blackvue.cli.bv_download import DotProgress
from blackvue.cli.bv_download import _capture_record_time
from blackvue.cli.bv_download import describe_recording_files
from blackvue.cli.bv_download import parse_args
from blackvue.cli.bv_download import parse_mode
from blackvue.cli.bv_download import select_by_context
from blackvue.cli.bv_download import select_by_mode
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

    assert "either ID or --host is required" in capsys.readouterr().err


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

    assert "--target requires --host" in capsys.readouterr().err


def test_parse_args_host_and_target_accepted():
    args = parse_args(["--host", "192.168.0.1", "--target", "/tmp/x"])

    assert args.id is None
    assert args.host == "192.168.0.1"
    assert args.target == Path("/tmp/x")


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
