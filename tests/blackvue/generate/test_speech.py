import os
import sys
import types

from blackvue.generate.media import MediaToolError
from blackvue.generate.speech import DIARIZATION_MODEL
from blackvue.generate.speech import SEGMENTATION_MODEL
from blackvue.generate.speech import SpeakerTurn
from blackvue.generate.speech import SpeechSegment
from blackvue.generate.speech import _get_diarization_pipeline
from blackvue.generate.speech import format_diarized_transcript


def test_format_diarized_transcript_groups_consecutive_same_speaker():
    segments = (
        SpeechSegment(start=0.0, end=1.0, text="Hello,"),
        SpeechSegment(start=1.0, end=2.0, text="how's the drive going?"),
        SpeechSegment(start=2.5, end=3.5, text="Not bad,"),
        SpeechSegment(start=3.5, end=4.5, text="traffic's light today."),
    )
    turns = (
        SpeakerTurn(start=0.0, end=2.0, speaker="SPEAKER_00"),
        SpeakerTurn(start=2.0, end=5.0, speaker="SPEAKER_01"),
    )

    result = format_diarized_transcript(segments, turns)

    assert result == (
        "[SPEAKER_00] Hello, how's the drive going?\n"
        "[SPEAKER_01] Not bad, traffic's light today."
    )


def test_format_diarized_transcript_splits_on_speaker_change():
    segments = (
        SpeechSegment(start=0.0, end=1.0, text="One."),
        SpeechSegment(start=1.0, end=2.0, text="Two."),
        SpeechSegment(start=2.0, end=3.0, text="Three."),
    )
    turns = (
        SpeakerTurn(start=0.0, end=1.0, speaker="SPEAKER_00"),
        SpeakerTurn(start=1.0, end=2.0, speaker="SPEAKER_01"),
        SpeakerTurn(start=2.0, end=3.0, speaker="SPEAKER_00"),
    )

    result = format_diarized_transcript(segments, turns)

    assert result == (
        "[SPEAKER_00] One.\n[SPEAKER_01] Two.\n[SPEAKER_00] Three."
    )


def test_format_diarized_transcript_falls_back_to_best_overlap():
    # The segment's midpoint (2.0) falls in the gap between the two
    # turns, so neither directly contains it - the turn with the
    # larger overlap against the whole segment wins.
    segments = (SpeechSegment(start=0.0, end=4.0, text="Hi."),)
    turns = (
        SpeakerTurn(start=-1.0, end=1.5, speaker="SPEAKER_00"),
        SpeakerTurn(start=3.5, end=10.0, speaker="SPEAKER_01"),
    )

    result = format_diarized_transcript(segments, turns)

    assert result == "[SPEAKER_00] Hi."


def test_format_diarized_transcript_labels_unknown_without_turns():
    segments = (SpeechSegment(start=0.0, end=1.0, text="Hi."),)

    result = format_diarized_transcript(segments, turns=())

    assert result == "[UNKNOWN] Hi."


def _install_fake_pyannote_audio(monkeypatch, pipeline_factory=None):
    """Inject a fake pyannote.audio module so _get_diarization_pipeline's
    lazy `from pyannote.audio import Pipeline` succeeds, letting tests
    reach the token-checking logic instead of the ImportError branch."""

    class _FakePipeline:
        @staticmethod
        def from_pretrained(model, use_auth_token):
            if pipeline_factory is None:
                raise AssertionError(
                    "from_pretrained should not be called here"
                )
            return pipeline_factory(model, use_auth_token)

    pyannote_pkg = types.ModuleType("pyannote")
    audio_module = types.ModuleType("pyannote.audio")
    audio_module.Pipeline = _FakePipeline
    pyannote_pkg.audio = audio_module

    monkeypatch.setitem(sys.modules, "pyannote", pyannote_pkg)
    monkeypatch.setitem(sys.modules, "pyannote.audio", audio_module)


def test_missing_token_message_explains_how_to_get_and_apply_one(
    monkeypatch,
):
    _install_fake_pyannote_audio(monkeypatch)
    monkeypatch.delitem(os.environ, "HF_TOKEN", raising=False)
    monkeypatch.delitem(os.environ, "HUGGINGFACE_TOKEN", raising=False)

    try:
        _get_diarization_pipeline(None)
        raised = False
        message = ""
    except MediaToolError as exc:
        raised = True
        message = str(exc)

    assert raised is True
    # How to get a token.
    assert "https://huggingface.co/settings/tokens" in message
    # How to unlock it (both gated models pyannote 3.1 depends on).
    assert f"https://huggingface.co/{DIARIZATION_MODEL}" in message
    assert f"https://huggingface.co/{SEGMENTATION_MODEL}" in message
    # How to apply it.
    assert "--hf-token" in message
    assert "HF_TOKEN" in message


def test_pipeline_load_failure_also_points_at_both_model_licenses(
    monkeypatch,
):
    def failing_from_pretrained(model, use_auth_token):
        raise RuntimeError("401 Client Error: gated repo")

    _install_fake_pyannote_audio(monkeypatch, failing_from_pretrained)

    try:
        _get_diarization_pipeline("a-real-looking-token")
        raised = False
        message = ""
    except MediaToolError as exc:
        raised = True
        message = str(exc)

    assert raised is True
    assert "401 Client Error: gated repo" in message
    assert f"https://huggingface.co/{DIARIZATION_MODEL}" in message
    assert f"https://huggingface.co/{SEGMENTATION_MODEL}" in message


def test_valid_token_loads_pipeline_without_error(monkeypatch):
    loaded = []

    def fake_from_pretrained(model, use_auth_token):
        loaded.append((model, use_auth_token))
        return "a-pipeline"

    _install_fake_pyannote_audio(monkeypatch, fake_from_pretrained)

    # Clear the module-level cache so this test doesn't see a pipeline
    # left behind by an earlier test using the same cache key.
    import blackvue.generate.speech as speech_module

    speech_module._DIARIZATION_PIPELINE_CACHE.clear()

    pipeline = _get_diarization_pipeline("a-real-looking-token")

    assert pipeline == "a-pipeline"
    assert loaded == [(DIARIZATION_MODEL, "a-real-looking-token")]
