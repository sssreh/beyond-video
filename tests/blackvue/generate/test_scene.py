import shutil
from pathlib import Path

import pytest

from blackvue.generate import scene
from blackvue.generate.media import MediaToolError


def test_is_qwen3_vl_case_insensitive():
    assert scene.is_qwen3_vl("Qwen/Qwen3-VL-8B-Instruct") is True
    assert scene.is_qwen3_vl("QWEN/QWEN3-VL-8B-INSTRUCT") is True
    assert scene.is_qwen3_vl("Qwen/Qwen2.5-VL-7B-Instruct") is False


def test_patch_factor_differs_by_model_family():
    assert scene._patch_factor_for("Qwen/Qwen2.5-VL-7B-Instruct") == 28
    assert scene._patch_factor_for("Qwen/Qwen3-VL-8B-Instruct") == 32


def test_build_prompt_selects_by_task():
    assert scene.build_prompt("describe") == scene.DESCRIBE_PROMPT
    assert scene.build_prompt("ocr") == scene.OCR_PROMPT
    assert scene.build_prompt("both") == scene.COMBINED_PROMPT
    assert scene.build_prompt("anything-else") == scene.COMBINED_PROMPT


def test_extract_description_section_pulls_only_that_heading():
    text = (
        "## Description\n"
        "Routine driving, nothing notable happened.\n\n"
        "## On-screen text\n"
        "- 84 km/h\n"
    )

    result = scene.extract_description_section(text)

    assert result == "Routine driving, nothing notable happened."
    assert "km/h" not in result


def test_extract_description_section_missing_heading_returns_empty():
    assert scene.extract_description_section("no headings here") == ""


# ---------------------------------------------------------------------------
# extract_description_events() / DescriptionEvent - added once DESCRIBE_PROMPT
# started asking for a bulleted, per-event-timestamped description instead of
# one holistic paragraph. Christer, after getting the (then evenly-spaced)
# description.srt working: "It would have been nice to both say and subtitle
# 'To the left, there's a red bus passing alongside the vehicle' at the same
# time you can see the red buss pass" - answered by real per-event
# timestamps, the same way zoom_into_signs() already provides them for sign
# reads. Christer, same message: "please keep the old output" - see the
# backward-compatibility tests below for how extract_description_section()
# guarantees that for every existing caller.
# ---------------------------------------------------------------------------

_TIMED_DESCRIPTION_TEXT = (
    "## Description\n"
    "- [t=0.0s] Clear weather, light traffic on a two-lane suburban road.\n"
    "- [t=12.4s] A red bus passes on the left, driving in the opposite "
    "direction.\n"
    "- [t=25.0s] The vehicle continues straight through a quiet "
    "intersection.\n"
    "\n"
    "## On-screen text\n"
    "- 42 km/h\n"
)


# ---------------------------------------------------------------------------
# _truncate_repeated_lines() - task #1260 follow-up 7. Added after three
# consecutive real-hardware runs showed the "## On-screen text" section
# degenerating into an exact-repeat loop even after two rounds of
# generate()-parameter tuning (adaptive_repetition_penalty raised to 1.1 in
# follow-up 5, adaptive_no_repeat_ngram_size raised to 5 in follow-up 6) -
# each fix reduced repetition somewhere else but a different exact phrase
# still looped: "LAN" x40+, then "Forbjudet att kora pa gatan"/"Korselvag"
# alternating x25+, then "LANGA"/"FORSTA" alternating x45+. This is a
# structural safety net instead of a fourth parameter guess.
# ---------------------------------------------------------------------------


def test_truncate_repeated_lines_leaves_clean_text_untouched():
    text = (
        "## Description\n"
        "- [t=0.0s] The car passes a red bus on the left.\n"
        "- [t=12.4s] The car continues through a quiet intersection.\n"
        "\n"
        "## On-screen text\n"
        "- 42 km/h\n"
        "- STOP\n"
    )

    assert scene._truncate_repeated_lines(text) == text


def test_truncate_repeated_lines_cuts_off_single_word_loop():
    # Real-hardware case: "LAN" repeated 40+ times.
    text = "## On-screen text\n" + "LAN " * 45

    result = scene._truncate_repeated_lines(text)

    assert "LAN" not in result
    assert result == "## On-screen text"


def test_truncate_repeated_lines_cuts_off_alternating_phrase_loop():
    # Real-hardware case: two Swedish phrases alternating 25+ times each,
    # cut off mid-word by max_new_tokens.
    text = "## On-screen text\n" + "Forbjudet att kora pa gatan Korselvag " * 30 + "Forbjudet att kora p"

    result = scene._truncate_repeated_lines(text)

    assert "Korselvag" not in result
    assert result == "## On-screen text"


def test_truncate_repeated_lines_only_truncates_at_loop_start_not_whole_text():
    # A repeat loop appearing after otherwise-good content should only
    # drop the looping tail, not the good content before it.
    good = "## Description\n- [t=0.0s] Routine driving, nothing notable.\n\n"
    text = good + "## On-screen text\n" + "STOP " * 10

    result = scene._truncate_repeated_lines(text)

    assert result.startswith(good.rstrip("\n"))
    assert "STOP" not in result


def test_extract_description_events_parses_bulleted_timestamps():
    events = scene.extract_description_events(_TIMED_DESCRIPTION_TEXT)

    assert events == [
        scene.DescriptionEvent(
            0.0, "Clear weather, light traffic on a two-lane suburban road."
        ),
        scene.DescriptionEvent(
            12.4,
            "A red bus passes on the left, driving in the opposite direction.",
        ),
        scene.DescriptionEvent(
            25.0, "The vehicle continues straight through a quiet intersection."
        ),
    ]


def test_extract_description_events_returns_empty_for_old_plain_prose():
    # An older scene.txt (or a still photo's response) has no bullets at
    # all - extract_description_events() must return [] rather than
    # trying to force-parse a timestamp out of plain prose, so callers
    # can treat "no events" as "nothing to sync against here."
    old_text = "## Description\nRoutine driving, nothing notable happened.\n"
    assert scene.extract_description_events(old_text) == []


def test_extract_description_events_returns_empty_when_heading_missing():
    assert scene.extract_description_events("no headings here") == []


# 2026-08-26 (task #1260 follow-up 8): real hardware produced bullets like
# '- [t="35s"]', "- [t='55s']", up through '- [t="#155s"]" - the model
# progressively wrapped each successive bullet's timestamp in extra stray
# quote characters (and once a "#") as generation went on. The strict
# strip-whitespace-then-float parse in _parse_timestamp_token() silently
# dropped every one of those bullets from the timeline (not corrupted
# content, just an invisible gap in description.srt/TTS sync) since the
# token no longer ended in a plain digit or "s". Fixed by falling back to
# pulling the first number-shaped substring out of the token.
_QUOTE_DRIFT_DESCRIPTION_TEXT = (
    "## Description\n"
    '- [t=0s] A white truck with a blue container drives past on the left.\n'
    "- [t=19s] The camera approaches a roundabout.\n"
    '- [t="35s"] After navigating the roundabout, the vehicle enters a highway.\n'
    "- [t='55s'] Traffic flows steadily on the highway.\n"
    "- [t=\"'75s\"] The vehicle slows near a traffic light.\n"
    "- [t='\"95s\"] A red bus travels in the opposite lane.\n"
    '- [t=""115s"] The camera continues straight onto a wide highway.\n'
    '- [t="""135s"] A large billboard spans across the roadway.\n'
    '- [t="#155s"] The road curves slightly to the right.\n'
)


def test_extract_description_events_survives_escalating_quote_corruption():
    events = scene.extract_description_events(_QUOTE_DRIFT_DESCRIPTION_TEXT)

    assert [e.timestamp_seconds for e in events] == [
        0.0, 19.0, 35.0, 55.0, 75.0, 95.0, 115.0, 135.0, 155.0,
    ]
    assert events[5].text.startswith("A red bus travels")


def test_parse_timestamp_token_still_rejects_genuinely_non_numeric_text():
    assert scene._parse_timestamp_token("not a number") is None
    assert scene._parse_timestamp_token('"""') is None


def test_extract_description_events_folds_a_multi_line_bullet():
    # Same defensive multi-line-continuation handling
    # web/archive_browser.py's _parse_sign_reads() already needed (see
    # that function's own docstring for the real bug it fixed) - a long
    # event description wrapping across raw lines must not lose its
    # tail here either.
    text = (
        "## Description\n"
        "- [t=8.0s] A blue sedan overtakes on the left, then merges back\n"
        "into the lane ahead of the vehicle.\n"
    )

    events = scene.extract_description_events(text)

    assert events == [
        scene.DescriptionEvent(
            8.0,
            "A blue sedan overtakes on the left, then merges back into "
            "the lane ahead of the vehicle.",
        )
    ]


def test_extract_description_events_parses_a_single_line_crammed_real_world_response():
    # Christer, first real run against his own footage after
    # _BULLET_START_RE's original line-anchored version shipped: "Now
    # the description looks like [...] the voice is reading the
    # timestamps and the srt file is still evenly spread. The timing
    # seems to be when the model created the description, not the time
    # in the video" - pasted back a real Qwen3-VL response that (a)
    # crammed every bullet onto one single line with no newlines at
    # all, and (b) used wildly inconsistent whitespace inside the
    # brackets ("[ t=0s ]", "-[t= 0.6s]", "[t = 0 .9 s]") plus a
    # negative leading timestamp ("[t=-0.3s]"). None of that matched
    # the original regex, so every bullet silently failed to parse and
    # the raw bracket text got read aloud verbatim. This is that exact
    # text (trimmed to 4 bullets), asserting it parses correctly now.
    text = (
        "## Description\n"
        "- [t=-0.3s] The view is from inside a moving car, looking forward "
        "through the windshield at a multi-lane road under overcast skies. "
        "- [ t=0s ] Several cars are visible ahead and beside the viewer's "
        "vehicle. "
        "-[t= 0.6s]The perspective shifts slightly as the car continues "
        "straight. "
        "- [t = 0 .9 s]A large blue sign becomes visible ahead, mounted on "
        "a pole near the roadside.\n"
    )

    events = scene.extract_description_events(text)

    assert events == [
        scene.DescriptionEvent(
            -0.3,
            "The view is from inside a moving car, looking forward "
            "through the windshield at a multi-lane road under overcast "
            "skies.",
        ),
        scene.DescriptionEvent(
            0.0,
            "Several cars are visible ahead and beside the viewer's "
            "vehicle.",
        ),
        scene.DescriptionEvent(
            0.6,
            "The perspective shifts slightly as the car continues "
            "straight.",
        ),
        scene.DescriptionEvent(
            0.9,
            "A large blue sign becomes visible ahead, mounted on a pole "
            "near the roadside.",
        ),
    ]


def test_extract_description_events_skips_a_bullet_with_an_unparseable_timestamp():
    # A malformed bracket that still isn't a number once whitespace is
    # stripped (e.g. the model writing actual words in there) should be
    # dropped rather than crashing the whole parse - the surrounding,
    # well-formed bullets must still come through.
    text = (
        "## Description\n"
        "- [t=soon] Something happens near the start.\n"
        "- [t=5.0s] A cyclist crosses the road ahead.\n"
    )

    events = scene.extract_description_events(text)

    assert events == [
        scene.DescriptionEvent(5.0, "A cyclist crosses the road ahead."),
    ]


# 2026-08-26 (task #1260 follow-up 25): a real --frames 16 adaptive-sampling
# run showed corruption the quote-drift fix above doesn't cover - missing
# "="s ("[t 84. 5s ]"), wrong closing characters (")"/"J"/"j" instead of
# "]"), and letter-for-digit substitution ("l"/"I" for "1", e.g. "tll44. 5
# S" meant to render "t=144.5s"). Christer's real pasted .scene.txt had 10
# of 16 bullets - the entire back half of the clip - dropped outright by
# the old strict _BULLET_START_RE, plus one more silently mistimed from
# t=126.5s to t=26.5s by _parse_timestamp_token()'s digit-extraction
# fallback skipping the corrupted leading "l". This is a trimmed
# reproduction of that real output (6 originally-surviving bullets +
# 4 originally-dropped/mistimed ones), used below both to prove the
# widened _BULLET_START_RE now finds all 10 bullet boundaries, and to
# prove _realign_bullet_timestamps() recovers the correct timestamps by
# position rather than by (unreliable) digit parsing.
_BRACKET_CORRUPTION_DESCRIPTION_TEXT = (
    "## Description\n"
    "- [t=9.5s] The car is stopped at a traffic light at an intersection.\n"
    "- [t=30.5s] The car proceeds through the intersection.\n"
    "- [t 84. 5s ] Traffic flows past a roadside advertisement billboard.\n"
    "- [tl35. 5s) A white van and a black SUV pass on the right.\n"
    "- [tll44. 5 S] The car maintains steady forward motion under signage.\n"
    "- [t Il77. 5s j The vehicle continues past a large intersection.\n"
)
_BRACKET_CORRUPTION_KNOWN_TIMESTAMPS = [9.5, 30.5, 84.5, 135.5, 144.5, 177.5]


def test_bullet_start_re_finds_every_bullet_despite_bracket_corruption():
    matches = list(scene._BULLET_START_RE.finditer(_BRACKET_CORRUPTION_DESCRIPTION_TEXT))

    assert len(matches) == len(_BRACKET_CORRUPTION_KNOWN_TIMESTAMPS)


def test_realign_bullet_timestamps_recovers_real_values_by_position():
    realigned = scene._realign_bullet_timestamps(
        _BRACKET_CORRUPTION_DESCRIPTION_TEXT, _BRACKET_CORRUPTION_KNOWN_TIMESTAMPS
    )

    events = scene.extract_description_events(realigned)

    assert [e.timestamp_seconds for e in events] == _BRACKET_CORRUPTION_KNOWN_TIMESTAMPS
    # The unterminated last bullet's description text must survive too -
    # confirming the "j" close-character match didn't eat real content.
    assert events[-1].text.startswith("The vehicle continues past a large intersection")


def test_realign_bullet_timestamps_leaves_text_untouched_on_count_mismatch():
    # Reassigning the wrong number of bullets to a known-timestamps list of
    # a different length would just be a new way to get this wrong - safer
    # to leave the (still-corrupted) text for _parse_timed_events()'s own
    # tolerant-but-imperfect fallback parsing to handle instead.
    mismatched = _BRACKET_CORRUPTION_KNOWN_TIMESTAMPS[:-1]

    realigned = scene._realign_bullet_timestamps(_BRACKET_CORRUPTION_DESCRIPTION_TEXT, mismatched)

    assert realigned == _BRACKET_CORRUPTION_DESCRIPTION_TEXT


def test_realign_bullet_timestamps_no_op_on_already_clean_text():
    clean = (
        "## Description\n"
        "- [t=0.0s] Clear weather, light traffic.\n"
        "- [t=12.4s] A red bus passes on the left.\n"
    )

    realigned = scene._realign_bullet_timestamps(clean, [0.0, 12.4])

    events = scene.extract_description_events(realigned)
    assert [e.timestamp_seconds for e in events] == [0.0, 12.4]


# 2026-08-26 (task #1260 follow-up 26): Christer's next real run - this time
# the PLAIN (non-adaptive) path, no --adaptive-sampling - showed the same
# corruption shapes as follow-up 25 above, plus one new variant:
# "[t]='...168.3"]'"/"[t]=\"...180.3\"]" - a stray "]" landing immediately
# after "t", before the "=". Without tolerating that, the old widened
# _BULLET_START_RE still matched that stray "]" as the bullet's own close,
# capturing raw_seconds as empty and dropping the timestamp outright (worse
# than the quote-drift case, which at least left real digits to fall back
# on). This is a trimmed reproduction of the two affected bullets from that
# real output.
def test_bullet_start_re_tolerates_stray_close_bracket_right_after_t():
    text = (
        "## Description\n"
        "- [t]='\\\"\\\"168.3\"]' The final frame captures the car moving forward.\n"
        "- [t]=\"\\\\\"180.3\"] The journey concludes with continued travel.\n"
    )

    matches = list(scene._BULLET_START_RE.finditer(text))

    assert len(matches) == 2
    # Even without realignment, the digit-extraction fallback in
    # _parse_timestamp_token() should now recover real timestamps instead of
    # an empty/unparseable raw_seconds capture.
    events = scene.extract_description_events(text)
    assert [e.timestamp_seconds for e in events] == [168.3, 180.3]


def test_describe_scene_realigns_corrupted_plain_timestamps_by_position(monkeypatch, tmp_path):
    # Wiring-level test mirroring the adaptive-path one above: the plain
    # (non-adaptive) branch now has its own known-timestamps list too
    # (_plain_video_frame_timestamps(), task #1260 follow-up 10) - when the
    # model's bullet count happens to match it exactly (as it did in
    # Christer's real run - 16 bullets, one per given grounding value),
    # describe_scene() should realign by position here too, not just on the
    # adaptive-sampling path.
    import types

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    class _FakeInputs(dict):
        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return [
                "## Description\n"
                "- [t=0s] Bullet at the very start.\n"
                "- [t]='\\\"\\\"1.0\"]' Bullet one, corrupted.\n"
                "- [tl2. 0s) Bullet two, corrupted.\n"
                "- [t=3.0s] Bullet at the very end.\n"
            ]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)
    monkeypatch.setattr(scene, "probe_video", lambda path: types.SimpleNamespace(duration_seconds=3.0))

    # duration=3.0, fps=1.0, max_frames=4 -> _plain_video_frame_timestamps()
    # returns exactly [0.0, 1.0, 2.0, 3.0] (min_frames floor of 4), matching
    # the 4 bullets the fake model "generated" above one-for-one.
    output_text = scene.describe_scene(video_path, zoom_signs=False, fps=1.0, max_frames=4)
    events = scene.extract_description_events(output_text)

    assert [e.timestamp_seconds for e in events] == [0.0, 1.0, 2.0, 3.0]


# 2026-08-26 (task #1260 follow-up 27): Christer's next real run (same clip,
# after follow-up 25/26 shipped) confirmed the timestamp fixes worked - all
# 16 bullets now parse with clean, correctly-ordered "[t=X.Ys]" brackets, no
# drops, no mistiming. But 5 of those 16 bullets showed a NEW leftover:
# "- [t=48.1s]\" A large billboard..." - a stray quote character surviving
# immediately *after* the bullet's own "]" close, becoming the description
# text's own leading character once parsed ('" A large billboard...'). This
# is the tail end of the same quote-drift corruption (follow-up 8) that used
# to land *inside* the bracket - this time it landed just outside it. Left
# alone, this stray character would be visible on the recording detail page
# and read aloud verbatim by TTS. This is a trimmed reproduction of two of
# the five affected real bullets.
_STRAY_QUOTE_AFTER_BRACKET_TEXT = (
    "## Description\n"
    "- [t=48.1s]\" A large billboard advertising \"Fraschtlage mot...\" appears on the right.\n"
    "- [t=60.1s]' The vehicle slows at an intersection with a yellow traffic light.\n"
    "- [t=72.1s] The car proceeds along a tree-lined road, passing a red bus on the left.\n"
)


def test_extract_description_events_strips_stray_quote_immediately_after_bracket():
    events = scene.extract_description_events(_STRAY_QUOTE_AFTER_BRACKET_TEXT)

    assert [e.timestamp_seconds for e in events] == [48.1, 60.1, 72.1]
    assert events[0].text.startswith("A large billboard advertising")
    assert events[1].text.startswith("The vehicle slows")
    # The unaffected third bullet (no leading stray quote to begin with)
    # must be completely unchanged by this fix.
    assert events[2].text.startswith("The car proceeds along a tree-lined road")


def test_extract_description_events_keeps_a_real_leading_quoted_word():
    # A bullet that legitimately opens with a quoted word (e.g. quoting
    # signage text) must keep its quote - only a stray quote character that
    # stands completely alone as its own "word" gets dropped, distinguishing
    # real content from the corruption artifact above.
    text = '## Description\n- [t=0.0s] "BESIKTA" sign is visible on a shopfront.\n'

    events = scene.extract_description_events(text)

    assert events == [
        scene.DescriptionEvent(0.0, '"BESIKTA" sign is visible on a shopfront.'),
    ]


def test_describe_scene_realigns_corrupted_adaptive_timestamps_by_position(monkeypatch, tmp_path):
    # Wiring-level test: describe_scene() itself must apply the
    # realignment to the adaptive path's raw model output before zoom-
    # signs/sampled-frames get appended, using the real
    # sampled_frame_timestamps it already computed pre-generation.
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    class _FakeInputs(dict):
        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return [
                "## Description\n"
                "- [t=9.5s] The car is stopped at a traffic light.\n"
                "- [tl35. 5s) The vehicle continues past a large intersection.\n"
            ]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)
    monkeypatch.setattr(
        scene,
        "_build_adaptive_message_content",
        lambda *args, **kwargs: (
            [{"type": "text", "text": "intro"}, {"type": "video", "video": "x"}],
            [9.5, 135.5],
            [],
        ),
    )

    output_text = scene.describe_scene(video_path, zoom_signs=False, adaptive_sampling=True)
    events = scene.extract_description_events(output_text)

    assert [e.timestamp_seconds for e in events] == [9.5, 135.5]


# ---------------------------------------------------------------------------
# extract_sampled_frame_timestamps() - added for adaptive frame sampling
# (--adaptive-sampling). describe_scene()'s adaptive path appends a
# "## Sampled frames" section listing the real per-second timestamps it
# actually asked the vision-language model to look at, in the exact same
# "- [t=X.Ys] text" bullet shape the "## Description" section already
# uses - so this reuses _BULLET_START_RE/_parse_timed_events() via
# _extract_raw_section() rather than needing any new parsing logic. The
# frame-viewer (web/app.py's _frame_viewer_timestamps()) prefers these
# real timestamps over its own even-spacing guess whenever they're
# present.
# ---------------------------------------------------------------------------

_TEXT_WITH_SAMPLED_FRAMES = (
    "## Description\n"
    "- [t=0.0s] Clear weather, light traffic on a two-lane suburban road.\n"
    "- [t=12.4s] A red bus passes on the left, driving in the opposite "
    "direction.\n"
    "\n"
    "## On-screen text\n"
    "- 42 km/h\n"
    "\n"
    "## Sampled frames\n"
    "- [t=0.0s] sampled frame\n"
    "- [t=3.4s] sampled frame\n"
    "- [t=5.2s] sampled frame\n"
    "- [t=12.0s] sampled frame\n"
)


def test_extract_sampled_frame_timestamps_parses_the_section():
    timestamps = scene.extract_sampled_frame_timestamps(_TEXT_WITH_SAMPLED_FRAMES)

    assert timestamps == [0.0, 3.4, 5.2, 12.0]


def test_extract_sampled_frame_timestamps_ignores_the_description_section():
    # Regression guard for the shared-heading-boundary logic: a "## Sampled
    # frames" extraction must not accidentally also pick up the
    # "## Description" section's own (differently-timed) bullets.
    timestamps = scene.extract_sampled_frame_timestamps(_TEXT_WITH_SAMPLED_FRAMES)

    assert 12.4 not in timestamps


def test_extract_description_events_unaffected_by_sampled_frames_section():
    # And the reverse: extract_description_events() must still return only
    # the Description section's own events, not bleed into Sampled frames.
    events = scene.extract_description_events(_TEXT_WITH_SAMPLED_FRAMES)

    assert events == [
        scene.DescriptionEvent(
            0.0, "Clear weather, light traffic on a two-lane suburban road."
        ),
        scene.DescriptionEvent(
            12.4,
            "A red bus passes on the left, driving in the opposite direction.",
        ),
    ]


def test_extract_sampled_frame_timestamps_returns_empty_for_old_format():
    # A recording generated without --adaptive-sampling (or before this
    # feature existed) has no "## Sampled frames" section at all - callers
    # must treat this identically to "nothing real to sync against, fall
    # back to your own even-spacing approximation" (see
    # web/app.py's _frame_viewer_timestamps()).
    assert scene.extract_sampled_frame_timestamps(_TIMED_DESCRIPTION_TEXT) == []


def test_extract_sampled_frame_timestamps_returns_empty_when_heading_missing():
    assert scene.extract_sampled_frame_timestamps("no headings here") == []


def test_extract_description_section_strips_timestamps_into_clean_prose():
    # This is the "please keep the old output" guarantee: every caller
    # of extract_description_section() (trip-summary input, the
    # scene_summary display/TTS text) keeps getting a single readable
    # paragraph back, with no bracket notation visible, whether the
    # underlying scene.txt is old-format or new-format.
    result = scene.extract_description_section(_TIMED_DESCRIPTION_TEXT)

    assert result == (
        "Clear weather, light traffic on a two-lane suburban road. "
        "A red bus passes on the left, driving in the opposite direction. "
        "The vehicle continues straight through a quiet intersection."
    )
    assert "[t=" not in result


def test_extract_description_section_still_unchanged_for_old_plain_prose():
    old_text = (
        "## Description\n"
        "Routine driving, nothing notable happened.\n\n"
        "## On-screen text\n"
        "- 84 km/h\n"
    )

    result = scene.extract_description_section(old_text)

    assert result == "Routine driving, nothing notable happened."


def test_normalize_plate_text_ignores_case_and_whitespace():
    assert scene._normalize_plate_text("  etr 734  ") == "ETR 734"
    assert scene._normalize_plate_text("ETR734") == "ETR734"
    assert (
        scene._normalize_plate_text("etr  734")
        != scene._normalize_plate_text("etr734")
    )


def test_vision_gpu_available_false_when_torch_missing(monkeypatch):
    import builtins

    monkeypatch.setattr(scene, "_VISION_GPU_AVAILABLE", None)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("no torch here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert scene.vision_gpu_available() is False


# ---------------------------------------------------------------------------
# scene_gpu_vram_gb() / resolve_scene_quantize() - Christer: "yes, can you
# make it auto by looking at present graphics cards", right after learning
# quantization needs its own flag (a loading-precision choice, not a
# different model). "auto" should pick a level from the largest single GPU
# detected - see resolve_scene_quantize()'s own docstring for why largest-
# single rather than summed-total (his real dual-RTX-3080-Ti box, 12GB per
# card, was the concrete example discussed).
# ---------------------------------------------------------------------------


def test_scene_gpu_vram_gb_false_when_torch_missing(monkeypatch):
    import builtins

    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", None)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("no torch here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert scene.scene_gpu_vram_gb() == []


def test_scene_gpu_vram_gb_no_cuda_returns_empty(monkeypatch):
    import sys
    import types

    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", None)

    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert scene.scene_gpu_vram_gb() == []


def test_scene_gpu_vram_gb_sorts_multiple_gpus_largest_first(monkeypatch):
    import sys
    import types

    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", None)

    class _FakeProps:
        def __init__(self, gb):
            self.total_memory = gb * (1024**3)

    fake_cuda = types.SimpleNamespace(
        is_available=lambda: True,
        device_count=lambda: 2,
        get_device_properties=lambda i: _FakeProps([8.0, 24.0][i]),
    )
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = fake_cuda
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert scene.scene_gpu_vram_gb() == [24.0, 8.0]


def test_scene_gpu_vram_gb_is_cached(monkeypatch):
    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [12.0])

    # No torch import happens at all once cached - if it tried, and
    # torch weren't on sys.path in a stripped test env, this would
    # blow up instead of returning the cached value.
    assert scene.scene_gpu_vram_gb() == [12.0]


def test_resolve_scene_quantize_explicit_values_pass_through():
    assert scene.resolve_scene_quantize("none", force_cpu=False) == "none"
    assert scene.resolve_scene_quantize("int8", force_cpu=False) == "int8"
    assert scene.resolve_scene_quantize("int4", force_cpu=False) == "int4"


def test_resolve_scene_quantize_rejects_unknown_value():
    with pytest.raises(ValueError):
        scene.resolve_scene_quantize("bogus", force_cpu=False)


def test_resolve_scene_quantize_force_cpu_always_resolves_auto_to_none(monkeypatch):
    # Even with a big GPU detected, force_cpu wins - bitsandbytes'
    # int8/int4 paths are CUDA-only, so quantizing on the way to a CPU
    # load would buy nothing.
    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [24.0])

    assert scene.resolve_scene_quantize("auto", force_cpu=True) == "none"


def test_resolve_scene_quantize_auto_no_gpu_resolves_to_none(monkeypatch):
    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [])

    assert scene.resolve_scene_quantize("auto", force_cpu=False) == "none"


def test_resolve_scene_quantize_auto_large_gpu_resolves_to_none(monkeypatch):
    # e.g. Christer's RTX 5090 laptop.
    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [24.0])

    assert scene.resolve_scene_quantize("auto", force_cpu=False) == "none"


def test_resolve_scene_quantize_auto_mid_gpu_resolves_to_int8(monkeypatch):
    # e.g. a single RTX 4080, 16GB.
    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [16.0])

    assert scene.resolve_scene_quantize("auto", force_cpu=False) == "int8"


def test_resolve_scene_quantize_auto_keyed_on_largest_single_gpu_not_sum(monkeypatch):
    # Two 16GB GPUs must still resolve to "int8", not "none" from a
    # summed 32GB - device_map="auto" sharding across both is not the
    # point; quantizing down onto *one* card is (see
    # resolve_scene_quantize()'s own docstring).
    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [16.0, 16.0])

    assert scene.resolve_scene_quantize("auto", force_cpu=False) == "int8"


def test_resolve_scene_quantize_auto_small_gpu_resolves_to_int4(monkeypatch):
    # e.g. Christer's real dual-RTX-3080-Ti box, 12GB/card - measured
    # at almost 14GB actual int8 usage, too tight for a 12GB card, so
    # this now falls through to int4 instead (see INT8_MIN_GB's own
    # comment for the real-world measurement behind this).
    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [12.0])

    assert scene.resolve_scene_quantize("auto", force_cpu=False) == "int4"


def test_resolve_scene_quantize_auto_boundaries(monkeypatch):
    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [20.0])
    assert scene.resolve_scene_quantize("auto", force_cpu=False) == "none"

    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [19.9])
    assert scene.resolve_scene_quantize("auto", force_cpu=False) == "int8"

    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [16.0])
    assert scene.resolve_scene_quantize("auto", force_cpu=False) == "int8"

    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [15.9])
    assert scene.resolve_scene_quantize("auto", force_cpu=False) == "int4"


def test_get_scene_model_cache_key_includes_resolved_quantize(monkeypatch):
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", {})
    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [12.0])  # auto -> int8

    load_calls = []

    def fake_load(model_name, *, force_cpu, quantize="none"):
        load_calls.append((model_name, force_cpu, quantize))
        return _fake_loaded_model()

    monkeypatch.setattr(scene, "_load_scene_model", fake_load)

    scene._get_scene_model("some-model", force_cpu=False, quantize="auto")

    assert load_calls == [("some-model", False, "int8")]
    assert "some-model:auto:int8" in scene._SCENE_MODEL_CACHE

    # A second call with the same (model, force_cpu, "auto") reuses the
    # cache - no second load.
    scene._get_scene_model("some-model", force_cpu=False, quantize="auto")
    assert len(load_calls) == 1

    # An explicit quantize level for the same model is a distinct
    # cache entry.
    scene._get_scene_model("some-model", force_cpu=False, quantize="int4")
    assert len(load_calls) == 2
    assert "some-model:auto:int4" in scene._SCENE_MODEL_CACHE


def test_get_scene_model_cache_key_includes_gpu_memory_fraction(monkeypatch):
    # gpu_memory_fraction is a process-wide CUDA setting only actually
    # applied inside _load_scene_model() at load time (see that
    # function) - so a second call asking for a different cap must not
    # hit a stale cache entry that never got the new cap applied.
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", {})
    monkeypatch.setattr(scene, "_SCENE_GPU_VRAM_GB", [12.0])  # auto -> int8

    load_calls = []

    def fake_load(model_name, *, force_cpu, quantize="none", gpu_memory_fraction=None):
        load_calls.append((model_name, force_cpu, quantize, gpu_memory_fraction))
        return _fake_loaded_model()

    monkeypatch.setattr(scene, "_load_scene_model", fake_load)

    scene._get_scene_model("some-model", force_cpu=False, quantize="auto")

    assert load_calls == [("some-model", False, "int8", None)]
    assert "some-model:auto:int8:None" in scene._SCENE_MODEL_CACHE

    # A second call with the same (model, force_cpu, "auto", None)
    # reuses the cache - no second load.
    scene._get_scene_model("some-model", force_cpu=False, quantize="auto")
    assert len(load_calls) == 1

    # An explicit gpu_memory_fraction for the same model is a distinct
    # cache entry.
    scene._get_scene_model(
        "some-model", force_cpu=False, quantize="auto", gpu_memory_fraction=0.5
    )
    assert len(load_calls) == 2
    assert "some-model:auto:int8:0.5" in scene._SCENE_MODEL_CACHE


def test_load_scene_model_gpu_memory_fraction_and_force_cpu_together_raises(monkeypatch):
    # Same contradiction-with-force_cpu reasoning as quantize's own
    # test above - checked before the torch/transformers imports.
    import sys
    import types

    for name in ("torch", "torchvision", "qwen_vl_utils"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    fake_transformers = types.ModuleType("transformers")
    fake_transformers.AutoProcessor = object
    fake_transformers.Qwen2_5_VLForConditionalGeneration = object
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    with pytest.raises(MediaToolError, match="can't be combined"):
        scene._load_scene_model(
            "some-model", force_cpu=True, quantize="none", gpu_memory_fraction=0.5
        )


def test_load_scene_model_gpu_memory_fraction_out_of_range_raises(monkeypatch):
    import sys
    import types

    for name in ("torch", "torchvision", "qwen_vl_utils"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    fake_transformers = types.ModuleType("transformers")
    fake_transformers.AutoProcessor = object
    fake_transformers.Qwen2_5_VLForConditionalGeneration = object
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    for bad_value in (0.0, -0.1, 1.5):
        with pytest.raises(MediaToolError, match="greater than 0 and at most 1.0"):
            scene._load_scene_model(
                "some-model",
                force_cpu=False,
                quantize="none",
                gpu_memory_fraction=bad_value,
            )


def test_load_scene_model_quantize_and_force_cpu_together_raises(monkeypatch):
    # resolve_scene_quantize() already resolves "auto" to "none" under
    # force_cpu (see its own test above) - reaching _load_scene_model()
    # with quantize != "none" and force_cpu=True can only mean an
    # explicit, contradictory SceneOptions(quantize=..., force_cpu=True).
    #
    # Stub torch/torchvision/qwen_vl_utils/transformers as importable
    # (empty modules) so this test exercises the quantize/force_cpu
    # guard itself, not a missing-dependency error from the imports
    # that happen earlier in _load_scene_model() - this environment
    # doesn't have the real scene extra installed.
    import sys
    import types

    for name in ("torch", "torchvision", "qwen_vl_utils"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    fake_transformers = types.ModuleType("transformers")
    fake_transformers.AutoProcessor = object
    fake_transformers.Qwen2_5_VLForConditionalGeneration = object
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    with pytest.raises(MediaToolError, match="can't be combined"):
        scene._load_scene_model("some-model", force_cpu=True, quantize="int8")


def _stub_scene_model_dependencies(monkeypatch, *, captured_kwargs):
    # Full happy-path stub of torch/torchvision/qwen_vl_utils/transformers
    # so _load_scene_model() can run all the way through to
    # ModelClass.from_pretrained() - captured_kwargs.append(...) records
    # what it was actually called with, for asserting on torch_dtype
    # below. Mirrors the stubbing pattern in the guard-clause tests
    # above, just taken one step further.
    import sys
    import types

    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(
        is_available=lambda: True,
        device_count=lambda: 1,
        set_per_process_memory_fraction=lambda *a, **k: None,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "torchvision", types.ModuleType("torchvision"))

    fake_qwen_vl_utils = types.ModuleType("qwen_vl_utils")
    fake_qwen_vl_utils.process_vision_info = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "qwen_vl_utils", fake_qwen_vl_utils)

    class _FakeModel:
        @classmethod
        def from_pretrained(cls, model_name, **kwargs):
            captured_kwargs.append(kwargs)
            return object()

    class _FakeAutoProcessor:
        @classmethod
        def from_pretrained(cls, model_name):
            return object()

    class _FakeBitsAndBytesConfig:
        def __init__(self, *, load_in_8bit=False, load_in_4bit=False):
            self.load_in_8bit = load_in_8bit
            self.load_in_4bit = load_in_4bit

    fake_transformers = types.ModuleType("transformers")
    fake_transformers.AutoProcessor = _FakeAutoProcessor
    fake_transformers.Qwen2_5_VLForConditionalGeneration = _FakeModel
    fake_transformers.BitsAndBytesConfig = _FakeBitsAndBytesConfig
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)


def test_load_scene_model_int8_quantize_forces_float16_to_avoid_matmul_cast_warning(
    monkeypatch,
):
    # bitsandbytes' int8 path (LLM.int8()/MatMul8bitLt) only ever
    # computes in float16 - loading a bfloat16-native model (Qwen3-VL)
    # with torch_dtype="auto" makes it silently cast on every single
    # matmul and print "MatMul8bitLt: inputs will be cast from
    # torch.bfloat16 to float16 during quantization" every time, which
    # floods stderr across a whole video's worth of frames. Christer
    # hit this running --scene-quantize int8 for real. Loading in
    # float16 up front sidesteps the cast (and its warning) entirely.
    captured_kwargs = []
    _stub_scene_model_dependencies(monkeypatch, captured_kwargs=captured_kwargs)

    scene._load_scene_model("some-model", force_cpu=False, quantize="int8")

    assert captured_kwargs[0]["torch_dtype"] == "float16"


def test_load_scene_model_int4_quantize_leaves_torch_dtype_auto(monkeypatch):
    # int4 (NF4) doesn't have the same forced-float16-compute issue as
    # int8 - its bnb_4bit_compute_dtype is independent of the load
    # dtype, so there's no reason to override "auto" here.
    captured_kwargs = []
    _stub_scene_model_dependencies(monkeypatch, captured_kwargs=captured_kwargs)

    scene._load_scene_model("some-model", force_cpu=False, quantize="int4")

    assert captured_kwargs[0]["torch_dtype"] == "auto"


def test_load_scene_model_no_quantize_leaves_torch_dtype_auto(monkeypatch):
    captured_kwargs = []
    _stub_scene_model_dependencies(monkeypatch, captured_kwargs=captured_kwargs)

    scene._load_scene_model("some-model", force_cpu=False, quantize="none")

    assert captured_kwargs[0]["torch_dtype"] == "auto"


def test_parse_grounding_boxes_plain_json():
    raw = '[{"bbox_2d": [1, 2, 3, 4], "label": "license plate"}]'

    boxes = scene._parse_grounding_boxes(raw)

    assert boxes == [{"label": "license plate", "box": (1.0, 2.0, 3.0, 4.0)}]


def test_parse_grounding_boxes_strips_markdown_fence():
    raw = '```json\n[{"bbox_2d": [1, 2, 3, 4], "label": "sign"}]\n```'

    boxes = scene._parse_grounding_boxes(raw)

    assert boxes == [{"label": "sign", "box": (1.0, 2.0, 3.0, 4.0)}]


def test_parse_grounding_boxes_recovers_truncated_json():
    # Simulates hitting --zoom-detect-max-new-tokens mid-list: the
    # first object is complete, the second is cut off.
    raw = (
        '[{"bbox_2d": [1, 2, 3, 4], "label": "sign"}, '
        '{"bbox_2d": [5, 6, 7'
    )

    boxes = scene._parse_grounding_boxes(raw)

    assert boxes == [{"label": "sign", "box": (1.0, 2.0, 3.0, 4.0)}]


def test_parse_grounding_boxes_empty_list_is_fine():
    assert scene._parse_grounding_boxes("[]") == []


def test_parse_grounding_boxes_unparseable_returns_empty():
    assert scene._parse_grounding_boxes("not json at all") == []


# ---------------------------------------------------------------------------
# _parse_batch_reads() (task #1244) - the tolerant-parsing contract
# _run_batch_image_prompt() relies on to always hand back exactly as
# many strings as crops it was asked to read, regardless of how well
# the model followed the "JSON list of N strings" instruction.
# ---------------------------------------------------------------------------


def test_parse_batch_reads_clean_json_list():
    raw = '["ETR734", "not legible", "BESIKTA"]'
    assert scene._parse_batch_reads(raw, 3) == ["ETR734", "not legible", "BESIKTA"]


def test_parse_batch_reads_tolerates_markdown_fence():
    raw = '```json\n["ABC123"]\n```'
    assert scene._parse_batch_reads(raw, 1) == ["ABC123"]


def test_parse_batch_reads_pads_short_response():
    # Model only returned 2 answers for 3 requested crops - the third
    # comes back as a visibly-a-failure placeholder rather than
    # silently reusing/duplicating one of the other two reads.
    raw = '["ETR734", "BESIKTA"]'
    result = scene._parse_batch_reads(raw, 3)
    assert result[:2] == ["ETR734", "BESIKTA"]
    assert "unread" in result[2]


def test_parse_batch_reads_truncates_long_response():
    raw = '["ETR734", "BESIKTA", "SHURGARD", "extra"]'
    assert scene._parse_batch_reads(raw, 3) == ["ETR734", "BESIKTA", "SHURGARD"]


def test_parse_batch_reads_unparseable_marks_every_entry_unread():
    result = scene._parse_batch_reads("not a json list at all", 2)
    assert len(result) == 2
    assert all("unread" in r for r in result)


def test_scene_options_defaults_match_tuned_values():
    opts = scene.SceneOptions()

    assert opts.task == "both"
    assert opts.model == scene.DEFAULT_MODEL
    assert opts.fps == 1.0
    # 16 -> 64 -> 32 -> 16 (2026-08-19): Christer initially wanted ~3s
    # between sampled frames instead of ~11s, for closer description-
    # event timing. 64 briefly shipped the same day but pushed per-frame
    # resolution below qwen_vl_utils' own VIDEO_MAX_PIXELS ceiling and
    # measurably hurt description quality ("less informative, fewer
    # cues"). 32 (via a total_pixels-budgeting scheme) fixed that, but
    # Christer then asked to just go back to 16 outright rather than
    # keep the added budgeting complexity ("I want to go back to 16").
    # See SceneOptions' own docstring, and describe_scene()'s video
    # branch, for the full explanation.
    assert opts.max_frames == 16
    assert opts.max_pixels == 360 * 420
    assert opts.resized_width == 1092
    assert opts.resized_height == 588
    assert not hasattr(opts, "video_total_pixels")
    assert opts.zoom_signs is True
    assert opts.zoom_plate_confidence_check is True
    assert opts.force_cpu is False
    assert opts.quantize == "auto"
    assert opts.gpu_memory_fraction is None


def test_describe_scene_rejects_opts_and_overrides_together():
    with pytest.raises(TypeError):
        scene.describe_scene(
            Path("video.mp4"), opts=scene.SceneOptions(), task="ocr"
        )


class _FakeLoadedModel:
    """Stand-in for scene._LoadedSceneModel that doesn't need torch/
    transformers - only the fields _zoom_into_signs actually reads."""

    def __init__(self):
        self.model = object()
        self.processor = object()
        self.process_vision_info = object()
        self.patch_factor = 28
        self.is_qwen3 = False


def test_zoom_into_signs_flags_disagreeing_plate_reads(monkeypatch, tmp_path):
    """The core regression test for the real ETR734-vs-FTR78P finding:
    a plate crop that reads differently on the two confidence-check
    passes must be reported as unverified, not silently picked
    between."""

    from PIL import Image

    frame = Image.new("RGB", (200, 100), color="white")
    monkeypatch.setattr(
        scene, "_extract_full_res_frames", lambda video_path, count: [(1.0, frame)]
    )

    # Detection (GROUND_PROMPT) still goes through
    # _run_single_image_prompt() - only the crop-reading step below
    # batches (task #1244), so this fake only ever sees the detection
    # call now.
    def fake_run_single_image_prompt(image, prompt, loaded, opts, **kwargs):
        assert prompt is scene.GROUND_PROMPT
        return '[{"bbox_2d": [10, 10, 60, 40], "label": "vehicle license plate"}]'

    # First OCR batch call (greedy): "ETR734". Second (force_sample,
    # the confidence check): a different read, "FTR78P" - the two
    # should disagree.
    def fake_run_batch_image_prompt(images, prompt_template, loaded, opts, **kwargs):
        if kwargs.get("force_sample"):
            return ["FTR78P"]
        return ["ETR734"]

    monkeypatch.setattr(scene, "_run_single_image_prompt", fake_run_single_image_prompt)
    monkeypatch.setattr(scene, "_run_batch_image_prompt", fake_run_batch_image_prompt)

    loaded = _FakeLoadedModel()
    opts = scene.SceneOptions(zoom_plate_confidence_check=True)

    result = scene._zoom_into_signs(Path("fake.mp4"), loaded, opts)

    assert "unverified" in result
    assert "ETR734" in result
    assert "FTR78P" in result


def test_zoom_into_signs_no_note_when_plate_reads_agree(monkeypatch):
    from PIL import Image

    frame = Image.new("RGB", (200, 100), color="white")
    monkeypatch.setattr(
        scene, "_extract_full_res_frames", lambda video_path, count: [(1.0, frame)]
    )

    def fake_run_single_image_prompt(image, prompt, loaded, opts, **kwargs):
        assert prompt is scene.GROUND_PROMPT
        return '[{"bbox_2d": [10, 10, 60, 40], "label": "vehicle license plate"}]'

    def fake_run_batch_image_prompt(images, prompt_template, loaded, opts, **kwargs):
        return ["ABC123"]

    monkeypatch.setattr(scene, "_run_single_image_prompt", fake_run_single_image_prompt)
    monkeypatch.setattr(scene, "_run_batch_image_prompt", fake_run_batch_image_prompt)

    loaded = _FakeLoadedModel()
    opts = scene.SceneOptions(zoom_plate_confidence_check=True)

    result = scene._zoom_into_signs(Path("fake.mp4"), loaded, opts)

    assert "unverified" not in result
    assert "ABC123" in result


def test_zoom_into_signs_skips_confidence_check_for_non_plate_signs(monkeypatch):
    from PIL import Image

    frame = Image.new("RGB", (200, 100), color="white")
    monkeypatch.setattr(
        scene, "_extract_full_res_frames", lambda video_path, count: [(1.0, frame)]
    )

    calls = []

    def fake_run_single_image_prompt(image, prompt, loaded, opts, **kwargs):
        assert prompt is scene.GROUND_PROMPT
        calls.append(("single", kwargs.get("force_sample", False)))
        return '[{"bbox_2d": [10, 10, 60, 40], "label": "shop sign"}]'

    def fake_run_batch_image_prompt(images, prompt_template, loaded, opts, **kwargs):
        calls.append(("batch", kwargs.get("force_sample", False)))
        return ["OPEN 24 HOURS"]

    monkeypatch.setattr(scene, "_run_single_image_prompt", fake_run_single_image_prompt)
    monkeypatch.setattr(scene, "_run_batch_image_prompt", fake_run_batch_image_prompt)

    loaded = _FakeLoadedModel()
    opts = scene.SceneOptions(zoom_plate_confidence_check=True)

    result = scene._zoom_into_signs(Path("fake.mp4"), loaded, opts)

    # Exactly one grounding call (single-image) + one batch OCR call -
    # no second (force_sample) confidence-check batch call for a
    # non-plate label.
    assert len(calls) == 2
    assert not any(force_sample for _kind, force_sample in calls)
    assert "unverified" not in result


def test_describe_scene_output_includes_disclaimer(monkeypatch, tmp_path):
    """describe_scene()'s output must always carry the disclaimer
    footer - the module-level mitigation for hallucinated place names
    (the real "Palm Jumeirah" finding) that couldn't be fixed by
    prompting alone."""

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    class _FakeInputs(dict):
        """Dict-like so **inputs works in describe_scene()'s
        model.generate(**inputs, ...) call, the same way a real HF
        BatchFeature supports mapping unpacking."""

        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return ["## Description\nRoutine driving, nothing notable happened."]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none": loaded)

    result = scene.describe_scene(video_path, zoom_signs=False)

    assert result.startswith("## Description")
    assert scene.DISCLAIMER in result


# ---------------------------------------------------------------------------
# Task #1245 follow-up 5, extended by task #1258 follow-up: relaxed
# repetition/ngram settings for the main describe call. Real hardware output
# at --adaptive-context-frames 2 (~80 sampled frames after context expansion)
# showed the "[t=" bullet-opening token progressively mutating through
# unrelated characters as generation went on - t, T, F, f, r, R, E, e, -, L,
# l, I, i, o, O, Q, q, w, W, X, x - the no_repeat_ngram_size=3 exact-3-gram
# ban forcing ever more desperate token substitutions once ~80 near-identical
# bullet openings had already appeared. Christer chose "loosen ngram/
# repetition settings for this call" over capping frame count or reverting
# context frames to 0.
#
# Originally scoped to opts.adaptive_sampling only - real hardware at
# --frames 32 on the plain non-adaptive path later showed the same drift on
# just 5 bullets ("- [ t=0s ]", "-[t=2.8s]", "-[-t=6.9s]"), so the relaxation
# now applies unconditionally to the main describe call regardless of
# adaptive_sampling. See SceneOptions.adaptive_repetition_penalty/
# adaptive_no_repeat_ngram_size - mirrors the zoom_repetition_penalty=1.0/
# zoom_no_repeat_ngram_size=0 precedent already running safely for the same
# "many short structured outputs in one completion" shape of problem.
# ---------------------------------------------------------------------------


def test_describe_scene_uses_relaxed_repetition_settings_when_adaptive_sampling_is_on(
    monkeypatch, tmp_path
):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    captured_kwargs = {}

    class _FakeInputs(dict):
        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            captured_kwargs.update(kwargs)
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return ["## Description\n- [t=70.0s] A red bus passes on the left."]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)
    monkeypatch.setattr(
        scene,
        "_build_adaptive_message_content",
        lambda *args, **kwargs: (
            [{"type": "text", "text": "intro"}, {"type": "video", "video": "x"}],
            [70.0],
            [],
        ),
    )

    scene.describe_scene(video_path, zoom_signs=False, adaptive_sampling=True)

    assert captured_kwargs["repetition_penalty"] == scene.SceneOptions().adaptive_repetition_penalty
    assert captured_kwargs["no_repeat_ngram_size"] == scene.SceneOptions().adaptive_no_repeat_ngram_size


def test_describe_scene_uses_relaxed_repetition_settings_when_adaptive_sampling_is_off(
    monkeypatch, tmp_path
):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    captured_kwargs = {}

    class _FakeInputs(dict):
        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            captured_kwargs.update(kwargs)
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return ["## Description\nRoutine driving, nothing notable happened."]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)

    scene.describe_scene(video_path, zoom_signs=False)

    # Task #1258 follow-up: real hardware showed the same "[t=" bracket-
    # formatting drift on the plain non-adaptive path at --frames 32 that
    # originally motivated the adaptive-only relaxation - now applied
    # unconditionally, so this path uses the relaxed values too, not the
    # tuned repetition_penalty=1.15/no_repeat_ngram_size=3 defaults
    # (those remain in use by summarize_trip()'s own separate call).
    assert captured_kwargs["repetition_penalty"] == scene.SceneOptions().adaptive_repetition_penalty
    assert captured_kwargs["no_repeat_ngram_size"] == scene.SceneOptions().adaptive_no_repeat_ngram_size


# ---------------------------------------------------------------------------
# Task #1245 follow-up 6: scale max_new_tokens with sampled-frame count for
# adaptive sampling. Real hardware output at --adaptive-context-frames 2
# (~80 sampled frames), after the follow-up-5 repetition-settings fix landed,
# came out uncorrupted but truncated mid-sentence after ~20 bullets -
# max_new_tokens=768 was only ever tuned against the 16-frame baseline (768 /
# 16 = 48 tokens/bullet) and never scaled when adaptive_context_frames
# multiplied the sampled-frame count. See
# SceneOptions.adaptive_max_new_tokens_per_frame.
# ---------------------------------------------------------------------------


def test_describe_scene_scales_max_new_tokens_with_sampled_frame_count(monkeypatch, tmp_path):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    captured_kwargs = {}

    class _FakeInputs(dict):
        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            captured_kwargs.update(kwargs)
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return ["## Description\n- [t=70.0s] A red bus passes on the left."]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    # 80 sampled frames - matches Christer's real --adaptive-context-frames 2
    # run (16 highlights x 5 frames each after context expansion).
    fake_timestamps = [float(i) for i in range(80)]

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)
    monkeypatch.setattr(
        scene,
        "_build_adaptive_message_content",
        lambda *args, **kwargs: (
            [{"type": "text", "text": "intro"}, {"type": "video", "video": "x"}],
            fake_timestamps,
            [],
        ),
    )

    scene.describe_scene(video_path, zoom_signs=False, adaptive_sampling=True)

    expected = len(fake_timestamps) * scene.SceneOptions().adaptive_max_new_tokens_per_frame
    assert expected > scene.SceneOptions().max_new_tokens  # sanity: the scaling actually matters here
    assert captured_kwargs["max_new_tokens"] == expected


def test_describe_scene_keeps_default_max_new_tokens_for_small_adaptive_frame_counts(monkeypatch, tmp_path):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    captured_kwargs = {}

    class _FakeInputs(dict):
        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            captured_kwargs.update(kwargs)
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return ["## Description\n- [t=70.0s] A red bus passes on the left."]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    # A single sampled frame (e.g. adaptive sampling with no context frames
    # and only one highlight) - len(timestamps) * 64 is well under 768, so
    # the fixed default should win via max(), not the scaled-down value.
    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)
    monkeypatch.setattr(
        scene,
        "_build_adaptive_message_content",
        lambda *args, **kwargs: (
            [{"type": "text", "text": "intro"}, {"type": "video", "video": "x"}],
            [70.0],
            [],
        ),
    )

    scene.describe_scene(video_path, zoom_signs=False, adaptive_sampling=True)

    assert captured_kwargs["max_new_tokens"] == scene.SceneOptions().max_new_tokens


def test_describe_scene_keeps_default_max_new_tokens_when_adaptive_sampling_is_off(monkeypatch, tmp_path):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    captured_kwargs = {}

    class _FakeInputs(dict):
        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            captured_kwargs.update(kwargs)
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return ["## Description\nRoutine driving, nothing notable happened."]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)

    scene.describe_scene(video_path, zoom_signs=False)

    assert captured_kwargs["max_new_tokens"] == scene.SceneOptions().max_new_tokens


# ---------------------------------------------------------------------------
# describe_scene()'s adaptive-sampling fallback detection (task #1238) - a
# real-archive bug report: "20220927_132155_E"'s adaptive-sampling retry
# produced a short, generic 4-bullet description bunched near t=0s, with
# none of that recording's known real content (a red bus, BESIKTA/BRH547
# signage), while the separate zoom-signs pass read every sign correctly
# across the whole clip. Root cause: _build_adaptive_message_content()
# always seeds its `content` list with one intro-text element before it
# ever adds real frame images (see that function), so `content` is truthy
# even when frame extraction returns zero images (e.g. a transient
# decord/network-read hiccup) - describe_scene() used to check
# `if adaptive_content:` to decide whether to use it, which let a
# text-only, image-less message through instead of falling back to the
# dependable plain "video" element. The vision model, shown no pictures at
# all, produced a plausible-sounding but entirely ungrounded description.
# Fixed by checking `sampled_frame_timestamps` instead (empty exactly when
# no real frames were extracted).
# ---------------------------------------------------------------------------


def test_describe_scene_falls_back_to_plain_video_when_adaptive_frames_empty(
    monkeypatch, tmp_path
):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    captured_messages = {}

    class _FakeInputs(dict):
        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            captured_messages["messages"] = messages
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return ["## Description\nRoutine driving, nothing notable happened."]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)

    # Reproduces the exact bug shape: a non-empty content list (the intro
    # text element _build_adaptive_message_content() always seeds first)
    # but zero real timestamps, because frame extraction itself found
    # nothing usable.
    monkeypatch.setattr(
        scene,
        "_build_adaptive_message_content",
        lambda *args, **kwargs: (
            [{"type": "text", "text": scene.ADAPTIVE_FRAME_INTRO_PROMPT}],
            [],
            [],
        ),
    )

    result = scene.describe_scene(video_path, zoom_signs=False, adaptive_sampling=True)

    assert result.startswith("## Description")
    message_content = captured_messages["messages"][0]["content"]
    types = [element.get("type") for element in message_content]
    # Fell back to the plain video element, not the empty adaptive one.
    assert "video" in types
    assert scene.ADAPTIVE_FRAME_INTRO_PROMPT not in [
        element.get("text") for element in message_content
    ]


def test_describe_scene_uses_adaptive_frames_when_extraction_succeeds(monkeypatch, tmp_path):
    """Sanity-check the other side of the same branch: when adaptive
    sampling genuinely does extract real frames, describe_scene() must
    still use them (not the plain video element) - this test would have
    failed if the fix had instead been to always fall back."""

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    captured_messages = {}

    class _FakeInputs(dict):
        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            captured_messages["messages"] = messages
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return ["## Description\n- [t=70.0s] A red bus passes on the left."]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)
    monkeypatch.setattr(
        scene,
        "_build_adaptive_message_content",
        lambda *args, **kwargs: (
            [
                {"type": "text", "text": scene.ADAPTIVE_FRAME_INTRO_PROMPT},
                {"type": "text", "text": "[Frame at t=70.0s]"},
                {"type": "image", "image": object()},
            ],
            [70.0],
            [],
        ),
    )

    result = scene.describe_scene(video_path, zoom_signs=False, adaptive_sampling=True)

    assert "## Sampled frames" in result
    assert "- [t=70.0s] sampled frame" in result
    message_content = captured_messages["messages"][0]["content"]
    types = [element.get("type") for element in message_content]
    assert "video" not in types
    assert "image" in types


def test_describe_scene_deletes_adaptive_cleanup_paths_after_the_model_call(
    monkeypatch, tmp_path
):
    """Task #1245: _build_adaptive_message_content() now writes the
    adaptively-chosen frames into a real temp clip on disk (see
    _write_frames_as_temp_video()) instead of building a list of
    in-memory image elements, and hands describe_scene() back the temp
    clip's parent directory as a cleanup path. describe_scene() must
    delete it once the model call that reads it has finished - it's
    disposable scratch data with no other owner."""

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    cleanup_dir = tmp_path / "adaptive-clip-scratch"
    cleanup_dir.mkdir()
    (cleanup_dir / "adaptive_clip.mp4").write_bytes(b"fake clip bytes")

    class _FakeInputs(dict):
        def __init__(self):
            super().__init__()
            self.input_ids = [[1, 2, 3]]

        def to(self, device):
            return self

    class _FakeModel:
        device = "cpu"

        def generate(self, **kwargs):
            return [[1, 2, 3, 4, 5]]

    class _FakeProcessor:
        def apply_chat_template(self, messages, **kwargs):
            return "prompt text"

        def __call__(self, **kwargs):
            return _FakeInputs()

        def batch_decode(self, ids, **kwargs):
            return ["## Description\n- [t=70.0s] A red bus passes on the left."]

    loaded = scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)
    monkeypatch.setattr(
        scene,
        "_build_adaptive_message_content",
        lambda *args, **kwargs: (
            [{"type": "text", "text": "intro"}, {"type": "video", "video": "x"}],
            [70.0],
            [cleanup_dir],
        ),
    )

    assert cleanup_dir.exists()

    scene.describe_scene(video_path, zoom_signs=False, adaptive_sampling=True)

    assert not cleanup_dir.exists()


# ---------------------------------------------------------------------------
# _write_frames_as_temp_video() / _adaptive_video_intro_text() (task #1245) -
# real, Christer-measured hardware timing showed the old approach (feeding
# the model N independent `{"type": "image", ...}` elements, one per
# adaptively-chosen frame) was ~7x slower than the plain non-adaptive path
# on the identical clip (732s vs 107s), traced to losing whatever temporal-
# merging efficiency Qwen2.5-VL/Qwen3-VL-style models apply to real video
# input. These stitch the chosen frames into a small throwaway clip instead,
# fed in as a single `{"type": "video", ...}` element - see
# _build_adaptive_message_content()'s own docstring for the full story.
# ---------------------------------------------------------------------------


def test_write_frames_as_temp_video_encodes_a_real_clip(tmp_path):
    from PIL import Image

    frames = [
        (0.0, Image.new("RGB", (64, 48), color=(200, 40, 40))),
        (12.5, Image.new("RGB", (64, 48), color=(40, 200, 40))),
        (30.0, Image.new("RGB", (64, 48), color=(40, 40, 200))),
    ]

    video_path = scene._write_frames_as_temp_video(frames, fps=1.0)

    try:
        assert video_path.exists()
        assert video_path.stat().st_size > 0
    finally:
        shutil.rmtree(video_path.parent, ignore_errors=True)


def test_write_frames_as_temp_video_raises_media_tool_error_on_ffmpeg_failure(
    monkeypatch, tmp_path
):
    from PIL import Image

    class _FakeResult:
        returncode = 1
        stderr = b"ffmpeg exploded"

    monkeypatch.setattr(scene.subprocess, "run", lambda *a, **kw: _FakeResult())

    frames = [(0.0, Image.new("RGB", (32, 32)))]

    with pytest.raises(MediaToolError):
        scene._write_frames_as_temp_video(frames, fps=1.0)


# ---------------------------------------------------------------------------
# Task #1245 follow-up 8: _extract_frame_at_timestamp()/_extract_frames_at_
# timestamps() - direct per-timestamp ffmpeg seeks, replacing a
# decord.VideoReader() + get_batch() approach. Christer's real-hardware
# report during --adaptive-context-frames testing: "it takes about 18
# seconds before the model is loaded and about 60s before qwen-vl-utils
# using decord to read video" - the 60s gap was _build_adaptive_message_
# content()'s own decord.VideoReader() open, which needs to build a full
# random-access frame index over the WHOLE source file to support
# get_batch(), regardless of how few timestamps are actually being
# sampled. `-ss` given before `-i` asks ffmpeg to seek at the container
# level instead, so cost scales with how far into the file a timestamp is,
# not with the file's total length. See _extract_frame_at_timestamp()'s
# own docstring for the full story.
# ---------------------------------------------------------------------------


def test_extract_frame_at_timestamp_grabs_a_real_frame_via_ffmpeg(tmp_path):
    from PIL import Image

    frames = [
        (0.0, Image.new("RGB", (64, 48), color=(200, 40, 40))),
        (1.0, Image.new("RGB", (64, 48), color=(40, 200, 40))),
        (2.0, Image.new("RGB", (64, 48), color=(40, 40, 200))),
    ]
    video_path = scene._write_frames_as_temp_video(frames, fps=1.0)

    try:
        image = scene._extract_frame_at_timestamp(video_path, 1.0)

        assert image is not None
        assert image.mode == "RGB"
        assert image.size == (64, 48)
    finally:
        shutil.rmtree(video_path.parent, ignore_errors=True)


def test_extract_frame_at_timestamp_returns_none_on_ffmpeg_failure(monkeypatch, tmp_path):
    class _FakeResult:
        returncode = 1
        stderr = b"ffmpeg exploded"
        stdout = b""

    monkeypatch.setattr(scene.subprocess, "run", lambda *a, **kw: _FakeResult())

    result = scene._extract_frame_at_timestamp(tmp_path / "video.mp4", 5.0)

    assert result is None


def test_extract_frame_at_timestamp_raises_media_tool_error_when_ffmpeg_missing(
    monkeypatch, tmp_path
):
    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError("ffmpeg not found")

    monkeypatch.setattr(scene.subprocess, "run", _raise_not_found)

    with pytest.raises(MediaToolError):
        scene._extract_frame_at_timestamp(tmp_path / "video.mp4", 5.0)


def test_extract_frames_at_timestamps_skips_failed_frames(monkeypatch, tmp_path):
    from PIL import Image

    fake_image = Image.new("RGB", (10, 10))

    def fake_extract(video_path, timestamp):
        return None if timestamp == 5.0 else fake_image

    monkeypatch.setattr(scene, "_extract_frame_at_timestamp", fake_extract)

    frames = scene._extract_frames_at_timestamps(tmp_path / "video.mp4", [1.0, 5.0, 9.0])

    assert [timestamp for timestamp, _ in frames] == [1.0, 9.0]


def test_build_adaptive_message_content_falls_back_when_duration_probe_fails(
    monkeypatch, tmp_path
):
    """No decord import remains in _build_adaptive_message_content() -
    the duration probe now goes through media.py's ffprobe-based
    probe() (imported here as probe_video), which raises MediaToolError
    on failure instead of decord raising some arbitrary Exception on
    open. Same graceful-degradation contract as before: fall back to
    uniform sampling (signaled by the ([], [], []) return), never a
    hard failure."""

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    def fake_probe(path):
        raise MediaToolError("ffprobe failed")

    monkeypatch.setattr(scene, "probe_video", fake_probe)

    opts = scene.SceneOptions()
    content, timestamps, cleanup = scene._build_adaptive_message_content(
        video_path, opts, [], [], None, warn=lambda msg: None
    )

    assert content == []
    assert timestamps == []
    assert cleanup == []


def test_adaptive_video_intro_text_lists_every_frames_real_timestamp():
    """Task #1245 follow-up: rewritten as one flowing comma list (not
    16 near-identical dashed '- frame N: t=Xs' lines) to stop the
    structural resemblance to the model's own '- [t=Xs]' output bullets
    that was very likely feeding no_repeat_ngram_size-driven digit-
    spacing corruption in real output Christer reported - see
    _adaptive_video_intro_text()'s own docstring for the full story."""

    # total_frame_count == len(highlight_timestamps): no context frames,
    # so the "extra frames, don't caption them separately" paragraph
    # (task #1245 follow-up 7) should NOT appear.
    text = scene._adaptive_video_intro_text([0.0, 12.5, 30.0], 3)

    assert "3 separate highlighted moments" in text
    assert "0.0s, 12.5s, 30.0s" in text
    # The old dashed-bullet-per-frame format is gone - nothing in the
    # intro text should look like a "- [t=" style bullet, since that's
    # exactly what structurally resembled the model's own output format.
    assert "- frame" not in text
    assert "\n" not in text
    assert "extra real frames" not in text


def test_adaptive_video_intro_text_explains_context_frames_when_present():
    """Task #1245 follow-up 7: Christer's clarification after seeing the
    corrected, complete-but-still-verbose 80-bullet output - "The idea
    was to add some frames around, but only look for 16 of them...
    never to look att more frames from our side." When
    total_frame_count exceeds the highlight count (context frames were
    expanded in), the intro text must still list only the highlight
    timestamps as the timeline to write bullets for, but should also
    tell the model extra real frames are present for context and that
    it should NOT write one bullet per frame - see
    _adaptive_video_intro_text()'s own docstring for the full story."""

    text = scene._adaptive_video_intro_text([10.0, 30.0], 10)

    assert "2 separate highlighted moments" in text
    assert "10.0s, 30.0s" in text
    assert "extra real frames" in text
    assert "10 in total" in text
    assert "do NOT" in text
    assert "Write exactly 2 bullets total" in text


def test_plain_video_frame_timestamps_matches_christers_own_example():
    """Task #1260 follow-up 10: Christer's own challenge, verbatim -
    "split 301 s with 16, thats not to hard." It isn't - qwen_vl_utils'
    smart_nframes() picks max_frames evenly-spaced frames whenever
    duration*fps exceeds it (confirmed via reading its source, task
    #1260 follow-up 13), so this is knowable in advance. 16 frames
    evenly spaced across 301s should start at 0.0s, end at 301.0s, and
    land ~20.1s apart."""

    timestamps = scene._plain_video_frame_timestamps(301.0, 2.0, 16)

    assert len(timestamps) == 16
    assert timestamps[0] == 0.0
    assert timestamps[-1] == 301.0
    # Evenly spaced - consecutive gaps should all be very close to
    # 301.0 / 15 ~= 20.07s.
    gaps = [round(b - a, 1) for a, b in zip(timestamps, timestamps[1:])]
    assert all(abs(gap - 20.1) < 0.2 for gap in gaps)


def test_plain_video_frame_timestamps_clamps_to_min_frames_for_short_clips():
    """A clip short enough that duration*fps falls under
    qwen_vl_utils' own FPS_MIN_FRAMES=4 floor should still get 4
    timestamps, not fewer - mirrors smart_nframes()'s own clamp."""

    timestamps = scene._plain_video_frame_timestamps(1.5, 2.0, 16)

    assert len(timestamps) == 4
    assert timestamps[0] == 0.0
    assert timestamps[-1] == 1.5


def test_plain_video_frame_timestamps_degenerate_inputs_return_empty():
    """Zero/negative duration, fps, or max_frames means there's
    nothing meaningful to ground - describe_scene() should fall back
    to the old ungrounded behavior rather than crash or divide by
    zero."""

    assert scene._plain_video_frame_timestamps(0.0, 2.0, 16) == []
    assert scene._plain_video_frame_timestamps(-5.0, 2.0, 16) == []
    assert scene._plain_video_frame_timestamps(180.0, 0.0, 16) == []
    assert scene._plain_video_frame_timestamps(180.0, 2.0, 0) == []


def test_plain_video_intro_text_lists_computed_timestamps():
    """Task #1260 follow-up 10: styled like
    _adaptive_video_intro_text() (one flowing sentence, no dashed
    bullet-shaped lines) for the same reason that function gives -
    avoiding structural resemblance to the model's own '- [t=Xs]'
    output that's already caused real no_repeat_ngram_size corruption
    on the adaptive path (see that function's own docstring)."""

    text = scene._plain_video_intro_text([0.0, 20.1, 301.0], 301.0)

    assert "3 frames" in text
    assert "301.0s" in text
    assert "0.0s, 20.1s, 301.0s" in text
    assert "computed fact" in text
    assert "- frame" not in text
    assert "\n" not in text


# --- context frames around adaptive highlights (task #1245 follow-up,
# Christer's direct request: "You could also add frames before and
# after our specified friend, just to get more") --------------------------
#
# _adaptive_video_intro_text()'s rewrite above only changed prompt
# wording; this gives the model real extra visual data to back it up -
# opt-in via SceneOptions.adaptive_context_frames (default 0, no-op).


def test_expand_with_context_frames_is_a_noop_when_disabled():
    timestamps = [10.0, 30.0]

    result = scene._expand_with_context_frames(timestamps, duration_seconds=60.0, context_frames=0, offset_seconds=0.5)

    assert result == timestamps


def test_expand_with_context_frames_adds_before_and_after_each_highlight():
    result = scene._expand_with_context_frames(
        [30.0], duration_seconds=60.0, context_frames=2, offset_seconds=0.5
    )

    assert result == [29.0, 29.5, 30.0, 30.5, 31.0]


def test_expand_with_context_frames_clamps_to_video_bounds():
    # A highlight near the very start/end shouldn't produce negative or
    # past-duration timestamps - context frames that would fall outside
    # [0, duration_seconds] clamp to the boundary instead.
    result = scene._expand_with_context_frames(
        [0.2, 59.8], duration_seconds=60.0, context_frames=1, offset_seconds=0.5
    )

    assert min(result) == 0.0
    assert max(result) == 60.0
    assert 0.2 in result and 59.8 in result


def test_expand_with_context_frames_dedupes_overlapping_windows():
    # Two highlights close enough together that their context windows
    # overlap shouldn't produce duplicate timestamps for
    # _extract_frames_at_timestamps() to decode twice.
    result = scene._expand_with_context_frames(
        [10.0, 10.5], duration_seconds=60.0, context_frames=1, offset_seconds=0.5
    )

    assert result == sorted(set(result))
    assert result == [9.5, 10.0, 10.5, 11.0]


# --- photo support (Christer's real report: "pictures dont get scene
# asset") ------------------------------------------------------------------
#
# describe_scene() used to build a `{"type": "video", ...}` message for
# every input, unconditionally - a photo recording's FRONT file got
# handed straight to qwen_vl_utils' video-decoding path (decord), which
# can't open a still image at all. Fixed by branching on is_photo_path()
# and decoding via _photo_as_pil_image() (an ffmpeg subprocess, not PIL
# directly - PIL doesn't reliably cover every PHOTO_EXTENSIONS member,
# same reasoning as export/media.py's render_image_as_video()) into a
# real `{"type": "image", ...}` element instead.


def test_photo_as_pil_image_decodes_a_real_jpeg_via_ffmpeg(tmp_path):
    from PIL import Image

    photo_path = tmp_path / "photo.jpg"
    Image.new("RGB", (64, 48), color=(200, 40, 40)).save(photo_path)

    result = scene._photo_as_pil_image(photo_path)

    assert result.size == (64, 48)
    assert result.mode == "RGB"


def test_photo_as_pil_image_raises_media_tool_error_on_unreadable_file(tmp_path):
    bad_path = tmp_path / "not_really_a_photo.jpg"
    bad_path.write_bytes(b"this is not a jpeg")

    with pytest.raises(MediaToolError):
        scene._photo_as_pil_image(bad_path)


def test_extract_full_res_frames_returns_a_single_frame_for_a_photo(tmp_path):
    """A photo has no timeline to sample - _extract_full_res_frames()
    must return exactly one (0.0, PIL.Image) entry decoded via
    _photo_as_pil_image(), never reaching decord.VideoReader (which
    can't open a still image, and isn't even installed in this test
    environment - a ModuleNotFoundError here would mean the video
    branch was wrongly taken)."""

    from PIL import Image

    photo_path = tmp_path / "photo.jpg"
    Image.new("RGB", (32, 24), color=(10, 200, 10)).save(photo_path)

    frames = scene._extract_full_res_frames(photo_path, 4)

    assert len(frames) == 1
    timestamp, frame = frames[0]
    assert timestamp == 0.0
    assert frame.size == (32, 24)


class _FakeInputs(dict):
    """Dict-like so **inputs works in describe_scene()'s
    model.generate(**inputs, ...) call, the same way a real HF
    BatchFeature supports mapping unpacking. Module-level (not nested
    in the disclaimer test above) since the photo-content tests below
    need the same stand-in."""

    def __init__(self):
        super().__init__()
        self.input_ids = [[1, 2, 3]]

    def to(self, device):
        return self


class _FakeModel:
    device = "cpu"

    def generate(self, **kwargs):
        return [[1, 2, 3, 4, 5]]


class _FakeProcessor:
    def apply_chat_template(self, messages, **kwargs):
        return "prompt text"

    def __call__(self, **kwargs):
        return _FakeInputs()

    def batch_decode(self, ids, **kwargs):
        return ["## Description\nRoutine driving, nothing notable happened."]


def _fake_loaded_scene_model(process_vision_info):
    return scene._LoadedSceneModel(
        model=_FakeModel(),
        processor=_FakeProcessor(),
        process_vision_info=process_vision_info,
        patch_factor=28,
        is_qwen3=False,
    )


def test_describe_scene_builds_an_image_content_element_for_a_photo(monkeypatch, tmp_path):
    from PIL import Image

    photo_path = tmp_path / "photo.jpg"
    Image.new("RGB", (40, 30), color=(0, 0, 200)).save(photo_path)

    captured_messages = []

    def fake_process_vision_info(messages, **kwargs):
        captured_messages.append(messages)
        return [], [], {}

    loaded = _fake_loaded_scene_model(fake_process_vision_info)
    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)

    # crop_top/crop_bottom off so the asserted size below reflects the
    # source photo exactly - describe_scene() applies the same overlay
    # crop to a photo as it does to a video (see _crop_overlay_from_image()
    # call in the image branch), which is correct behavior but would
    # otherwise make this assertion depend on SceneOptions' own tuned
    # crop defaults rather than on the photo branch actually running.
    result = scene.describe_scene(
        photo_path, zoom_signs=False, crop_top=0.0, crop_bottom=0.0
    )

    assert result.startswith("## Description")
    content = captured_messages[0][0]["content"]
    image_ele = content[0]
    assert image_ele["type"] == "image"
    # A real decoded PIL Image, not a path string - confirms
    # _photo_as_pil_image() actually ran rather than the video branch
    # silently swallowing the photo path as a "video" string.
    assert image_ele["image"].size == (40, 30)


def test_describe_scene_still_builds_a_video_content_element_for_a_real_video(
    monkeypatch, tmp_path
):
    """Regression check: an ordinary (non-photo-extension) path must
    still get the original `{"type": "video", ...}` treatment,
    unaffected by the new photo branch."""

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    captured_messages = []

    def fake_process_vision_info(messages, **kwargs):
        captured_messages.append(messages)
        return [], [], {}

    loaded = _fake_loaded_scene_model(fake_process_vision_info)
    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)

    scene.describe_scene(video_path, zoom_signs=False)

    content = captured_messages[0][0]["content"]
    video_ele = content[0]
    assert video_ele["type"] == "video"
    assert video_ele["video"] == str(video_path.resolve())
    assert video_ele["max_frames"] == 16


def test_describe_scene_video_element_uses_fixed_resize(monkeypatch, tmp_path):
    """2026-08-19: after briefly experimenting with total_pixels
    budgeting (to trade resolution for higher max_frames) and then
    reverting that entirely at Christer's request ("I want to go back
    to 16"), the video branch is back to passing a fixed
    resized_width/resized_height, same as the photo branch - see the
    sibling test above for that one, and SceneOptions' own docstring
    in scene.py for the full history."""

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"x")

    captured_messages = []

    def fake_process_vision_info(messages, **kwargs):
        captured_messages.append(messages)
        return [], [], {}

    loaded = _fake_loaded_scene_model(fake_process_vision_info)
    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu, quantize="none", **_: loaded)

    scene.describe_scene(video_path, zoom_signs=False)

    video_ele = captured_messages[0][0]["content"][0]
    assert video_ele["resized_width"] == 1092
    assert video_ele["resized_height"] == 588
    assert "total_pixels" not in video_ele


# --- unload_scene_model() -------------------------------------------------
#
# "Scene model never unloads from GPU" (Christer). These tests exercise
# the cache-eviction logic in isolation - no real torch/transformers
# needed, since unload_scene_model() only imports torch after it has
# already found at least one entry to drop, and none of these fake
# entries require torch to construct.


def _fake_loaded_model():
    return scene._LoadedSceneModel(
        model=object(),
        processor=object(),
        process_vision_info=lambda messages, **kwargs: ([], [], {}),
        patch_factor=28,
        is_qwen3=False,
    )


def test_unload_scene_model_noop_on_empty_cache(monkeypatch):
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", {})

    # Must not raise, and must not even try to import torch - if it
    # did and torch weren't installed, this would blow up instead of
    # quietly returning.
    scene.unload_scene_model()

    assert scene._SCENE_MODEL_CACHE == {}


def test_unload_scene_model_no_args_clears_everything(monkeypatch):
    cache = {
        "model-a:auto:none": _fake_loaded_model(),
        "model-b:cpu:none": _fake_loaded_model(),
    }
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", cache)

    scene.unload_scene_model()

    assert cache == {}


def test_unload_scene_model_by_name_evicts_both_variants_only(monkeypatch):
    # model-a has entries under both cpu/auto *and* two different
    # resolved quantize levels (auto could have resolved to "int8" on
    # one call and an explicit "int4" on another, within the same
    # process) - model_name-only eviction must drop every one of
    # model-a's entries regardless of quantize level, matching
    # _get_scene_model()'s own cache-key scheme (see unload_scene_model()'s
    # docstring).
    cache = {
        "model-a:auto:none": _fake_loaded_model(),
        "model-a:auto:int8": _fake_loaded_model(),
        "model-a:cpu:none": _fake_loaded_model(),
        "model-b:auto:none": _fake_loaded_model(),
    }
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", cache)

    scene.unload_scene_model("model-a")

    assert list(cache) == ["model-b:auto:none"]


def test_unload_scene_model_by_name_and_force_cpu_evicts_one_entry(monkeypatch):
    cache = {
        "model-a:auto:none": _fake_loaded_model(),
        "model-a:cpu:none": _fake_loaded_model(),
        "model-a:cpu:int4": _fake_loaded_model(),
    }
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", cache)

    scene.unload_scene_model("model-a", force_cpu=True)

    # Both cpu entries (regardless of quantize level) are evicted -
    # force_cpu narrows to the cpu/auto axis only, not quantize.
    assert list(cache) == ["model-a:auto:none"]


def test_unload_scene_model_unknown_name_is_a_noop(monkeypatch):
    cache = {"model-a:auto:none": _fake_loaded_model()}
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", cache)

    scene.unload_scene_model("no-such-model")

    # The one real entry survives untouched.
    assert list(cache) == ["model-a:auto:none"]


def test_unload_scene_model_safe_when_torch_not_installed(monkeypatch):
    """If torch was never importable, the module wouldn't have been
    able to load a real scene model in the first place, so the cache
    would be empty anyway - but guard the import-failure path itself
    in case that assumption ever changes."""

    import builtins

    cache = {"model-a:auto:none": _fake_loaded_model()}
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", cache)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("no torch here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    scene.unload_scene_model()

    assert cache == {}
