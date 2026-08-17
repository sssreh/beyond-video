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


def test_scene_options_defaults_match_tuned_values():
    opts = scene.SceneOptions()

    assert opts.task == "both"
    assert opts.model == scene.DEFAULT_MODEL
    assert opts.fps == 1.0
    assert opts.max_frames == 16
    assert opts.zoom_signs is True
    assert opts.zoom_plate_confidence_check is True
    assert opts.force_cpu is False


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

    call_count = {"n": 0}

    def fake_run_single_image_prompt(image, prompt, loaded, opts, **kwargs):
        call_count["n"] += 1
        if prompt is scene.GROUND_PROMPT:
            return '[{"bbox_2d": [10, 10, 60, 40], "label": "vehicle license plate"}]'
        # First OCR call (greedy): "ETR734". Second (force_sample,
        # the confidence check): a different read, "FTR78P" - the two
        # should disagree.
        if kwargs.get("force_sample"):
            return "FTR78P"
        return "ETR734"

    monkeypatch.setattr(scene, "_run_single_image_prompt", fake_run_single_image_prompt)

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
        if prompt is scene.GROUND_PROMPT:
            return '[{"bbox_2d": [10, 10, 60, 40], "label": "vehicle license plate"}]'
        return "ABC123"

    monkeypatch.setattr(scene, "_run_single_image_prompt", fake_run_single_image_prompt)

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
        calls.append((prompt is scene.GROUND_PROMPT, kwargs.get("force_sample", False)))
        if prompt is scene.GROUND_PROMPT:
            return '[{"bbox_2d": [10, 10, 60, 40], "label": "shop sign"}]'
        return "OPEN 24 HOURS"

    monkeypatch.setattr(scene, "_run_single_image_prompt", fake_run_single_image_prompt)

    loaded = _FakeLoadedModel()
    opts = scene.SceneOptions(zoom_plate_confidence_check=True)

    result = scene._zoom_into_signs(Path("fake.mp4"), loaded, opts)

    # Exactly one grounding call + one OCR call - no second
    # (force_sample) confidence-check call for a non-plate label.
    assert len(calls) == 2
    assert not any(force_sample for _is_ground, force_sample in calls)
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

    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu: loaded)

    result = scene.describe_scene(video_path, zoom_signs=False)

    assert result.startswith("## Description")
    assert scene.DISCLAIMER in result


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
    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu: loaded)

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
    monkeypatch.setattr(scene, "_get_scene_model", lambda model, *, force_cpu: loaded)

    scene.describe_scene(video_path, zoom_signs=False)

    content = captured_messages[0][0]["content"]
    video_ele = content[0]
    assert video_ele["type"] == "video"
    assert video_ele["video"] == str(video_path.resolve())


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
        "model-a:auto": _fake_loaded_model(),
        "model-b:cpu": _fake_loaded_model(),
    }
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", cache)

    scene.unload_scene_model()

    assert cache == {}


def test_unload_scene_model_by_name_evicts_both_variants_only(monkeypatch):
    cache = {
        "model-a:auto": _fake_loaded_model(),
        "model-a:cpu": _fake_loaded_model(),
        "model-b:auto": _fake_loaded_model(),
    }
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", cache)

    scene.unload_scene_model("model-a")

    assert list(cache) == ["model-b:auto"]


def test_unload_scene_model_by_name_and_force_cpu_evicts_one_entry(monkeypatch):
    cache = {
        "model-a:auto": _fake_loaded_model(),
        "model-a:cpu": _fake_loaded_model(),
    }
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", cache)

    scene.unload_scene_model("model-a", force_cpu=True)

    assert list(cache) == ["model-a:auto"]


def test_unload_scene_model_unknown_name_is_a_noop(monkeypatch):
    cache = {"model-a:auto": _fake_loaded_model()}
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", cache)

    scene.unload_scene_model("no-such-model")

    # The one real entry survives untouched.
    assert list(cache) == ["model-a:auto"]


def test_unload_scene_model_safe_when_torch_not_installed(monkeypatch):
    """If torch was never importable, the module wouldn't have been
    able to load a real scene model in the first place, so the cache
    would be empty anyway - but guard the import-failure path itself
    in case that assumption ever changes."""

    import builtins

    cache = {"model-a:auto": _fake_loaded_model()}
    monkeypatch.setattr(scene, "_SCENE_MODEL_CACHE", cache)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("no torch here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    scene.unload_scene_model()

    assert cache == {}
