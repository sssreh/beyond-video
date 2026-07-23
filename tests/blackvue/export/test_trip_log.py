from blackvue.export.trip_log import TripLog


def test_open_writes_the_header_immediately(tmp_path):
    log = TripLog.open(tmp_path, trip_label="trip_20260715_100000", command="bv-export --stitch")
    log.close()

    text = (tmp_path / "trip.log").read_text(encoding="utf-8")
    assert "=== bv-export trip log: trip_20260715_100000 ===" in text
    assert "Started:" in text
    assert "Command: bv-export --stitch" in text


def test_membership_writes_a_section_header_once(tmp_path):
    log = TripLog.open(tmp_path, trip_label="trip", command="bv-export")
    log.membership("20260715_100000_N", "first recording in the archive")
    log.membership("20260715_100500_N", "continues the trip")
    log.close()

    text = (tmp_path / "trip.log").read_text(encoding="utf-8")
    assert text.count("--- Trip membership ---") == 1
    assert "20260715_100000_N: first recording in the archive" in text
    assert "20260715_100500_N: continues the trip" in text


def test_step_writes_a_section_header_once_and_a_timestamp(tmp_path):
    log = TripLog.open(tmp_path, trip_label="trip", command="bv-export")
    log.step("concatenated front.mp4 from 2 recording(s)")
    log.step("wrote trip.gpx (10 fix(es))")
    log.close()

    text = (tmp_path / "trip.log").read_text(encoding="utf-8")
    assert text.count("--- Export steps ---") == 1
    lines = [l for l in text.splitlines() if "concatenated front.mp4" in l]
    assert len(lines) == 1
    # HH:MM:SS timestamp prefix.
    assert lines[0][2] == ":" and lines[0][5] == ":"


def test_step_appends_elapsed_seconds_when_given(tmp_path):
    log = TripLog.open(tmp_path, trip_label="trip", command="bv-export")
    log.step("rendered map.mp4", elapsed_seconds=181.3)
    log.close()

    text = (tmp_path / "trip.log").read_text(encoding="utf-8")
    assert "rendered map.mp4 (181.3s)" in text


def test_warning_is_recorded_as_a_step_prefixed_with_warning(tmp_path):
    log = TripLog.open(tmp_path, trip_label="trip", command="bv-export")
    log.warning("stitch: no GPS data to auto-pick a layout from")
    log.close()

    text = (tmp_path / "trip.log").read_text(encoding="utf-8")
    assert "WARNING: stitch: no GPS data to auto-pick a layout from" in text


def test_close_writes_a_finished_footer_by_default(tmp_path):
    log = TripLog.open(tmp_path, trip_label="trip", command="bv-export")
    log.close()

    text = (tmp_path / "trip.log").read_text(encoding="utf-8")
    assert "Finished:" in text
    assert "Did not finish cleanly" not in text


def test_close_failed_writes_a_did_not_finish_cleanly_footer(tmp_path):
    log = TripLog.open(tmp_path, trip_label="trip", command="bv-export")
    log.close(failed=True)

    text = (tmp_path / "trip.log").read_text(encoding="utf-8")
    assert "Did not finish cleanly" in text
    assert "ran for" in text


def test_context_manager_marks_failed_on_an_exception():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            with TripLog.open(tmp_path, trip_label="trip", command="bv-export") as log:
                log.step("starting something")
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        text = (tmp_path / "trip.log").read_text(encoding="utf-8")
        assert "Did not finish cleanly" in text
        assert "starting something" in text


def test_context_manager_marks_finished_cleanly_with_no_exception(tmp_path):
    with TripLog.open(tmp_path, trip_label="trip", command="bv-export") as log:
        log.step("a step")

    text = (tmp_path / "trip.log").read_text(encoding="utf-8")
    assert "Finished:" in text


def test_lines_are_flushed_immediately_not_only_on_close(tmp_path):
    # The whole point of incremental writing is that a reader can see
    # progress before close() ever runs - simulate that by reading the
    # file mid-way through, before close() is called.
    log = TripLog.open(tmp_path, trip_label="trip", command="bv-export")
    log.step("first step")

    text_before_close = (tmp_path / "trip.log").read_text(encoding="utf-8")
    assert "first step" in text_before_close

    log.close()


def test_a_trip_with_no_membership_or_steps_still_reads_cleanly(tmp_path):
    log = TripLog.open(tmp_path, trip_label="trip", command="bv-export")
    log.close()

    text = (tmp_path / "trip.log").read_text(encoding="utf-8")
    assert "--- Trip membership ---" not in text
    assert "--- Export steps ---" not in text
    assert "Finished:" in text
