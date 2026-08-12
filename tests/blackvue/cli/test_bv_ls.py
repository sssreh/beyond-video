from blackvue.archive.asset import Asset
from blackvue.cli.bv_ls import _asset_group_spans
from blackvue.cli.bv_ls import bv_ls
from blackvue.cli.bv_ls import main
from blackvue.core.camera_config import CameraConfig
from blackvue.core.camera_config import config_path
from blackvue.core.camera_config import save_camera_config


def test_asset_group_spans_merges_consecutive_same_group_assets():
    spans = _asset_group_spans(
        [
            Asset.DURATION,
            Asset.TRANSCRIPT,
            Asset.TRANSCRIPT_DIARIZED,
            Asset.TRANSLATION,
            Asset.TRANSLATION_DIARIZED,
            Asset.SUBTITLES,
        ]
    )

    assert spans == [
        (None, [Asset.DURATION]),
        ("Transcript", [Asset.TRANSCRIPT, Asset.TRANSCRIPT_DIARIZED]),
        ("Translate", [Asset.TRANSLATION, Asset.TRANSLATION_DIARIZED]),
        (None, [Asset.SUBTITLES]),
    ]


def test_asset_group_spans_keeps_ungrouped_assets_separate():
    # Two consecutive ungrouped assets must not be merged into one
    # span just because they're both group=None.
    spans = _asset_group_spans([Asset.DURATION, Asset.GPS])

    assert spans == [
        (None, [Asset.DURATION]),
        (None, [Asset.GPS]),
    ]


def test_asset_group_spans_does_not_merge_a_group_split_by_a_gap():
    # If a differently-grouped (or ungrouped) asset sits between two
    # assets that share a group label, they must not be merged - only
    # genuinely consecutive same-group assets share a span.
    spans = _asset_group_spans(
        [Asset.TRANSCRIPT, Asset.DURATION, Asset.TRANSCRIPT_DIARIZED]
    )

    assert spans == [
        ("Transcript", [Asset.TRANSCRIPT]),
        (None, [Asset.DURATION]),
        ("Transcript", [Asset.TRANSCRIPT_DIARIZED]),
    ]


def test_full_display_order_group_spans_are_well_formed():
    # Sanity check against the real, current display order - every
    # grouped span should have exactly the two members we expect, and
    # group labels should fit within the combined column width so the
    # header row stays aligned.
    assets = Asset.display_order()
    widths = {asset: max(len(asset.label), 3) for asset in assets}

    spans = _asset_group_spans(assets)

    grouped = {label: members for label, members in spans if label}

    assert set(grouped) == {"Scene", "Transcript", "Translate"}

    for label, members in grouped.items():
        span_width = sum(widths[a] for a in members) + (len(members) - 1)
        assert len(label) <= span_width


def test_asset_table_marks_line_up_under_their_own_header_column(tmp_path):
    # Regression test for a real off-by-one bug Christer spotted in a
    # live bv-ls run: the header row's prefix ("Recording" padded,
    # then two spaces) and each data row's prefix (the recording id
    # padded, then a space) used to differ by one character whenever
    # a recording id was longer than the word "Recording" - which is
    # every real recording id - silently shifting every X mark one
    # column early. Uses bv_ls()'s injectable `say` (no capsys needed)
    # so this can run in any harness.
    recording_id = "20260715_133255_N"
    assert len(recording_id) > len("Recording")

    (tmp_path / f"{recording_id}F.mp4").write_bytes(b"x")
    (tmp_path / f"{recording_id}I.mp4").write_bytes(b"x")
    (tmp_path / f"{recording_id}.3gf").write_bytes(b"x")
    (tmp_path / f"{recording_id}F.thm").write_bytes(b"x")
    (tmp_path / f"{recording_id}.aac").write_bytes(b"x")
    (tmp_path / f"{recording_id}.duration.txt").write_text("300")
    (tmp_path / f"{recording_id}.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")

    lines = []
    exit_code = bv_ls(str(tmp_path), say=lines.append)

    assert exit_code == 0

    asset_header = lines[1]
    row = lines[3]

    # The decisive check: header and row are built from the same
    # recording_width + column widths + separators, so if their
    # prefixes are the same length, their total lengths must match
    # too. This is what actually catches the one-character prefix
    # mismatch - a per-column "is there an X somewhere nearby" check
    # can pass by accident when a shift-by-one still lands inside a
    # wide-enough neighboring column.
    assert len(row) == len(asset_header), (
        f"row and header lengths differ ({len(row)} vs "
        f"{len(asset_header)}) - their column prefixes are out of "
        f"sync:\nheader: {asset_header!r}\nrow:    {row!r}"
    )

    # Belt and braces: every asset label present in asset_header
    # should have its X (if any) exactly centered in that column's
    # own character span, not merely somewhere near it.
    for label in ("Int", "3G", "FThm", "Aud", "Dur", "SRT"):
        start = asset_header.index(label)
        end = start + len(label)
        column = row[start:end]
        assert "X" in column, (
            f"expected an X somewhere in {row[start:end]!r} "
            f"(column {label!r} at {start}:{end}) - full header:\n"
            f"{asset_header!r}\nfull row:\n{row!r}"
        )


def test_main_reports_a_missing_path_cleanly_instead_of_a_traceback(
    tmp_path, capsys
):
    missing = tmp_path / "no-such-archive"

    exit_code = main([str(missing)])

    err = capsys.readouterr().err

    assert exit_code == 1
    assert "bv-ls" in err
    assert str(missing) in err
    assert "Traceback" not in err


def test_main_reports_a_file_given_as_path_cleanly(tmp_path, capsys):
    a_file = tmp_path / "not_a_folder.txt"
    a_file.write_text("x")

    exit_code = main([str(a_file)])

    err = capsys.readouterr().err

    assert exit_code == 1
    assert "bv-ls" in err
    assert str(a_file) in err
    assert "Traceback" not in err


def test_main_resolves_a_camera_id_to_its_configured_target(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "20260715_100000_NF.mp4").write_bytes(b"x")

    config_dir = tmp_path / "config"
    save_camera_config(
        config_path(config_dir, "Kirby"),
        CameraConfig(id="Kirby", name="Kirby", archive=archive),
    )

    exit_code = main(["Kirby", "--config-dir", str(config_dir)])

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "20260715_100000" in out


def test_trips_groups_close_recordings_and_shows_one_row_each(
    tmp_path, capsys
):
    # Two recordings 2 minutes apart (same trip), then a third an hour
    # later (its own trip).
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x" * 10)
    (tmp_path / "20260715_100200_NF.mp4").write_bytes(b"x" * 10)
    (tmp_path / "20260715_110000_NF.mp4").write_bytes(b"x" * 10)

    exit_code = bv_ls(str(tmp_path), trips=True)

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trip_20260715_100000_20260715_100200" in out
    assert "trip_20260715_110000_20260715_110000" in out


def test_trips_respects_max_gap_override(tmp_path, capsys):
    # 5 minutes apart: same trip under the default 5-minute gap (plus
    # its 10-second tolerance), but two separate trips once --max-gap
    # is tightened to 1 minute.
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_100500_NF.mp4").write_bytes(b"x")

    exit_code = bv_ls(str(tmp_path), trips=True, max_gap_minutes=1)

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trip_20260715_100000_20260715_100000" in out
    assert "trip_20260715_100500_20260715_100500" in out
    # Confirms it did NOT fall back to the 10-minute default and
    # merge them into a single trip.
    assert "trip_20260715_100000_20260715_100500" not in out


def test_trips_bridges_a_gap_when_gps_shows_movement_and_movement_flag_given(
    tmp_path, capsys
):
    # 30 minutes apart - would be two trips under the default 5-minute
    # gap, but the first recording's .gps file shows the vehicle still
    # moving right at the end of the recording, so they should bridge
    # into one trip - only when movement=True is explicitly given
    # (opt-in - see test_trips_does_not_bridge_by_default below for
    # why this isn't the default anymore).
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_100000_N.gps").write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "30.00,45.00,010124,,,A*6D\n"
    )
    (tmp_path / "20260715_103000_NF.mp4").write_bytes(b"x")

    exit_code = bv_ls(str(tmp_path), trips=True, movement=True)

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trip_20260715_100000_20260715_103000" in out


def test_trips_does_not_bridge_by_default(tmp_path, capsys):
    # Movement-based bridging is off by default - confirmed on a real
    # archive to have no ceiling on how large a gap it'll bridge (a
    # single GPS speed reading bridged a genuine 6-day gap into one
    # trip), so the plain --max-gap time rule is the only splitting
    # rule unless --movement is explicitly given.
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_100000_N.gps").write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "30.00,45.00,010124,,,A*6D\n"
    )
    (tmp_path / "20260715_103000_NF.mp4").write_bytes(b"x")

    exit_code = bv_ls(str(tmp_path), trips=True)

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trip_20260715_100000_20260715_100000" in out
    assert "trip_20260715_103000_20260715_103000" in out
    assert "trip_20260715_100000_20260715_103000" not in out


def test_main_movement_flag_enables_gps_bridging(tmp_path, capsys):
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_100000_N.gps").write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "30.00,45.00,010124,,,A*6D\n"
    )
    (tmp_path / "20260715_103000_NF.mp4").write_bytes(b"x")

    exit_code = main([str(tmp_path), "--trips", "--movement"])

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trip_20260715_100000_20260715_103000" in out


def test_main_leaves_movement_false_without_the_flag(tmp_path, capsys):
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_100000_N.gps").write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "30.00,45.00,010124,,,A*6D\n"
    )
    (tmp_path / "20260715_103000_NF.mp4").write_bytes(b"x")

    exit_code = main([str(tmp_path), "--trips"])

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trip_20260715_100000_20260715_103000" not in out


def test_trips_uses_duration_file_to_avoid_a_false_split(tmp_path, capsys):
    # The first recording starts at 10:00:00 and, per its
    # .duration.txt, really runs 12 minutes - so it doesn't end until
    # 10:12:00. The second recording starts at 10:11:00, actually
    # *before* that real end (a negative computed gap - always inside
    # any positive max_gap) - even though the raw start-to-start gap
    # (11 minutes) would exceed it.
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_100000_N.duration.txt").write_text("720\n")
    (tmp_path / "20260715_101100_NF.mp4").write_bytes(b"x")

    exit_code = bv_ls(str(tmp_path), trips=True)

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trip_20260715_100000_20260715_101100" in out


def test_no_duration_flag_ignores_duration_files(tmp_path, capsys):
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_100000_N.duration.txt").write_text("720\n")
    (tmp_path / "20260715_101100_NF.mp4").write_bytes(b"x")

    exit_code = bv_ls(str(tmp_path), trips=True, duration=False)

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trip_20260715_100000_20260715_100000" in out
    assert "trip_20260715_101100_20260715_101100" in out
    assert "trip_20260715_100000_20260715_101100" not in out


def test_trips_default_gap_tolerance_absorbs_a_few_seconds(tmp_path, capsys):
    # 5 minutes and 5 seconds apart - within the default 10-second
    # tolerance on top of the default 5-minute max-gap.
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_100505_NF.mp4").write_bytes(b"x")

    exit_code = bv_ls(str(tmp_path), trips=True)

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trip_20260715_100000_20260715_100505" in out


def test_gap_tolerance_can_be_tightened(tmp_path, capsys):
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_101005_NF.mp4").write_bytes(b"x")

    exit_code = bv_ls(
        str(tmp_path), trips=True, gap_tolerance_seconds=0
    )

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "trip_20260715_100000_20260715_100000" in out
    assert "trip_20260715_101005_20260715_101005" in out


def test_trips_defaults_to_a_five_minute_gap(tmp_path, capsys):
    (tmp_path / "20260715_100000_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_100400_NF.mp4").write_bytes(b"x")

    exit_code = bv_ls(str(tmp_path), trips=True)

    out = capsys.readouterr().out

    assert exit_code == 0
    # 4 minutes apart - one trip under the default 5-minute gap.
    assert "trip_20260715_100000_20260715_100400" in out
