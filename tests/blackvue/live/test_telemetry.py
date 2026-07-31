from blackvue.live.telemetry import GSensorSample
from blackvue.live.telemetry import GpsSample
from blackvue.live.telemetry import TelemetryState
from blackvue.live.telemetry import _drain_livedata_buffer


def test_telemetry_state_latest_gps_returns_the_most_recent_reading():
    state = TelemetryState()

    state.add_gps(1.0, 2.0)
    state.add_gps(3.0, 4.0)

    latest = state.latest_gps()
    assert (latest.latitude, latest.longitude) == (3.0, 4.0)


def test_telemetry_state_latest_gps_returns_none_when_empty():
    state = TelemetryState()

    assert state.latest_gps() is None


def test_telemetry_state_gps_history_returns_every_sample_within_the_window():
    state = TelemetryState()

    state.add_gps(1.0, 1.0)
    state.add_gps(2.0, 2.0)
    state.add_gps(3.0, 3.0)

    history = state.gps_history()
    assert [(s.latitude, s.longitude) for s in history] == [
        (1.0, 1.0), (2.0, 2.0), (3.0, 3.0),
    ]


def test_telemetry_state_trims_gps_samples_older_than_history_seconds():
    # A tiny history window (0.0 seconds) means _trim() drops
    # everything except whatever was just added - exercises the
    # trimming logic without needing to sleep in a test.
    state = TelemetryState(history_seconds=0.0)

    state.add_gps(1.0, 1.0)
    state.add_gps(2.0, 2.0)

    history = state.gps_history()
    assert len(history) == 1
    assert (history[-1].latitude, history[-1].longitude) == (2.0, 2.0)


def test_telemetry_state_gsensor_history_filters_by_seconds(monkeypatch):
    import blackvue.live.telemetry as telemetry_module

    fake_time = [0.0]
    monkeypatch.setattr(telemetry_module.time, "monotonic", lambda: fake_time[0])

    state = TelemetryState()
    state.add_gsensor(1.0, 2.0, 3.0)

    fake_time[0] = 100.0
    state.add_gsensor(4.0, 5.0, 6.0)

    recent = state.gsensor_history(seconds=10.0)
    assert len(recent) == 1
    assert (recent[0].front_rear, recent[0].left_right, recent[0].upper_lower) == (
        4.0, 5.0, 6.0,
    )

    everything = state.gsensor_history(seconds=1000.0)
    assert len(everything) == 2


def test_drain_livedata_buffer_consumes_a_complete_gps_object():
    state = TelemetryState()
    # find_next_gps()'s own pattern only matches the inner {"LATITUDE":
    # .., "LONGITUDE": ..} object, not the outer {"GPS": ...} wrapper -
    # so the wrapper's own closing brace is left in the remainder,
    # same as parser/test_livedata.py's find_next_gps() test.
    buffer = '{"GPS":{"LATITUDE":1.0, "LONGITUDE":2.0}}TRAILING'

    remainder = _drain_livedata_buffer(buffer, state)

    assert remainder == "}TRAILING"
    latest = state.latest_gps()
    assert (latest.latitude, latest.longitude) == (1.0, 2.0)


def test_drain_livedata_buffer_consumes_a_complete_gsensor_object():
    state = TelemetryState()
    buffer = '{"3G":{"FrontRear":1, "LeftRight":2, "UpperLower":3}}TRAILING'

    remainder = _drain_livedata_buffer(buffer, state)

    assert remainder == "}TRAILING"
    history = state.gsensor_history(seconds=1000.0)
    assert len(history) == 1
    assert (history[0].front_rear, history[0].left_right, history[0].upper_lower) == (
        1.0, 2.0, 3.0,
    )


def test_drain_livedata_buffer_consumes_multiple_objects_in_one_pass():
    state = TelemetryState()
    buffer = (
        '{"3G":{"FrontRear":1, "LeftRight":2, "UpperLower":3}}'
        '{"GPS":{"LATITUDE":10.0, "LONGITUDE":20.0}}'
        '{"GPS":{"LATITUDE":30.0, "LONGITUDE":40.0}}'
    )

    remainder = _drain_livedata_buffer(buffer, state)

    # Only the very last object's own outer closing brace survives -
    # every stray brace between objects gets swept up as noise ahead
    # of the next match during a later pass (truncating to
    # buffer[end:] discards anything before a match's own start too,
    # not just the match itself).
    assert remainder == "}"
    assert len(state.gps_history()) == 2
    assert len(state.gsensor_history(seconds=1000.0)) == 1
    latest = state.latest_gps()
    assert (latest.latitude, latest.longitude) == (30.0, 40.0)


def test_drain_livedata_buffer_leaves_an_incomplete_trailing_object_untouched():
    state = TelemetryState()
    buffer = '{"GPS":{"LATITUDE":1.0, "LONGITUDE":2.0}}{"GPS":{"LATITUDE":3.'

    remainder = _drain_livedata_buffer(buffer, state)

    assert remainder == '}{"GPS":{"LATITUDE":3.'
    assert len(state.gps_history()) == 1


def test_drain_livedata_buffer_drops_an_overlong_buffer_with_no_match(monkeypatch):
    import blackvue.live.telemetry as telemetry_module

    monkeypatch.setattr(telemetry_module, "MAX_BUFFER_CHARS", 10)
    state = TelemetryState()
    buffer = "x" * 20

    remainder = _drain_livedata_buffer(buffer, state)

    assert remainder == ""


def test_gsensor_max_deviation_starts_at_zero_with_no_readings():
    state = TelemetryState()

    assert state.gsensor_max_deviation() == 0.0


def test_gsensor_max_deviation_only_ever_grows():
    # Christer's own proposed fix: "when data comes in we put it at
    # maximum and then when newer data comes in and are greater than
    # the previous max value, we scale down the lines to match the new
    # max value" - i.e. the scale watermark must never shrink back
    # down, even once a later reading is much smaller than an earlier
    # peak. No calibration/baseline step anymore (removed at Christer's
    # own later request - see WORKING_CONTEXT.md) - deviation here is
    # just raw |reading|, measured from the very first sample.
    state = TelemetryState()

    state.add_gsensor(0.0, 0.0, 0.0)
    assert state.gsensor_max_deviation() == 0.0

    state.add_gsensor(5.0, 0.0, 0.0)
    assert state.gsensor_max_deviation() == 5.0

    # A much smaller reading afterwards must not shrink the watermark.
    state.add_gsensor(0.5, 0.0, 0.0)
    assert state.gsensor_max_deviation() == 5.0

    # A new, bigger peak on a *different* axis still grows it.
    state.add_gsensor(0.0, 0.0, 8.0)
    assert state.gsensor_max_deviation() == 8.0

    # Negative readings count by magnitude, same as positive ones.
    state.add_gsensor(-20.0, 0.0, 0.0)
    assert state.gsensor_max_deviation() == 20.0
