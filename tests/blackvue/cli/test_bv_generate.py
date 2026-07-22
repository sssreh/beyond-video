import argparse

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
from blackvue.cli.bv_generate import parse_args
from blackvue.generate.speech import SpeakerTurn
from blackvue.generate.speech import SpeechSegment
from blackvue.generate.speech import Transcript


def _base_args(**overrides):
    defaults = dict(
        extract_audio=False,
        get_duration=False,
        transcribe=False,
        translate=None,
        language=None,
        model_size="small",
        diarize=False,
        hf_token=None,
        srt=False,
        lrc=False,
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

    def fake_transcribe(source, *, language=None, model_size="small"):
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
    def fake_transcribe(source, *, language, model_size):
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

    def fake_transcribe(source, *, language, model_size):
        transcribed.append(source)
        return Transcript(
            text="hej da",
            language=language or "sv",
            segments=(SpeechSegment(0.0, 1.0, "hej da"),),
        )

    monkeypatch.setattr(bv_generate, "extract_audio", fake_extract_audio)
    monkeypatch.setattr(bv_generate, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        bv_generate, "detect_language", lambda source, *, model_size: "sv"
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
        bv_generate, "detect_language", lambda source, *, model_size: "sv"
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
        bv_generate, "detect_language", lambda source, *, model_size: "sv"
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


def test_transcribe_and_translate_together_still_works(
    tmp_path, monkeypatch
):
    """Regression check: --transcribe (+ optional --translate) keeps
    using its own single-Whisper-run path, unaffected by the new
    cache-first --translate-only path."""

    calls = []

    def fake_detect_language(source, *, model_size):
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
        bv_generate, "detect_language", lambda source, *, model_size: "sv"
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
        bv_generate, "detect_language", lambda source, *, model_size: "sv"
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
