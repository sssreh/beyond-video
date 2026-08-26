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


def test_adaptive_video_intro_text_lists_every_frames_real_timestamp():
    text = scene._adaptive_video_intro_text([0.0, 12.5, 30.0])

    assert "3 individual moments" in text
    assert "frame 1: t=0.0s" in text
    assert "frame 2: t=12.5s" in text
    assert "frame 3: t=30.0s" in text


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
