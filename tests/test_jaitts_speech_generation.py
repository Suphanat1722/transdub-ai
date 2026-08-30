import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from app.services.speech_generation import (
    GENERATION_END_MARGIN_SECONDS,
    apply_glossary,
    count_spoken_characters,
    estimate_total_duration_seconds,
    generate_speech,
)


def test_generation_uses_the_reference_duration_after_flowtts_preprocessing(monkeypatch, tmp_path):
    reference = tmp_path / "reference.wav"
    processed_reference = tmp_path / "processed-reference.wav"
    output = tmp_path / "output.wav"
    sf.write(reference, np.zeros(5 * 24_000, dtype=np.float32), 24_000)

    captured = {}

    def preprocess_ref_audio_text(_path, text, clip_short=True):
        captured["clip_short"] = clip_short
        sf.write(processed_reference, np.zeros(2 * 24_000, dtype=np.float32), 24_000)
        return str(processed_reference), f"{text}. "

    def infer_process(ref_file, ref_text, gen_text, *_args, **kwargs):
        captured.update(
            ref_file=ref_file,
            ref_text=ref_text,
            gen_text=gen_text,
            fix_duration=kwargs["fix_duration"],
        )
        return np.zeros(24_000, dtype=np.float32), 24_000, np.zeros((1, 1), dtype=np.float32)

    flowtts = types.ModuleType("flowtts")
    flowtts.__path__ = []
    flowtts_infer = types.ModuleType("flowtts.infer")
    flowtts_infer.__path__ = []
    inference_module = types.ModuleType("flowtts.inference")
    inference_module.convert_to_wav = lambda source, target: None
    inference_module.remove_silence_edges = lambda audio: audio
    utils_module = types.ModuleType("flowtts.infer.utils_infer")
    utils_module.chunk_text = lambda text, max_chars: [text]
    utils_module.infer_process = infer_process
    utils_module.preprocess_ref_audio_text = preprocess_ref_audio_text
    f5_tts = types.ModuleType("f5_tts")
    f5_tts.__path__ = []
    f5_model = types.ModuleType("f5_tts.model")
    f5_model.__path__ = []
    f5_utils = types.ModuleType("f5_tts.model.utils")
    f5_utils.seed_everything = lambda seed: captured.update(seed=seed)
    for name, module in (
        ("flowtts", flowtts),
        ("flowtts.infer", flowtts_infer),
        ("flowtts.inference", inference_module),
        ("flowtts.infer.utils_infer", utils_module),
        ("f5_tts", f5_tts),
        ("f5_tts.model", f5_model),
        ("f5_tts.model.utils", f5_utils),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    class FakeModel:
        ema_model = object()
        vocoder = object()
        mel_spec_type = "vocos"
        device = "cpu"

        @staticmethod
        def export_wav(waveform, path):
            sf.write(path, waveform, 24_000)

    pipeline = SimpleNamespace(
        temp_dir=tmp_path,
        audio_config=SimpleNamespace(
            silence_padding=200,
            target_rms=0.1,
            cross_fade_duration=0.15,
            nfe_step=32,
            cfg_strength=2.5,
        ),
        model_config=SimpleNamespace(seed=123),
        model=FakeModel(),
        _process_audio_silence=lambda audio: audio,
    )

    generate_speech(
        pipeline,
        text="ใหม่ i",
        reference_audio=str(reference),
        reference_text="ต้นฉบับ",
        output_file=str(output),
        speech_speed=1.0,
        duration_multiplier=1.0,
    )

    expected_seconds = (
        2
        + 2 * count_spoken_characters("ใหม่ i") / count_spoken_characters("ต้นฉบับ. ")
        + GENERATION_END_MARGIN_SECONDS
    )
    assert captured["ref_file"] == str(processed_reference)
    assert captured["clip_short"] is False
    assert captured["gen_text"] == "ใหม่ i"
    assert captured["fix_duration"] == pytest.approx(expected_seconds)
    assert captured["seed"] == 123
    assert output.is_file()
    assert not processed_reference.exists()


def test_generation_assigns_duration_per_batch_instead_of_reusing_whole_duration(monkeypatch, tmp_path):
    calls = []
    exported = {}

    def infer_process(_ref_file, _ref_text, gen_text, *_args, **kwargs):
        calls.append((gen_text, kwargs["fix_duration"], kwargs["cross_fade_duration"]))
        return np.ones(24_000, dtype=np.float32), 24_000, None

    utils_module = types.ModuleType("flowtts.infer.utils_infer")
    utils_module.chunk_text = lambda _text, max_chars: ["ช่วงแรก", "ช่วงที่สอง"]
    utils_module.infer_process = infer_process
    f5_utils = types.ModuleType("f5_tts.model.utils")
    f5_utils.seed_everything = lambda _seed: None
    monkeypatch.setitem(sys.modules, "flowtts.infer.utils_infer", utils_module)
    monkeypatch.setitem(sys.modules, "f5_tts.model.utils", f5_utils)

    class FakeModel:
        ema_model = object()
        vocoder = object()
        mel_spec_type = "vocos"
        device = "cpu"

        @staticmethod
        def export_wav(waveform, path):
            exported.update(waveform=waveform, path=path)

    pipeline = SimpleNamespace(
        audio_config=SimpleNamespace(
            target_rms=0.1,
            cross_fade_duration=0.15,
            nfe_step=32,
            cfg_strength=2.5,
        ),
        model_config=SimpleNamespace(seed=123),
        model=FakeModel(),
    )
    prepared = SimpleNamespace(audio_path="prepared.wav", text="ข้อความอ้างอิงยาว", duration_seconds=5.0)
    generate_speech(
        pipeline,
        text="ข้อความยาวที่ต้องแบ่งสองช่วง",
        reference_audio="reference.wav",
        reference_text="ข้อความอ้างอิงยาว",
        output_file=str(tmp_path / "output.wav"),
        speech_speed=1.0,
        duration_multiplier=1.1,
        prepared_reference=prepared,
    )

    assert [call[0] for call in calls] == ["ช่วงแรก", "ช่วงที่สอง"]
    assert all(call[2] == 0 for call in calls)
    whole_duration = estimate_total_duration_seconds(
        prepared.duration_seconds,
        prepared.text,
        "ข้อความยาวที่ต้องแบ่งสองช่วง",
        1.0,
        1.1,
    )
    assert all(call[1] < whole_duration for call in calls)
    assert len(exported["waveform"]) == 44_400


def test_glossary_changes_only_explicit_terms():
    rules = [{"source": "i", "spoken": "ไอ"}, {"source": "C++", "spoken": "ซีพลัสพลัส"}]
    assert apply_glossary("สั้น ๆ เช่น i และ C++", rules) == "สั้น ๆ เช่น ไอ และ ซีพลัสพลัส"
    assert apply_glossary("Initializer item_1", rules) == "Initializer item_1"
    assert apply_glossary("i ไม่ถูกเดา", []) == "i ไม่ถูกเดา"
    assert apply_glossary("path", [{"source": "path", "spoken": r"C:\voice\1"}]) == r"C:\voice\1"
