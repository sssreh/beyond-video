import argparse

import pytest

from blackvue.cli.bv_download import parse_mode
from blackvue.cli.bv_download import select_by_context
from blackvue.cli.bv_download import select_by_mode
from blackvue.domain.recording import Recording


def recording(id_: str) -> Recording:
    return Recording(id=id_, entries=[])


def test_parse_mode_single():
    assert parse_mode("N") == {"N"}


def test_parse_mode_multiple_case_insensitive():
    assert parse_mode("n,p") == {"N", "P"}


def test_parse_mode_all():
    assert parse_mode("all") == {"N", "E", "M", "P"}
    assert parse_mode("All") == {"N", "E", "M", "P"}


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
