import argparse
from pathlib import Path

import pytest

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.cli import bv_generate
from blackvue.cli.bv_generate import _language_from_generated_filename
from blackvue.cli.bv_generate import _language_suffixed_name
from blackvue.cli.bv_generate import _should_write
from blackvue.cli.bv_generate import _translate_diarized
from blackvue.cli.bv_generate import _translate_segments
from blackvue.cli.bv_generate import main
from blackvue.cli.bv_generate import parse_args
from blackvue.core.camera_config import CameraConfig
from blackvue.core.camera_config import config_path
from blackvue.core.camera_config import save_camera_config
from blackvue.generate import SCENE_DEFAULT_MODEL
from blackvue.generate.media import MediaToolError
from blackvue.generate.speech import SpeakerTurn
from blackvue.generate.speech import SpeechSegment
from blackvue.generate.speech import Transcript


def test_main_resolves_a_camera_id_to_its_configured_target(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()

    config_dir = tmp_path / "config"
    save_camera_config(
        config_path(config_dir, "Kirby"),
        CameraConfig(id="Kirby", name="Kirby", archive=archive),
    )

    exit_code = main(
        ["Kirby", "--config-dir", str(config_dir), "--get-duration"]
    )

    out = capsys.readouterr().out

    assert exit_code == 0
    assert str(archive) in out
    assert "no recordings found" in out


def _base_args(**overrides):
    defaults = dict(
        extract_audio=False,
        get_duration=False,
        transcribe=False,
        translate=None,
        language=None,
        model_size="small",
        npu_model_dir=None,
        cpu=False,
        diarize=False,
        hf_token=None,
        srt=False,
        lrc=False,
        describe_scene=False,
        scene_model=SCENE_DEFAULT_MODEL,
        camera="front",
        overwrite=False,
        dry_run=False,
        verbose=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _refuse(*_args, **_kwargs):
    raise AssertionError("should not have been called for a parking recording")


def test_parse_args_requires_at_least_one_action():
    with pytest.raises(SystemExit):
        parse_args(["/some/path"])


def test_parse_args_accepts_extract_audio():
    args = parse_args(["/some/path", "--extract-audio"])

    assert args.extract_audio is True
    assert args.get_duration is False
    assert args.translate is None


def test_parse_args_translate_takes_a_language():
    args = parse_args(["/some/path", "--translate", "es"])

    assert args.translate == "es"


def test_parse_args_normalizes_3letter_translate_code():
    args = parse_args(["/some/path", "--translate", "eng"])

    assert args.translate == "en"


def test_parse_args_normalizes_3letter_language_code():
    args = parse_args(["/some/path", "--transcribe", "--language", "swe"])

    assert args.language == "sv"


def test_parse_args_defaults_path_to_cwd():
    args = parse_args(["--get-duration"])

    assert args.path == "."


def test_parse_args_diarize_requires_transcribe_or_translate():
    with pytest.raises(SystemExit):
        parse_args(["/some/path", "--extract-audio", "--diarize"])


def test_parse_args_diarize_allowed_with_transcribe():
    args = parse_args(["/some/path", "--transcribe", "--diarize"])

    assert args.diarize is True


def test_parse_args_npu_model_dir_defaults_to_none():
    args = parse_args(["/some/path", "--transcribe"])

    assert args.npu_model_dir is None


def test_parse_args_npu_model_dir_requires_language():
    with pytest.raises(SystemExit):
        parse_args([
            "/some/path", "--transcribe", "--npu-model-dir", "/tmp/npu-model",
        ])


def test_parse_args_npu_model_dir_allowed_with_language():
    args = parse_args([
        "/some/path", "--transcribe",
        "--npu-model-dir", "/tmp/npu-model", "--language", "en",
    ])

    assert args.npu_model_dir == Path("/tmp/npu-model")
    assert args.language == "en"


def test_parse_args_cpu_flag_defaults_to_false():
    args = parse_args(["/some/path", "--transcribe"])

    assert args.cpu is False


def test_parse_args_cpu_flag_can_be_set():
    args = parse_args(["/some/path", "--transcribe", "--cpu"])

    assert args.cpu is True


def test_parse_args_model_size_explicit_value_is_kept_even_on_a_gpu_machine(
    monkeypatch,
):
    # An explicit --model-size always wins - gpu_available() should not
    # even be consulted once the user has said what they want.
    monkeypatch.setattr(
        bv_generate,
        "gpu_available",
        lambda: (_ for _ in ()).throw(
            AssertionError("should not check for a GPU with an explicit --model-size")
        ),
    )

    args = parse_args(["/some/path", "--transcribe", "--model-size", "medium"])

    assert args.model_size == "medium"


def test_parse_args_model_size_defaults_to_large_on_a_gpu_machine(monkeypatch):
    # Christer: "I would like medium or large model default if you have
    # a gpu" - he picked "large" specifically.
    monkeypatch.setattr(bv_generate, "gpu_available", lambda: True)

    args = parse_args(["/some/path", "--transcribe"])

    assert args.model_size == "large"


def test_parse_args_model_size_defaults_to_small_without_a_gpu(monkeypatch):
    monkeypatch.setattr(bv_generate, "gpu_available", lambda: False)

    args = parse_args(["/some/path", "--transcribe"])

    assert args.model_size == "small"


def test_parse_args_model_size_default_resolution_applies_to_translate_too(
    monkeypatch,
):
    monkeypatch.setattr(bv_generate, "gpu_available", lambda: True)

    args = parse_args(["/some/path", "--translate", "es"])

    assert args.model_size == "large"


def test_parse_args_model_size_skips_gpu_check_for_extract_audio_only(
    monkeypatch,
):
    # --extract-audio/--get-duration never touch Whisper at all, so
    # resolving a GPU-aware default for them would just be an
    # unnecessary ctranslate2 import - gpu_available() should never be
    # called in that case.
    monkeypatch.setattr(
        bv_generate,
        "gpu_available",
        lambda: (_ for _ in ()).throw(
            AssertionError("should not check for a GPU when no Whisper action is requested")
        ),
    )

    args = parse_args(["/some/path", "--extract-audio"])

    assert args.model_size == "small"


def test_parse_args_model_size_skips_gpu_check_with_npu_model_dir(monkeypatch):
    # The NPU backend doesn't use model_size at all - --npu-model-dir
    # bypasses faster-whisper entirely, so there's no reason to probe
    # for a CUDA GPU either.
    monkeypatch.setattr(
        bv_generate,
        "gpu_available",
        lambda: (_ for _ in ()).throw(
            AssertionError("should not check for a GPU when --npu-model-dir is given")
        ),
    )

    args = parse_args([
        "/some/path", "--transcribe",
        "--npu-model-dir", "/tmp/npu-model", "--language", "en",
    ])

    assert args.model_size == "small"


def test_translate_diarized_preserves_speaker_labels(monkeypatch):
    calls = []

    def fake_translate(text, *, source_language, target_language):
        calls.append(text)
        return text.upper()

    monkeypatch.setattr(bv_generate, "translate", fake_translate)

    text = "[SPEAKER_00] hello there\n[SPEAKER_01] hi"

    result = _translate_diarized(
        text, source_language="en", target_language="es"
    )

    assert result == "[SPEAKER_00] HELLO THERE\n[SPEAKER_01] HI"
    assert calls == ["hello there", "hi"]


def test_translate_diarized_passes_through_unlabeled_lines(monkeypatch):
    monkeypatch.setattr(
        bv_generate, "translate", lambda text, **_: text.upper()
    )

    result = _translate_diarized(
        "just plain text", source_language="en", target_language="es"
    )

    assert result == "JUST PLAIN TEXT"


def test_translate_segments_translates_each_text_and_keeps_timing(
    monkeypatch,
):
    calls = []

    def fake_translate(text, *, source_language, target_language):
        calls.append(text)
        return text.upper()

    monkeypatch.setattr(bv_generate, "translate", fake_translate)

    segments = (
        SpeechSegment(0.0, 1.0, "hello"),
        SpeechSegment(1.0, 2.5, "there"),
    )

    result = _translate_segments(
        segments, source_language="en", target_language="es"
    )

    assert [s.text for s in result] == ["HELLO", "THERE"]
    assert [(s.start, s.end) for s in result] == [(0.0, 1.0), (1.0, 2.5)]
    assert calls == ["hello", "there"]


def test_translate_segments_empty_input_returns_empty(monkeypatch):
    monkeypatch.setattr(bv_generate, "translate", _refuse)

    assert _translate_segments((), source_language="en", target_language="es") == ()


def test_language_suffixed_name_default_language_stays_plain():
    name = _language_suffixed_name(
        "20260715_133255_N", "en", "transcript.txt"
    )

    assert name == "20260715_133255_N.transcript.txt"


def test_language_suffixed_name_non_default_language_gets_suffix():
    name = _language_suffixed_name(
        "20260715_133255_N", "sv", "translation.txt"
    )

    assert name == "20260715_133255_N_swe.translation.txt"


def test_language_suffixed_name_is_case_insensitive_for_default_check():
    name = _language_suffixed_name(
        "20260715_133255_N", "EN", "transcript.txt"
    )

    assert name == "20260715_133255_N.transcript.txt"


def test_language_suffixed_name_diarized_default_language():
    name = _language_suffixed_name(
        "20260715_133255_N", "en", "transcript.txt", diarized=True
    )

    assert name == "20260715_133255_N.diarized.transcript.txt"


def test_language_suffixed_name_diarized_non_default_language():
    name = _language_suffixed_name(
        "20260715_133255_N", "sv", "translation.txt", diarized=True
    )

    assert name == "20260715_133255_N_swe.diarized.translation.txt"


def test_language_from_generated_filename_diarized_default():
    language = _language_from_generated_filename(
        "20260715_133255_N",
        "20260715_133255_N.diarized.transcript.txt",
        "transcript.txt",
    )

    assert language == "en"


def test_language_from_generated_filename_diarized_suffixed():
    language = _language_from_generated_filename(
        "20260715_133255_N",
        "20260715_133255_N_tha.diarized.transcript.txt",
        "transcript.txt",
    )

    assert language == "th"


def test_should_write_true_for_missing_file(tmp_path):
    target = tmp_path / "missing.aac"

    assert _should_write(target, overwrite=False, dry_run=False) is True


def test_should_write_true_when_overwrite_forced(tmp_path):
    target = tmp_path / "existing.aac"
    target.write_text("x")

    assert _should_write(target, overwrite=True, dry_run=False) is True


def test_should_write_false_in_dry_run_for_existing_file(tmp_path):
    target = tmp_path / "existing.aac"
    target.write_text("x")

    assert _should_write(target, overwrite=False, dry_run=True) is False


def test_should_write_false_when_batch_and_not_overwriting(
    tmp_path, monkeypatch
):
    target = tmp_path / "existing.aac"
    target.write_text("x")

    monkeypatch.setattr(bv_generate, "_interactive", lambda: False)

    assert _should_write(target, overwrite=False, dry_run=False) is False


def test_should_write_prompts_and_accepts_yes_when_interactive(
    tmp_path, monkeypatch
):
    target = tmp_path / "existing.aac"
    target.write_text("x")

    monkeypatch.setattr(bv_generate, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    assert _should_write(target, overwrite=False, dry_run=False) is True


def test_should_write_prompts_and_defaults_to_no_when_interactive(
    tmp_path, monkeypatch
):
    target = tmp_path / "existing.aac"
    target.write_text("x")

    monkeypatch.setattr(bv_generate, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    assert _should_write(target, overwrite=False, dry_run=False) is False


def test_extract_audio_skips_parking_recordings(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(bv_generate, "select_source", _refuse)

    recording = Recording(id=RecordingId("20260715_134010_P"))
    args = _base_args(extract_audio=True)

    had_error = bv_generate._do_extract_audio(recording, tmp_path, args)

    assert had_error is False
    assert not (tmp_path / "20260715_134010_P.aac").exists()
    assert "no audio" in capsys.readouterr().err


def test_transcribe_and_translate_skip_parking_recordings(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(bv_generate, "transcribe", _refuse)
    monkeypatch.setattr(bv_generate, "detect_language", _refuse)
    monkeypatch.setattr(bv_generate, "translate", _refuse)
    monkeypatch.setattr(bv_generate, "select_source", _refuse)

    recording = Recording(id=RecordingId("20260715_134010_P"))
    args = _base_args(transcribe=True, translate="sv")

    had_error = bv_generate._do_transcribe_and_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    assert not (tmp_path / "20260715_134010_P.transcript.txt").exists()
    assert "no audio" in capsys.readouterr().err


def test_get_duration_still_runs_for_parking_recordings(
    tmp_path, monkeypatch
):
    # get-duration is the one action that *should* run on parking
    # (timelapse) recordings - that's the whole reason the span
    # calculation multiplies by frame rate.
    called = []

    from blackvue.archive.asset import Asset
    from blackvue.archive.asset_file import AssetFile

    def fake_get_span(recording_id, path):
        called.append(path)
        return 1800

    monkeypatch.setattr(bv_generate, "get_span", fake_get_span)

    recording = Recording(id=RecordingId("20260715_134010_P"))
    video = tmp_path / "20260715_134010_PF.mp4"
    video.write_bytes(b"x")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video
    )

    args = _base_args(get_duration=True)

    had_error = bv_generate._do_get_duration(recording, tmp_path, args)

    assert had_error is False
    assert called == [video]
    assert (
        tmp_path / "20260715_134010_P.duration.txt"
    ).read_text().strip() == "1800"


def test_language_from_generated_filename_default():
    language = _language_from_generated_filename(
        "20260715_133255_N",
        "20260715_133255_N.transcript.txt",
        "transcript.txt",
    )

    assert language == "en"


def test_language_from_generated_filename_suffixed():
    language = _language_from_generated_filename(
        "20260715_133255_N",
        "20260715_133255_N_tha.transcript.txt",
        "transcript.txt",
    )

    assert language == "th"


def _fake_transcribe_factory(calls, language="th"):
    """Build a transcribe() stub that mimics auto-detection.

    The real call site always passes language= explicitly (args.language,
    which is None when the user didn't force one) - so the fallback has
    to happen *inside* here, the same way Whisper's own auto-detect
    would fill in a real language.
    """

    default_language = language

    def fake_transcribe(
        source,
        *,
        language=None,
        model_size="small",
        npu_model_dir=None,
        force_cpu=False,
    ):
        calls.append(source)
        return Transcript(
            text="hello world",
            language=language or default_language,
            segments=(SpeechSegment(0.0, 1.0, "hello world"),),
        )

    return fake_transcribe


def test_translate_only_reuses_existing_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(bv_generate, "transcribe", _refuse)
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: (
            f"[{source_language}->{target_language}] {text}"
        ),
    )

    recording = Recording(id=RecordingId("20260715_133255_N"))
    transcript_path = tmp_path / "20260715_133255_N_tha.transcript.txt"
    transcript_path.write_text("hej da")
    recording.assets[Asset.TRANSCRIPT] = AssetFile(
        asset=Asset.TRANSCRIPT, path=transcript_path
    )

    args = _base_args(translate="sv")

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    out = tmp_path / "20260715_133255_N_swe.translation.txt"
    assert out.read_text().strip() == "[th->sv] hej da"


def test_translate_only_reuses_existing_audio_and_persists_transcript(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(calls)
    )
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: text.upper(),
    )

    recording = Recording(id=RecordingId("20260715_140000_N"))
    audio_path = tmp_path / "20260715_140000_N.aac"
    audio_path.write_bytes(b"a")
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=audio_path
    )

    args = _base_args(translate="sv")

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    assert calls == [audio_path]
    assert (tmp_path / "20260715_140000_N_tha.transcript.txt").exists()
    assert (tmp_path / "20260715_140000_N_swe.translation.txt").exists()


def test_translate_only_re_extracts_when_cached_audio_is_empty(
    tmp_path, monkeypatch
):
    """--translate's own cached-audio reuse (recording.file(Asset.AUDIO))
    must apply the same self-healing check as --transcribe's - a
    tracked but empty .aac (the real-world leftover from a failed
    extraction) has to be treated as absent, not handed to
    transcribe() as-is."""

    extracted = []

    def fake_extract_audio(source, destination):
        extracted.append((source, destination))
        destination.write_bytes(b"real-audio")

    calls = []
    monkeypatch.setattr(bv_generate, "extract_audio", fake_extract_audio)
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(calls)
    )
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: text.upper(),
    )

    recording = Recording(id=RecordingId("20260715_140000_N"))
    video_path = tmp_path / "20260715_140000_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )
    empty_aac = tmp_path / "20260715_140000_N.aac"
    empty_aac.write_bytes(b"")
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=empty_aac
    )

    args = _base_args(translate="sv")

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    assert extracted == [(video_path, empty_aac)]
    assert calls == [empty_aac]


def test_translate_only_extracts_and_persists_from_scratch(
    tmp_path, monkeypatch
):
    extracted = []

    def fake_extract_audio(source, destination):
        extracted.append((source, destination))
        destination.write_bytes(b"audio")

    calls = []
    monkeypatch.setattr(bv_generate, "extract_audio", fake_extract_audio)
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(calls)
    )
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: text.upper(),
    )

    recording = Recording(id=RecordingId("20260715_150000_N"))
    video_path = tmp_path / "20260715_150000_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )

    args = _base_args(translate="sv")

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    assert len(extracted) == 1
    assert (tmp_path / "20260715_150000_N.aac").exists()
    assert (tmp_path / "20260715_150000_N_tha.transcript.txt").exists()
    assert (tmp_path / "20260715_150000_N_swe.translation.txt").exists()


def test_translate_only_with_diarize_bypasses_cached_transcript(
    tmp_path, monkeypatch
):
    calls = []
    diarize_calls = []

    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(calls)
    )
    monkeypatch.setattr(
        bv_generate,
        "diarize",
        lambda source, *, hf_token: diarize_calls.append(source)
        or (SpeakerTurn(0.0, 1.0, "SPEAKER_00"),),
    )
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: text.upper(),
    )

    recording = Recording(id=RecordingId("20260715_160000_N"))
    transcript_path = tmp_path / "20260715_160000_N.transcript.txt"
    transcript_path.write_text("cached text")
    recording.assets[Asset.TRANSCRIPT] = AssetFile(
        asset=Asset.TRANSCRIPT, path=transcript_path
    )
    audio_path = tmp_path / "20260715_160000_N.aac"
    audio_path.write_bytes(b"a")
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=audio_path
    )

    args = _base_args(translate="sv", diarize=True)

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    # A fresh transcription happens (diarization needs segment
    # timing a cached plain-text transcript doesn't have), but the
    # already-extracted audio is still reused rather than re-extracted.
    assert calls == [audio_path]
    assert diarize_calls == [audio_path]


def test_translate_only_diarize_produces_diarized_filenames(
    tmp_path, monkeypatch
):
    def fake_transcribe(
        source, *, language, model_size, npu_model_dir=None, force_cpu=False
    ):
        return Transcript(
            text="hello",
            language=language or "th",
            segments=(SpeechSegment(0.0, 1.0, "hello"),),
        )

    monkeypatch.setattr(bv_generate, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        bv_generate,
        "diarize",
        lambda source, *, hf_token: (SpeakerTurn(0.0, 1.0, "SPEAKER_00"),),
    )
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: text,
    )

    recording = Recording(id=RecordingId("20260715_170000_N"))
    audio_path = tmp_path / "20260715_170000_N.aac"
    audio_path.write_bytes(b"a")
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=audio_path
    )

    args = _base_args(translate="sv", diarize=True)

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    assert (
        tmp_path / "20260715_170000_N_tha.diarized.transcript.txt"
    ).exists()
    assert (
        tmp_path / "20260715_170000_N_swe.diarized.translation.txt"
    ).exists()


def test_translate_only_diarize_reuses_existing_diarized_transcript(
    tmp_path, monkeypatch
):
    # Diarized and plain transcripts are tracked as separate assets,
    # so a --translate --diarize run should reuse an already-diarized
    # transcript (no need to re-run Whisper+pyannote) the same way a
    # plain run reuses a plain one.
    monkeypatch.setattr(bv_generate, "transcribe", _refuse)
    monkeypatch.setattr(bv_generate, "diarize", _refuse)
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: (
            f"[{source_language}->{target_language}] {text}"
        ),
    )

    recording = Recording(id=RecordingId("20260715_180000_N"))
    diarized_transcript_path = (
        tmp_path / "20260715_180000_N_tha.diarized.transcript.txt"
    )
    diarized_transcript_path.write_text("[SPEAKER_00] cached text")
    recording.assets[Asset.TRANSCRIPT_DIARIZED] = AssetFile(
        asset=Asset.TRANSCRIPT_DIARIZED, path=diarized_transcript_path
    )

    args = _base_args(translate="sv", diarize=True)

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    out = tmp_path / "20260715_180000_N_swe.diarized.translation.txt"
    # _translate_diarized only sends the spoken text (not the label)
    # through translate(), then re-attaches the label.
    assert out.read_text().strip() == "[SPEAKER_00] [th->sv] cached text"


def test_translate_only_ignores_diarized_transcript_when_not_diarizing(
    tmp_path, monkeypatch
):
    # A recording that only has a diarized transcript (no plain one)
    # must not be mistaken for a plain-transcript cache hit - it
    # should fall through and re-transcribe instead.
    calls = []
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(calls, "th")
    )
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: text.upper(),
    )

    recording = Recording(id=RecordingId("20260715_190000_N"))
    diarized_transcript_path = (
        tmp_path / "20260715_190000_N.diarized.transcript.txt"
    )
    diarized_transcript_path.write_text("[SPEAKER_00] cached text")
    recording.assets[Asset.TRANSCRIPT_DIARIZED] = AssetFile(
        asset=Asset.TRANSCRIPT_DIARIZED, path=diarized_transcript_path
    )
    audio_path = tmp_path / "20260715_190000_N.aac"
    audio_path.write_bytes(b"a")
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=audio_path
    )

    args = _base_args(translate="sv")

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    assert calls == [audio_path], "should re-transcribe, not reuse cache"


def test_transcribe_extracts_and_persists_audio_when_missing(
    tmp_path, monkeypatch
):
    extracted = []
    transcribed = []

    def fake_extract_audio(source, destination):
        extracted.append((source, destination))
        destination.write_bytes(b"audio")

    def fake_transcribe(
        source, *, language, model_size, npu_model_dir=None, force_cpu=False
    ):
        transcribed.append(source)
        return Transcript(
            text="hej da",
            language=language or "sv",
            segments=(SpeechSegment(0.0, 1.0, "hej da"),),
        )

    monkeypatch.setattr(bv_generate, "extract_audio", fake_extract_audio)
    monkeypatch.setattr(bv_generate, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        bv_generate,
        "detect_language",
        lambda source, *, model_size, force_cpu=False: "sv",
    )

    recording = Recording(id=RecordingId("20260715_133255_N"))
    video_path = tmp_path / "20260715_133255_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )

    args = _base_args(transcribe=True)

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    aac_path = tmp_path / "20260715_133255_N.aac"
    assert extracted == [(video_path, aac_path)]
    assert transcribed == [aac_path]
    assert aac_path.exists()


def test_transcribe_threads_npu_model_dir_into_transcribe_call(
    tmp_path, monkeypatch
):
    # --npu-model-dir has to actually reach transcribe() for the NPU
    # backend (blackvue.generate.speech) to ever get used - this is
    # the CLI-layer half of that wiring; the backend itself (bypassing
    # faster-whisper when npu_model_dir is given) is covered by
    # test_speech.py's own NPU tests.
    received_npu_model_dir = []

    def fake_transcribe(
        source, *, language, model_size, npu_model_dir=None, force_cpu=False
    ):
        received_npu_model_dir.append(npu_model_dir)
        return Transcript(
            text="hello",
            language=language or "en",
            segments=(SpeechSegment(0.0, 1.0, "hello"),),
        )

    monkeypatch.setattr(
        bv_generate,
        "extract_audio",
        lambda source, destination: destination.write_bytes(b"audio"),
    )
    monkeypatch.setattr(bv_generate, "transcribe", fake_transcribe)

    recording = Recording(id=RecordingId("20260715_133255_N"))
    video_path = tmp_path / "20260715_133255_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )

    npu_model_dir = Path("/tmp/npu-model")
    args = _base_args(
        transcribe=True, language="en", npu_model_dir=npu_model_dir
    )

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    assert received_npu_model_dir == [npu_model_dir]


def test_transcribe_reuses_existing_audio_without_extracting(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate,
        "transcribe",
        _fake_transcribe_factory([], "sv"),
    )
    monkeypatch.setattr(
        bv_generate,
        "detect_language",
        lambda source, *, model_size, force_cpu=False: "sv",
    )

    recording = Recording(id=RecordingId("20260715_140000_N"))
    audio_path = tmp_path / "20260715_140000_N.aac"
    audio_path.write_bytes(b"a")
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=audio_path
    )

    args = _base_args(transcribe=True)

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is False


def test_transcribe_reuses_audio_already_written_this_run(
    tmp_path, monkeypatch
):
    # Simulates --extract-audio and --transcribe running together:
    # the .aac lands on disk from --extract-audio before
    # --transcribe runs, but the in-memory Recording (built once at
    # archive-load time) doesn't know about it. The on-disk check
    # must still catch it and skip re-extracting.
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    transcribed = []
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(transcribed, "sv")
    )
    monkeypatch.setattr(
        bv_generate,
        "detect_language",
        lambda source, *, model_size, force_cpu=False: "sv",
    )

    recording = Recording(id=RecordingId("20260715_150000_N"))
    video_path = tmp_path / "20260715_150000_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )
    aac_path = tmp_path / "20260715_150000_N.aac"
    aac_path.write_bytes(b"already-there")

    args = _base_args(transcribe=True)

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    assert transcribed == [aac_path]


def test_transcribe_treats_empty_cached_audio_as_absent_for_language_detection(
    tmp_path, monkeypatch
):
    """Reproduces Christer's real report: a previous ffmpeg failure
    (the ADTS/MP3 codec mismatch, since fixed) left a genuine 0-byte
    .aac tracked as this recording's Asset.AUDIO. Before this fix,
    that empty file was picked as the language-detection source
    ahead of the real video, and detect_language()/soundfile choked
    on it with a libsndfile "End of file" error - exactly what
    Christer pasted. It must fall back to the video instead, the same
    self-healing discipline load_or_compute_duration() already
    applies to .duration.txt."""

    # extract_audio() is legitimately called here too - the cached
    # .aac is empty, so the second self-healing check further down
    # (the actual transcription audio source, a separate reuse check
    # from the language-detection one) re-extracts it as well. Not
    # stubbed with _refuse, since this test is specifically about
    # confirming detect_language() gets the video, not about whether
    # extraction happens at all.
    detected_from = []
    extracted = []

    def fake_extract_audio(source, destination):
        extracted.append((source, destination))
        destination.write_bytes(b"real-audio")

    monkeypatch.setattr(bv_generate, "extract_audio", fake_extract_audio)
    monkeypatch.setattr(
        bv_generate,
        "detect_language",
        lambda source, *, model_size, force_cpu=False: (
            detected_from.append(source) or "sv"
        ),
    )
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory([], "sv")
    )

    recording = Recording(id=RecordingId("20260802_161928_N"))
    video_path = tmp_path / "20260802_161928_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )
    empty_aac = tmp_path / "20260802_161928_N.aac"
    empty_aac.write_bytes(b"")  # the real-world leftover: 0 bytes
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=empty_aac
    )

    args = _base_args(transcribe=True)

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    assert detected_from == [video_path]
    assert extracted == [(video_path, empty_aac)]


def test_transcribe_re_extracts_when_cached_audio_file_is_empty(
    tmp_path, monkeypatch
):
    """Same real-world scenario as above, but for the actual
    transcription audio source (a separate reuse check further down
    from the language-detection one) - an empty .aac already on disk
    must be overwritten by a fresh extraction, not handed to
    transcribe() as-is."""

    extracted = []

    def fake_extract_audio(source, destination):
        extracted.append((source, destination))
        destination.write_bytes(b"real-audio")

    monkeypatch.setattr(bv_generate, "extract_audio", fake_extract_audio)
    transcribed = []
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(transcribed, "sv")
    )

    recording = Recording(id=RecordingId("20260802_162029_N"))
    video_path = tmp_path / "20260802_162029_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )
    empty_aac = tmp_path / "20260802_162029_N.aac"
    empty_aac.write_bytes(b"")

    args = _base_args(transcribe=True, language="sv")

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    assert len(extracted) == 1
    assert extracted[0] == (video_path, empty_aac)
    assert transcribed == [empty_aac]
    assert empty_aac.read_bytes() == b"real-audio"


def test_transcribe_and_translate_together_still_works(
    tmp_path, monkeypatch
):
    """Regression check: --transcribe (+ optional --translate) keeps
    using its own single-Whisper-run path, unaffected by the new
    cache-first --translate-only path."""

    calls = []

    def fake_detect_language(source, *, model_size, force_cpu=False):
        return "sv"

    monkeypatch.setattr(bv_generate, "detect_language", fake_detect_language)
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(calls, "sv")
    )
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: (
            f"[{target_language}] {text}"
        ),
    )
    monkeypatch.setattr(
        bv_generate,
        "extract_audio",
        lambda source, destination: destination.write_bytes(b"audio"),
    )

    recording = Recording(id=RecordingId("20260715_133255_N"))
    video_path = tmp_path / "20260715_133255_NF.mp4"
    video_path.write_bytes(b"x")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )

    args = _base_args(transcribe=True, translate="es")

    had_error = bv_generate._do_transcribe_and_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    assert (tmp_path / "20260715_133255_N_swe.transcript.txt").exists()
    assert (tmp_path / "20260715_133255_N_spa.translation.txt").exists()


def test_parse_args_srt_requires_transcribe_or_translate():
    with pytest.raises(SystemExit):
        parse_args(["/some/path", "--extract-audio", "--srt"])


def test_parse_args_lrc_requires_transcribe_or_translate():
    with pytest.raises(SystemExit):
        parse_args(["/some/path", "--extract-audio", "--lrc"])


def test_parse_args_srt_lrc_allowed_with_transcribe():
    args = parse_args(["/some/path", "--transcribe", "--srt", "--lrc"])

    assert args.srt is True
    assert args.lrc is True


def test_transcribe_writes_srt_and_lrc_when_requested(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bv_generate,
        "extract_audio",
        lambda source, destination: destination.write_bytes(b"audio"),
    )
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory([], "sv")
    )
    monkeypatch.setattr(
        bv_generate,
        "detect_language",
        lambda source, *, model_size, force_cpu=False: "sv",
    )

    recording = Recording(id=RecordingId("20260715_200000_N"))
    video_path = tmp_path / "20260715_200000_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )

    args = _base_args(transcribe=True, srt=True, lrc=True)

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    srt_text = (tmp_path / "20260715_200000_N.srt").read_text()
    assert "00:00:00,000 --> 00:00:01,000" in srt_text
    assert "hello world" in srt_text
    lrc_text = (tmp_path / "20260715_200000_N.lrc").read_text()
    assert "[00:00.00] hello world" in lrc_text


def test_transcribe_srt_reflects_translate_language(tmp_path, monkeypatch):
    """Reproduces Christer's report: 'bv-generate --transcribe --srt
    --translate eng' produced an .srt still in the original spoken
    language (Russian, in his case) instead of English. format_srt()
    was always called with the untranslated transcript.segments, even
    when --translate was given - the docs' own example ("Transcribe
    and translate to Swedish, with subtitles") promises otherwise."""

    monkeypatch.setattr(
        bv_generate,
        "extract_audio",
        lambda source, destination: destination.write_bytes(b"audio"),
    )
    monkeypatch.setattr(
        bv_generate,
        "detect_language",
        lambda source, *, model_size, force_cpu=False: "ru",
    )
    monkeypatch.setattr(
        bv_generate,
        "transcribe",
        lambda source, *, language=None, model_size="small", npu_model_dir=None, force_cpu=False: Transcript(
            text="Привет мир",
            language="ru",
            segments=(
                SpeechSegment(0.0, 1.0, "Привет"),
                SpeechSegment(1.0, 2.0, "мир"),
            ),
        ),
    )

    translated = {"Привет": "Hello", "мир": "world", "Привет мир": "Hello world"}
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: translated[text],
    )

    recording = Recording(id=RecordingId("20260802_161928_N"))
    video_path = tmp_path / "20260802_161928_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )

    args = _base_args(transcribe=True, srt=True, translate="es")

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    srt_text = (tmp_path / "20260802_161928_N.srt").read_text(
        encoding="utf-8"
    )
    assert "Hello" in srt_text
    assert "world" in srt_text
    assert "Привет" not in srt_text
    # The plain-text transcript is unaffected - still the original
    # spoken language, only the subtitles change.
    assert (
        "Привет мир"
        in (tmp_path / "20260802_161928_N_rus.transcript.txt").read_text(
            encoding="utf-8"
        )
    )


def test_transcribe_srt_stays_original_language_without_translate(
    tmp_path, monkeypatch
):
    """Regression check: no --translate means --srt keeps behaving
    exactly as before - original spoken language, translate() never
    even called."""

    monkeypatch.setattr(
        bv_generate,
        "extract_audio",
        lambda source, destination: destination.write_bytes(b"audio"),
    )
    monkeypatch.setattr(
        bv_generate,
        "detect_language",
        lambda source, *, model_size, force_cpu=False: "ru",
    )
    monkeypatch.setattr(
        bv_generate,
        "transcribe",
        lambda source, *, language=None, model_size="small", npu_model_dir=None, force_cpu=False: Transcript(
            text="Привет мир",
            language="ru",
            segments=(SpeechSegment(0.0, 1.0, "Привет мир"),),
        ),
    )
    monkeypatch.setattr(bv_generate, "translate", _refuse)

    recording = Recording(id=RecordingId("20260802_161928_N"))
    video_path = tmp_path / "20260802_161928_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )

    args = _base_args(transcribe=True, srt=True)

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    srt_text = (tmp_path / "20260802_161928_N.srt").read_text(
        encoding="utf-8"
    )
    assert "Привет мир" in srt_text


def test_transcribe_srt_translation_failure_skips_srt_and_reports_error(
    tmp_path, monkeypatch
):
    """A failed segment translation (e.g. missing argos-translate
    language pack) must not silently fall back to writing an
    untranslated .srt - that would look like a success while quietly
    giving the user the wrong language again."""

    monkeypatch.setattr(
        bv_generate,
        "extract_audio",
        lambda source, destination: destination.write_bytes(b"audio"),
    )
    monkeypatch.setattr(
        bv_generate,
        "detect_language",
        lambda source, *, model_size, force_cpu=False: "ru",
    )
    monkeypatch.setattr(
        bv_generate,
        "transcribe",
        lambda source, *, language=None, model_size="small", npu_model_dir=None, force_cpu=False: Transcript(
            text="Привет мир",
            language="ru",
            segments=(SpeechSegment(0.0, 1.0, "Привет мир"),),
        ),
    )

    def failing_translate(text, *, source_language, target_language):
        raise MediaToolError("no argos-translate language installed")

    monkeypatch.setattr(bv_generate, "translate", failing_translate)

    recording = Recording(id=RecordingId("20260802_161928_N"))
    video_path = tmp_path / "20260802_161928_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )

    args = _base_args(transcribe=True, srt=True, translate="es")

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is True
    assert not (tmp_path / "20260802_161928_N.srt").exists()


def test_translate_only_srt_reflects_translate_language(tmp_path, monkeypatch):
    """Same fix, the _do_translate_only path (--translate without
    --transcribe) - this function only ever runs with --translate
    given, so its .srt/.lrc must always be in the target language."""

    monkeypatch.setattr(
        bv_generate,
        "extract_audio",
        lambda source, destination: destination.write_bytes(b"audio"),
    )
    monkeypatch.setattr(
        bv_generate,
        "transcribe",
        lambda source, *, language=None, model_size="small", npu_model_dir=None, force_cpu=False: Transcript(
            text="Привет мир",
            language="ru",
            segments=(
                SpeechSegment(0.0, 1.0, "Привет"),
                SpeechSegment(1.0, 2.0, "мир"),
            ),
        ),
    )

    translated = {"Привет": "Hello", "мир": "world", "Привет мир": "Hello world"}
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: translated[text],
    )

    recording = Recording(id=RecordingId("20260715_140000_N"))
    video_path = tmp_path / "20260715_140000_NF.mp4"
    video_path.write_bytes(b"v")
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=video_path
    )

    args = _base_args(translate="es", srt=True)

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    srt_text = (tmp_path / "20260715_140000_N.srt").read_text(
        encoding="utf-8"
    )
    assert "Hello" in srt_text
    assert "world" in srt_text
    assert "Привет" not in srt_text


def test_transcribe_srt_only_still_transcribes_when_transcript_up_to_date(
    tmp_path, monkeypatch
):
    # Even if the transcript file itself is already there and doesn't
    # need rewriting, a missing .srt still has to trigger a fresh
    # transcribe() call - there's nowhere else to get segment timing.
    transcribed = []
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(transcribed, "sv")
    )
    monkeypatch.setattr(
        bv_generate,
        "detect_language",
        lambda source, *, model_size, force_cpu=False: "sv",
    )

    recording = Recording(id=RecordingId("20260715_210000_N"))
    audio_path = tmp_path / "20260715_210000_N.aac"
    audio_path.write_bytes(b"a")
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=audio_path
    )
    transcript_path = tmp_path / "20260715_210000_N_swe.transcript.txt"
    transcript_path.write_text("already here")
    recording.assets[Asset.TRANSCRIPT] = AssetFile(
        asset=Asset.TRANSCRIPT, path=transcript_path
    )

    args = _base_args(transcribe=True, srt=True, language="sv")

    had_error = bv_generate._do_transcribe_with_optional_translate(
        recording, tmp_path, args
    )

    assert had_error is False
    assert transcribed == [audio_path]
    assert (tmp_path / "20260715_210000_N.srt").exists()
    # The already-current transcript file itself is left untouched.
    assert transcript_path.read_text() == "already here"


def test_translate_only_writes_srt_lrc_on_fresh_transcribe(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory([], "th")
    )
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: text.upper(),
    )

    recording = Recording(id=RecordingId("20260715_220000_N"))
    audio_path = tmp_path / "20260715_220000_N.aac"
    audio_path.write_bytes(b"a")
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=audio_path
    )

    args = _base_args(translate="sv", srt=True, lrc=True)

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    assert (tmp_path / "20260715_220000_N.srt").exists()
    assert (tmp_path / "20260715_220000_N.lrc").exists()


def test_translate_only_srt_lrc_forces_a_fresh_transcribe_over_the_cache(
    tmp_path, monkeypatch
):
    # A cached plain-text transcript has no segment timing, so --srt/
    # --lrc can't be satisfied by the normal cache-first --translate
    # path. Reported by Christer against a real archive: --translate
    # --srt --lrc silently produced no subtitles when a transcript
    # already existed. Fixed by bypassing the transcript-reuse cache
    # entirely whenever --srt/--lrc are requested, so Whisper always
    # runs and there's real segment timing to draw from.
    calls = []
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(calls)
    )
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: text.upper(),
    )

    recording = Recording(id=RecordingId("20260715_230000_N"))
    transcript_path = tmp_path / "20260715_230000_N_tha.transcript.txt"
    transcript_path.write_text("stale cached text")
    recording.assets[Asset.TRANSCRIPT] = AssetFile(
        asset=Asset.TRANSCRIPT, path=transcript_path
    )
    audio_path = tmp_path / "20260715_230000_N.aac"
    audio_path.write_bytes(b"a")
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=audio_path
    )

    args = _base_args(translate="sv", srt=True, lrc=True)

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    assert calls == [audio_path], "should re-transcribe, not reuse the cache"
    assert (tmp_path / "20260715_230000_N.srt").exists()
    assert (tmp_path / "20260715_230000_N.lrc").exists()


def test_translate_only_without_srt_lrc_still_reuses_cached_transcript(
    tmp_path, monkeypatch
):
    # Regression check: plain --translate (no --srt/--lrc) keeps the
    # original cache-first behaviour - the fix above only bypasses the
    # cache when subtitle timing is actually needed.
    monkeypatch.setattr(bv_generate, "transcribe", _refuse)
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: (
            f"[{source_language}->{target_language}] {text}"
        ),
    )

    recording = Recording(id=RecordingId("20260715_231500_N"))
    transcript_path = tmp_path / "20260715_231500_N_tha.transcript.txt"
    transcript_path.write_text("hej da")
    recording.assets[Asset.TRANSCRIPT] = AssetFile(
        asset=Asset.TRANSCRIPT, path=transcript_path
    )

    args = _base_args(translate="sv")

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    out = tmp_path / "20260715_231500_N_swe.translation.txt"
    assert out.read_text().strip() == "[th->sv] hej da"


def test_translate_only_srt_lrc_still_generated_when_translation_already_exists(
    tmp_path, monkeypatch
):
    # The exact scenario Christer hit: he'd already run --translate
    # once (translation.txt exists on disk), then re-ran with --srt
    # --lrc added. The old code gated the *entire* function on
    # translation.txt's own _should_write check - since translation.txt
    # already existed and --overwrite wasn't passed, the function
    # returned before ever reaching the srt/lrc-writing code, so
    # nothing was generated even after the cache-bypass fix above.
    # need_srt_write/need_lrc_write must now be checked independently.
    #
    # Also covers a second, related fix: translate() *is* now called
    # here (unlike before) - the SRT/LRC need their own translated
    # segments regardless of whether translation.txt itself needs
    # rewriting, since format_srt()/format_lrc() previously always
    # used the *untranslated* segments even under --translate. Only
    # translation.txt's own write is skipped (already up to date).
    calls = []
    monkeypatch.setattr(
        bv_generate, "transcribe", _fake_transcribe_factory(calls)
    )
    monkeypatch.setattr(bv_generate, "extract_audio", _refuse)
    monkeypatch.setattr(
        bv_generate,
        "translate",
        lambda text, *, source_language, target_language: text.upper(),
    )

    recording = Recording(id=RecordingId("20260715_233000_N"))
    transcript_path = tmp_path / "20260715_233000_N_tha.transcript.txt"
    transcript_path.write_text("stale cached text")
    recording.assets[Asset.TRANSCRIPT] = AssetFile(
        asset=Asset.TRANSCRIPT, path=transcript_path
    )
    audio_path = tmp_path / "20260715_233000_N.aac"
    audio_path.write_bytes(b"a")
    recording.assets[Asset.AUDIO] = AssetFile(
        asset=Asset.AUDIO, path=audio_path
    )

    translation_path = tmp_path / "20260715_233000_N_swe.translation.txt"
    translation_path.write_text("already translated, from an earlier run")

    args = _base_args(translate="sv", srt=True, lrc=True)

    had_error = bv_generate._do_translate_only(recording, tmp_path, args)

    assert had_error is False
    assert calls == [audio_path], "should still re-transcribe for segment timing"
    srt_text = (tmp_path / "20260715_233000_N.srt").read_text(
        encoding="utf-8"
    )
    lrc_text = (tmp_path / "20260715_233000_N.lrc").read_text(
        encoding="utf-8"
    )
    assert "HELLO WORLD" in srt_text
    assert "HELLO WORLD" in lrc_text
    # translation.txt itself was already up to date and --overwrite
    # wasn't given, so it should be left untouched.
    assert (
        translation_path.read_text() == "already translated, from an earlier run"
    )


def test_run_reports_no_recordings_found_for_an_empty_archive(
    tmp_path, capsys
):
    args = parse_args([str(tmp_path), "--extract-audio"])

    exit_code = bv_generate._run(args)

    out = capsys.readouterr().out

    assert exit_code == bv_generate.EXIT_OK
    assert "no recordings found" in out
    assert str(tmp_path) in out


def test_overwrite_decision_asks_once_and_caches_the_answer(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    decision = bv_generate._OverwriteDecision()

    first = decision(Path("a.txt"))
    second = decision(Path("b.txt"))

    assert first is True
    assert second is True


def test_overwrite_decision_only_calls_input_once(monkeypatch):
    calls = []

    def fake_input(prompt):
        calls.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", fake_input)

    decision = bv_generate._OverwriteDecision()
    decision(Path("a.txt"))
    decision(Path("b.txt"))
    decision(Path("c.txt"))

    assert len(calls) == 1


def test_should_write_for_shares_one_overwrite_decision_across_calls(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bv_generate, "_interactive", lambda: True)

    calls = []

    def fake_input(prompt):
        calls.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)

    first_file = tmp_path / "first.txt"
    first_file.write_text("existing")
    second_file = tmp_path / "second.txt"
    second_file.write_text("existing")

    args = _base_args()
    args.overwrite_decision = bv_generate._OverwriteDecision()

    first_result = bv_generate._should_write_for(first_file, args)
    second_result = bv_generate._should_write_for(second_file, args)

    assert first_result is True
    assert second_result is True
    assert len(calls) == 1, "should only prompt once for the whole run"


def test_should_write_for_falls_back_to_asking_every_time_without_a_decision(
    tmp_path, monkeypatch
):
    # No args.overwrite_decision set (e.g. a direct call site that
    # predates the shared-decision mechanism) - falls back to the
    # old per-call prompt behaviour rather than crashing.
    monkeypatch.setattr(bv_generate, "_interactive", lambda: True)

    calls = []

    def fake_input(prompt):
        calls.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)

    a_file = tmp_path / "a.txt"
    a_file.write_text("existing")
    b_file = tmp_path / "b.txt"
    b_file.write_text("existing")

    args = _base_args()

    bv_generate._should_write_for(a_file, args)
    bv_generate._should_write_for(b_file, args)

    assert len(calls) == 2


def test_parse_args_describe_scene_is_a_valid_action_by_itself():
    args = parse_args(["/some/path", "--describe-scene"])

    assert args.describe_scene is True
    assert args.scene_model == SCENE_DEFAULT_MODEL


def test_parse_args_scene_model_override():
    args = parse_args([
        "/some/path", "--describe-scene", "--scene-model", "Qwen/Qwen3-VL-8B-Instruct",
    ])

    assert args.scene_model == "Qwen/Qwen3-VL-8B-Instruct"


def test_do_describe_scene_skips_parking_mode_ok(monkeypatch, tmp_path):
    # Unlike the audio actions, describe-scene does NOT skip Parking
    # recordings - they're still video. select_source() returning a
    # real file (even for a P-mode recording) should proceed normally.
    calls = []

    def fake_describe_scene(source, *, model, force_cpu):
        calls.append((source, model, force_cpu))
        return "## Description\nParked, nothing notable.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_generate, "describe_scene", fake_describe_scene)

    recording = Recording(id=RecordingId("20260715_134010_P"))
    video = tmp_path / "20260715_134010_PF.mp4"
    video.write_bytes(b"x")
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=video)

    args = _base_args(describe_scene=True, cpu=True)

    had_error = bv_generate._do_describe_scene(recording, tmp_path, args)

    assert had_error is False
    assert calls == [(video, SCENE_DEFAULT_MODEL, True)]
    written = (tmp_path / "20260715_134010_P.scene.txt").read_text(encoding="utf-8")
    assert "Parked, nothing notable." in written


def test_do_describe_scene_no_source_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(bv_generate, "select_source", lambda recording: None)

    recording = Recording(id=RecordingId("20260715_134010_N"))
    args = _base_args(describe_scene=True)

    had_error = bv_generate._do_describe_scene(recording, tmp_path, args)

    assert had_error is True
    assert not (tmp_path / "20260715_134010_N.scene.txt").exists()


def test_do_describe_scene_dry_run_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(bv_generate, "describe_scene", _refuse)

    recording = Recording(id=RecordingId("20260715_134010_N"))
    video = tmp_path / "20260715_134010_NF.mp4"
    video.write_bytes(b"x")
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=video)

    args = _base_args(describe_scene=True, dry_run=True)

    had_error = bv_generate._do_describe_scene(recording, tmp_path, args)

    assert had_error is False
    assert not (tmp_path / "20260715_134010_N.scene.txt").exists()


def test_do_describe_scene_propagates_media_tool_error(monkeypatch, tmp_path):
    def fake_describe_scene(source, *, model, force_cpu):
        raise MediaToolError("out of VRAM")

    monkeypatch.setattr(bv_generate, "describe_scene", fake_describe_scene)

    recording = Recording(id=RecordingId("20260715_134010_N"))
    video = tmp_path / "20260715_134010_NF.mp4"
    video.write_bytes(b"x")
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=video)

    args = _base_args(describe_scene=True)

    had_error = bv_generate._do_describe_scene(recording, tmp_path, args)

    assert had_error is True
    assert not (tmp_path / "20260715_134010_N.scene.txt").exists()


def test_parse_args_camera_defaults_to_front():
    args = parse_args(["/some/path", "--describe-scene"])

    assert args.camera == "front"


def test_parse_args_camera_accepts_rear_and_both():
    assert parse_args(
        ["/some/path", "--describe-scene", "--camera", "rear"]
    ).camera == "rear"
    assert parse_args(
        ["/some/path", "--describe-scene", "--camera", "both"]
    ).camera == "both"


def test_parse_args_camera_rejects_invalid_choice():
    with pytest.raises(SystemExit):
        parse_args(["/some/path", "--describe-scene", "--camera", "sideways"])


def _make_front_rear_recording(recording_id: str, tmp_path: Path, *, front=True, rear=True):
    recording = Recording(id=RecordingId(recording_id))
    if front:
        front_video = tmp_path / f"{recording_id}F.mp4"
        front_video.write_bytes(b"x")
        recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=front_video)
    if rear:
        rear_video = tmp_path / f"{recording_id}R.mp4"
        rear_video.write_bytes(b"x")
        recording.assets[Asset.REAR] = AssetFile(asset=Asset.REAR, path=rear_video)
    return recording


def test_do_describe_scene_camera_front_is_unchanged_default(monkeypatch, tmp_path):
    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append((source, kwargs))
        return "## Description\nFront view.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_generate, "describe_scene", fake_describe_scene)

    recording = _make_front_rear_recording("20260715_134010_N", tmp_path)
    args = _base_args(describe_scene=True, camera="front")

    had_error = bv_generate._do_describe_scene(recording, tmp_path, args)

    assert had_error is False
    assert len(calls) == 1
    assert "task" not in calls[0][1]
    assert (tmp_path / "20260715_134010_N.scene.txt").exists()
    assert not (tmp_path / "20260715_134010_N.rear.scene.txt").exists()


def test_do_describe_scene_camera_rear_only(monkeypatch, tmp_path):
    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append((source, kwargs))
        return "## Description\nRear view.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_generate, "describe_scene", fake_describe_scene)

    recording = _make_front_rear_recording("20260715_134010_N", tmp_path)
    args = _base_args(describe_scene=True, camera="rear")

    had_error = bv_generate._do_describe_scene(recording, tmp_path, args)

    assert had_error is False
    assert len(calls) == 1
    # Rear-only is a deliberate choice - it gets the normal full task,
    # not an OCR-only bonus pass.
    assert "task" not in calls[0][1]
    assert not (tmp_path / "20260715_134010_N.scene.txt").exists()
    assert (tmp_path / "20260715_134010_N.rear.scene.txt").exists()


def test_do_describe_scene_camera_rear_errors_without_rear_video(monkeypatch, tmp_path):
    monkeypatch.setattr(bv_generate, "describe_scene", _refuse)

    recording = _make_front_rear_recording("20260715_134010_N", tmp_path, rear=False)
    args = _base_args(describe_scene=True, camera="rear")

    had_error = bv_generate._do_describe_scene(recording, tmp_path, args)

    assert had_error is True
    assert not (tmp_path / "20260715_134010_N.rear.scene.txt").exists()


def test_do_describe_scene_camera_both_writes_front_and_rear_bonus(monkeypatch, tmp_path):
    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append((source, kwargs))
        return "## Description\nSome view.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_generate, "describe_scene", fake_describe_scene)

    recording = _make_front_rear_recording("20260715_134010_N", tmp_path)
    args = _base_args(describe_scene=True, camera="both")

    had_error = bv_generate._do_describe_scene(recording, tmp_path, args)

    assert had_error is False
    assert len(calls) == 2
    assert "task" not in calls[0][1]
    assert calls[1][1]["task"] == "ocr"
    assert (tmp_path / "20260715_134010_N.scene.txt").exists()
    assert (tmp_path / "20260715_134010_N.rear.scene.txt").exists()


def test_do_describe_scene_camera_both_skips_bonus_without_distinct_rear(monkeypatch, tmp_path):
    # No front video at all - the front pass already used the rear
    # video as its own fallback, so a second pass on the same file
    # under a different name would just duplicate that work.
    calls = []

    def fake_describe_scene(source, **kwargs):
        calls.append((source, kwargs))
        return "## Description\nSome view.\n\n---\ndisclaimer"

    monkeypatch.setattr(bv_generate, "describe_scene", fake_describe_scene)

    recording = _make_front_rear_recording("20260715_134010_N", tmp_path, front=False)
    args = _base_args(describe_scene=True, camera="both")

    had_error = bv_generate._do_describe_scene(recording, tmp_path, args)

    assert had_error is False
    assert len(calls) == 1
    assert (tmp_path / "20260715_134010_N.scene.txt").exists()
    assert not (tmp_path / "20260715_134010_N.rear.scene.txt").exists()
