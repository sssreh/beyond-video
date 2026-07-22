import os
import subprocess
import sys
import types
from pathlib import Path

from blackvue.generate.media import MediaToolError
from blackvue.generate.speech import DEPENDENT_MODELS
from blackvue.generate.speech import DIARIZATION_MODEL
from blackvue.generate.speech import SpeakerTurn
from blackvue.generate.speech import SpeechSegment
from blackvue.generate.speech import _get_diarization_pipeline
from blackvue.generate.speech import diarize
from blackvue.generate.speech import format_diarized_transcript
from blackvue.generate.speech import transcribe


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


class _FakeRawSegment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class _FakeTranscriptionInfo:
    def __init__(self, language, duration):
        self.language = language
        self.duration = duration


class _FakeWhisperModel:
    def __init__(self, raw_segments, info):
        self._raw_segments = raw_segments
        self._info = info

    def transcribe(self, source, language=None):
        return self._raw_segments, self._info


def test_transcribe_clamps_segment_timestamps_to_the_real_audio_duration(
    monkeypatch,
):
    # faster-whisper decodes in fixed-size chunks internally, so a
    # segment's end (or even start) timestamp can land past the
    # audio's real length (info.duration, measured from the same
    # decode) - this is what showed up as Christer's merged trip.srt
    # running a couple of seconds longer than the actual video and
    # trip.lrc: the last cue's end (121.5s) overran the real 120.0s of
    # audio.
    raw_segments = (
        _FakeRawSegment(0.0, 5.0, "hello"),
        _FakeRawSegment(118.0, 121.5, "goodbye"),
    )
    info = _FakeTranscriptionInfo(language="en", duration=120.0)

    import blackvue.generate.speech as speech_module

    monkeypatch.setattr(
        speech_module,
        "_get_whisper_model",
        lambda model_size: _FakeWhisperModel(raw_segments, info),
    )

    transcript = transcribe(Path("/tmp/audio.aac"))

    assert transcript.segments == (
        SpeechSegment(start=0.0, end=5.0, text="hello"),
        SpeechSegment(start=118.0, end=120.0, text="goodbye"),
    )


class _FakeCudaAwareWhisperModel:
    """Stands in for faster_whisper.WhisperModel: records every
    construction call, and optionally raises for device="cuda" to
    simulate a machine with no usable GPU (no NVIDIA card, driver too
    old, missing cuDNN/cuBLAS, ...) - the exact exception type varies
    in practice, so this uses a plain RuntimeError as a representative
    stand-in.
    """

    calls: list[tuple[str, str, str]] = []
    cuda_should_fail = True

    def __init__(self, model_size, device, compute_type):
        type(self).calls.append((model_size, device, compute_type))
        if device == "cuda" and type(self).cuda_should_fail:
            raise RuntimeError("no CUDA-capable device found")
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type


def _install_fake_faster_whisper(monkeypatch, *, cuda_should_fail):
    _FakeCudaAwareWhisperModel.calls = []
    _FakeCudaAwareWhisperModel.cuda_should_fail = cuda_should_fail

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _FakeCudaAwareWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)


def test_load_whisper_model_falls_back_to_cpu_when_cuda_is_unavailable(
    monkeypatch,
):
    import blackvue.generate.speech as speech_module

    _install_fake_faster_whisper(monkeypatch, cuda_should_fail=True)

    model = speech_module._load_whisper_model("small")

    assert model.device == "cpu"
    assert model.compute_type == "int8"
    assert _FakeCudaAwareWhisperModel.calls == [
        ("small", "cuda", "float16"),
        ("small", "cpu", "int8"),
    ]


def test_load_whisper_model_uses_cuda_when_available(monkeypatch):
    import blackvue.generate.speech as speech_module

    _install_fake_faster_whisper(monkeypatch, cuda_should_fail=False)

    model = speech_module._load_whisper_model("small")

    assert model.device == "cuda"
    assert model.compute_type == "float16"
    # No CPU fallback attempted - the first (GPU) load already worked.
    assert _FakeCudaAwareWhisperModel.calls == [("small", "cuda", "float16")]


def test_get_whisper_model_reports_missing_faster_whisper_cleanly():
    # Genuine, unmocked condition in this sandbox: faster-whisper isn't
    # installed (matches the pattern _load_waveform_via_ffmpeg's own
    # missing-torch test uses below). A cache key unique to this test
    # avoids colliding with any other test's cached model under the
    # same _WHISPER_MODEL_CACHE.
    import blackvue.generate.speech as speech_module

    try:
        speech_module._get_whisper_model("_real-missing-dependency-check")
        raised = False
        message = ""
    except MediaToolError as exc:
        raised = True
        message = str(exc)

    assert raised is True
    assert "faster-whisper" in message
    assert "pip install faster-whisper" in message


def _install_fake_pyannote_audio(
    monkeypatch, pipeline_factory=None, *, legacy_kwarg=True
):
    """Inject a fake pyannote.audio module so _get_diarization_pipeline's
    lazy `from pyannote.audio import Pipeline` succeeds, letting tests
    reach the token-checking logic instead of the ImportError branch.

    legacy_kwarg=True (the default) simulates an older pyannote.audio
    that only accepts `from_pretrained(model, use_auth_token=...)` -
    this is what exercises _load_pipeline's fallback path, since
    beyond-video's code tries the newer `token=` keyword first.
    legacy_kwarg=False simulates a current install that only accepts
    `token=...`, matching the real-world error this was written for
    ("unexpected keyword argument 'use_auth_token'" no longer applies
    - now it would be the reverse if we hadn't fixed it).
    """

    class _FakePipeline:
        call_count = 0

        @staticmethod
        def from_pretrained(model, **kwargs):
            _FakePipeline.call_count += 1

            expected_kwarg = "use_auth_token" if legacy_kwarg else "token"

            if list(kwargs) != [expected_kwarg]:
                raise TypeError(
                    f"from_pretrained() got an unexpected keyword "
                    f"argument {next(iter(kwargs))!r}"
                )

            if pipeline_factory is None:
                raise AssertionError(
                    "from_pretrained should not be called here"
                )

            return pipeline_factory(model, kwargs[expected_kwarg])

    pyannote_pkg = types.ModuleType("pyannote")
    audio_module = types.ModuleType("pyannote.audio")
    audio_module.Pipeline = _FakePipeline
    pyannote_pkg.audio = audio_module

    monkeypatch.setitem(sys.modules, "pyannote", pyannote_pkg)
    monkeypatch.setitem(sys.modules, "pyannote.audio", audio_module)

    return _FakePipeline


def _clear_diarization_pipeline_cache():
    # Tests share blackvue.generate.speech's module-level pipeline
    # cache (keyed by hf_token). Clear it before each test that calls
    # _get_diarization_pipeline() so an earlier test's cached result
    # can't be returned instead of exercising this test's fakes.
    import blackvue.generate.speech as speech_module

    speech_module._DIARIZATION_PIPELINE_CACHE.clear()


def test_missing_token_message_explains_how_to_get_and_apply_one(
    monkeypatch,
):
    _install_fake_pyannote_audio(monkeypatch)
    _clear_diarization_pipeline_cache()
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
    # How to unlock it - the main model plus every known dependency.
    assert f"https://huggingface.co/{DIARIZATION_MODEL}" in message
    for model in DEPENDENT_MODELS:
        assert f"https://huggingface.co/{model}" in message
    # How to apply it.
    assert "--hf-token" in message
    assert "HF_TOKEN" in message


def test_pipeline_load_failure_also_points_at_known_model_licenses(
    monkeypatch,
):
    def failing_from_pretrained(model, use_auth_token):
        raise RuntimeError("401 Client Error: gated repo")

    _install_fake_pyannote_audio(monkeypatch, failing_from_pretrained)
    _clear_diarization_pipeline_cache()

    try:
        _get_diarization_pipeline("token-for-load-failure-test")
        raised = False
        message = ""
    except MediaToolError as exc:
        raised = True
        message = str(exc)

    assert raised is True
    assert "401 Client Error: gated repo" in message
    assert f"https://huggingface.co/{DIARIZATION_MODEL}" in message
    for model in DEPENDENT_MODELS:
        assert f"https://huggingface.co/{model}" in message


def test_valid_token_loads_pipeline_without_error(monkeypatch):
    # legacy_kwarg=True: the installed pyannote.audio only accepts
    # use_auth_token=, so this exercises _load_pipeline's fallback -
    # the first attempt (token=) fails with TypeError, the second
    # (use_auth_token=) succeeds.
    loaded = []

    def fake_from_pretrained(model, use_auth_token):
        loaded.append((model, use_auth_token))
        return "a-pipeline"

    fake_pipeline_class = _install_fake_pyannote_audio(
        monkeypatch, fake_from_pretrained, legacy_kwarg=True
    )
    _clear_diarization_pipeline_cache()

    pipeline = _get_diarization_pipeline("token-for-legacy-fallback-test")

    assert pipeline == "a-pipeline"
    assert loaded == [(DIARIZATION_MODEL, "token-for-legacy-fallback-test")]
    assert fake_pipeline_class.call_count == 2  # token= failed, then fell back


def test_load_pipeline_uses_new_token_keyword_when_supported(monkeypatch):
    # legacy_kwarg=False: this is the real-world case the bug report
    # was about - a current pyannote.audio install that only accepts
    # token=, and no longer accepts use_auth_token= at all. The first
    # attempt should succeed outright, with no fallback needed.
    loaded = []

    def fake_from_pretrained(model, token):
        loaded.append((model, token))
        return "a-pipeline"

    fake_pipeline_class = _install_fake_pyannote_audio(
        monkeypatch, fake_from_pretrained, legacy_kwarg=False
    )
    _clear_diarization_pipeline_cache()

    pipeline = _get_diarization_pipeline("token-for-modern-kwarg-test")

    assert pipeline == "a-pipeline"
    assert loaded == [(DIARIZATION_MODEL, "token-for-modern-kwarg-test")]
    assert fake_pipeline_class.call_count == 1  # no fallback needed


class _FakeTurn:
    def __init__(self, start, end):
        self.start = start
        self.end = end


class _FakeAnnotation:
    """Stands in for pyannote.core.Annotation: the object diarize()
    expects to be able to call .itertracks(yield_label=True) on."""

    def __init__(self, tracks):
        self._tracks = tracks  # list of (turn, track_name, speaker)

    def itertracks(self, yield_label=False):
        return iter(self._tracks)


def _install_fake_diarization_pipeline(monkeypatch, pipeline_instance):
    """Like _install_fake_pyannote_audio, but for diarize() tests: the
    fake Pipeline.from_pretrained() returns a callable pipeline_instance
    (pipeline(path) -> output), rather than a plain sentinel value."""

    return _install_fake_pyannote_audio(
        monkeypatch,
        lambda model, token: pipeline_instance,
        legacy_kwarg=False,
    )


_FAKE_AUDIO_INPUT = {"waveform": "fake-waveform", "sample_rate": 16000}


def _fake_load_waveform_via_ffmpeg(monkeypatch, audio_input=_FAKE_AUDIO_INPUT):
    """diarize() decodes audio itself via ffmpeg before ever calling
    the pyannote pipeline (see _load_waveform_via_ffmpeg) - stub that
    out so these tests exercise diarize()'s pipeline-output handling
    without needing a real audio file, numpy, or torch installed."""

    import blackvue.generate.speech as speech_module

    monkeypatch.setattr(
        speech_module, "_load_waveform_via_ffmpeg", lambda source: audio_input
    )


def test_diarize_supports_legacy_pipeline_returning_annotation_directly(
    monkeypatch,
):
    # speaker-diarization-3.1 (and presumably other older pipelines)
    # return the Annotation itself when called.
    annotation = _FakeAnnotation(
        [
            (_FakeTurn(0.0, 1.0), "_", "SPEAKER_00"),
            (_FakeTurn(1.0, 2.0), "_", "SPEAKER_01"),
        ]
    )
    received_inputs = []

    class _FakePipelineInstance:
        def __call__(self, audio_input):
            received_inputs.append(audio_input)
            return annotation

    _install_fake_diarization_pipeline(monkeypatch, _FakePipelineInstance())
    _fake_load_waveform_via_ffmpeg(monkeypatch)
    _clear_diarization_pipeline_cache()

    turns = diarize(
        Path("/tmp/audio.aac"), hf_token="token-for-diarize-legacy-test"
    )

    assert turns == (
        SpeakerTurn(start=0.0, end=1.0, speaker="SPEAKER_00"),
        SpeakerTurn(start=1.0, end=2.0, speaker="SPEAKER_01"),
    )
    # Confirms diarize() hands the pipeline the decoded waveform dict,
    # not a bare path - i.e. it never lets pyannote's own (torchcodec-
    # dependent) audio loader get involved.
    assert received_inputs == [_FAKE_AUDIO_INPUT]


def test_diarize_supports_new_pipeline_wrapper_output(monkeypatch):
    # DIARIZATION_MODEL (speaker-diarization-community-1) wraps the
    # Annotation in an output object exposing it as
    # .speaker_diarization instead of returning it directly.
    annotation = _FakeAnnotation(
        [(_FakeTurn(0.0, 1.5), "_", "SPEAKER_00")]
    )

    class _FakeOutput:
        def __init__(self, speaker_diarization):
            self.speaker_diarization = speaker_diarization

    class _FakePipelineInstance:
        def __call__(self, audio_input):
            return _FakeOutput(annotation)

    _install_fake_diarization_pipeline(monkeypatch, _FakePipelineInstance())
    _fake_load_waveform_via_ffmpeg(monkeypatch)
    _clear_diarization_pipeline_cache()

    turns = diarize(
        Path("/tmp/audio.aac"), hf_token="token-for-diarize-wrapper-test"
    )

    assert turns == (SpeakerTurn(start=0.0, end=1.5, speaker="SPEAKER_00"),)


def test_load_waveform_via_ffmpeg_decodes_real_audio(monkeypatch, tmp_path):
    # Real ffmpeg, real decode - only torch is faked (not installed in
    # this sandbox), with a minimal stand-in that mirrors the
    # torch.from_numpy(...).unsqueeze(0) shape contract
    # _load_waveform_via_ffmpeg relies on.
    import blackvue.generate.speech as speech_module

    class _FakeTensor:
        def __init__(self, array):
            self.array = array

        def unsqueeze(self, dim):
            return self

    fake_torch = types.ModuleType("torch")
    fake_torch.from_numpy = lambda array: _FakeTensor(array)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    # A short, real audio file: 0.5s of silence at 8kHz mono, so the
    # ffmpeg command under test has to actually resample/remix it to
    # _DIARIZATION_SAMPLE_RATE (16kHz) mono to prove those flags work,
    # not just pass through an already-matching file.
    source = tmp_path / "silence.wav"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
            "-t", "0.5",
            str(source),
        ],
        check=True,
        capture_output=True,
    )

    result = speech_module._load_waveform_via_ffmpeg(source)

    assert result["sample_rate"] == 16000
    # 0.5s @ 16kHz mono float32 = 8000 samples.
    assert len(result["waveform"].array) == 8000


def test_load_waveform_via_ffmpeg_reports_missing_torch_cleanly(tmp_path):
    # Genuine, unmocked condition in this sandbox: torch isn't
    # installed. Confirms the missing-dependency path gives a clean
    # MediaToolError (matching the project's pattern everywhere else)
    # instead of a raw ImportError/traceback.
    import blackvue.generate.speech as speech_module

    source = tmp_path / "does-not-need-to-exist.aac"

    try:
        speech_module._load_waveform_via_ffmpeg(source)
        raised = False
        message = ""
    except MediaToolError as exc:
        raised = True
        message = str(exc)

    assert raised is True
    assert "torch" in message
