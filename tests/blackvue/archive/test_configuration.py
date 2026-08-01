import pytest

from blackvue.archive.configuration import RECORD_TIME_SUFFIX
from blackvue.archive.configuration import Configuration
from blackvue.archive.configuration import ConfigurationError
from blackvue.archive.configuration import parse_record_time_seconds
from blackvue.archive.configuration import read_record_time_snapshot
from blackvue.archive.configuration import write_record_time_snapshot

# A synthetic config.ini - the [Tab1]/RecordTime shape real BlackVue
# cameras use, but with no real Wi-Fi/cloud/camera-identifying values
# in it (see configuration.py's own module docstring for why real
# config.ini content must never appear anywhere in this repo, tests
# included - Christer's real file has Wi-Fi/cloud SSIDs and passwords
# in it).
_SYNTHETIC_CONFIG_INI = """[Tab1]
TimeSet=0
RecordTime=3
NormalRecord=1
[Wifi]
ap_ssid=SyntheticCam-0000
ap_pw=0000000000000000000000000000000000000000000000000000000000000000
[Cloud]
sta_ssid=SyntheticNetwork
sta_pw=0000000000000000000000000000000000000000000000000000000000000000
"""


def test_parse_record_time_seconds_reads_minutes_and_converts():
    assert parse_record_time_seconds(_SYNTHETIC_CONFIG_INI) == 180


def test_parse_record_time_seconds_raises_on_missing_section():
    with pytest.raises(ConfigurationError):
        parse_record_time_seconds("[Wifi]\nap_ssid=x\n")


def test_parse_record_time_seconds_raises_on_missing_key():
    with pytest.raises(ConfigurationError):
        parse_record_time_seconds("[Tab1]\nNormalRecord=1\n")


def test_parse_record_time_seconds_raises_on_non_integer_value():
    with pytest.raises(ConfigurationError):
        parse_record_time_seconds("[Tab1]\nRecordTime=not-a-number\n")


def test_parse_record_time_seconds_ignores_percent_signs_elsewhere():
    """interpolation=None must be in effect - a stray '%' in an
    unrelated field (plausible in a Wi-Fi password or userString)
    should never break parsing RecordTime."""

    text = "[Tab1]\nRecordTime=1\n[Tab3]\nuserString=100%sure\n"
    assert parse_record_time_seconds(text) == 60


def test_write_record_time_snapshot_writes_only_the_derived_integer(tmp_path):
    path = write_record_time_snapshot(tmp_path, "20260801_095509_N", 180)

    assert path.name == f"20260801_095509_N{RECORD_TIME_SUFFIX}"
    assert path.read_text(encoding="utf-8") == "180\n"


def test_read_record_time_snapshot_round_trips(tmp_path):
    path = write_record_time_snapshot(tmp_path, "20260801_095509_N", 60)

    assert read_record_time_snapshot(path) == 60


def test_read_record_time_snapshot_returns_none_for_missing_file(tmp_path):
    assert read_record_time_snapshot(tmp_path / "missing.record_time.txt") is None


def test_read_record_time_snapshot_returns_none_for_corrupt_file(tmp_path):
    path = tmp_path / "20260801_095509_N.record_time.txt"
    path.write_text("not-a-number\n", encoding="utf-8")

    assert read_record_time_snapshot(path) is None


def test_configuration_from_explicit_record_time_never_reads_the_path():
    """The record_time= kwarg must fully bypass file parsing - this is
    what lets Archive build a Configuration straight from a
    .record_time.txt snapshot without ever touching a raw config.ini."""

    configuration = Configuration("<fallback>", record_time=180)

    assert configuration.record_time == 180


def test_configuration_recording_id_strips_the_snapshot_suffix(tmp_path):
    path = write_record_time_snapshot(tmp_path, "20260801_095509_N", 180)
    configuration = Configuration(path, record_time=180)

    assert configuration.recording_id.value == "20260801_095509_N"


def test_configuration_maximum_gap_adds_tolerance():
    configuration = Configuration("<fallback>", record_time=180)

    assert configuration.maximum_gap == 190


def test_configuration_fallback_is_300_seconds():
    configuration = Configuration.fallback()

    assert configuration.record_time == 300
    assert configuration.maximum_gap == 310
