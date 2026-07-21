from blackvue.generate.language_codes import short_code


def test_short_code_known_language():
    assert short_code("sv") == "swe"
    assert short_code("th") == "tha"
    assert short_code("en") == "eng"


def test_short_code_is_case_insensitive():
    assert short_code("SV") == "swe"


def test_short_code_falls_back_to_input_for_unknown_language():
    assert short_code("zz") == "zz"
