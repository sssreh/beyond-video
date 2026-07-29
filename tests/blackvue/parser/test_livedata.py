from blackvue.parser.livedata import parse_gps_fix


def test_parse_gps_fix_finds_a_plain_gps_object():
    text = '{"GPS":{"LATITUDE":59.334591, "LONGITUDE":18.063240}}'

    assert parse_gps_fix(text) == (59.334591, 18.063240)


def test_parse_gps_fix_ignores_a_3g_only_chunk():
    text = '{"3G":{"FrontRear":1, "LeftRight":2, "UpperLower":3}}'

    assert parse_gps_fix(text) is None


def test_parse_gps_fix_returns_none_for_a_gps_object_cut_off_mid_way():
    # A GPS object split across a chunk boundary looks, from a single
    # chunk's own point of view, like this - see
    # BlackVueClient.live_gps()'s docstring for how a caller recovers
    # by re-parsing a growing buffer instead.
    text = '{"GPS":{"LATITUDE":59.33'

    assert parse_gps_fix(text) is None


def test_parse_gps_fix_handles_negative_coordinates():
    text = '{"GPS":{"LATITUDE":-33.865143, "LONGITUDE":-70.5}}'

    assert parse_gps_fix(text) == (-33.865143, -70.5)


def test_parse_gps_fix_ignores_surrounding_multipart_framing():
    text = (
        "--ptaboundary\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 40\r\n"
        "\r\n"
        '{"GPS":{"LATITUDE":1.0, "LONGITUDE":2.0}}\r\n'
    )

    assert parse_gps_fix(text) == (1.0, 2.0)


def test_parse_gps_fix_finds_the_gps_object_after_a_3g_object():
    text = (
        '{"3G":{"FrontRear":1, "LeftRight":2, "UpperLower":3}}'
        '{"GPS":{"LATITUDE":10.0, "LONGITUDE":20.0}}'
    )

    assert parse_gps_fix(text) == (10.0, 20.0)
