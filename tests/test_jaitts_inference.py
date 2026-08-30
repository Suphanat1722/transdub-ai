import queue
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import inference


class FakeQueue:
    def __init__(self, items=None):
        self.items = list(items or [])

    def put(self, item):
        self.items.append(item)

    def get(self, timeout=None):
        del timeout
        if not self.items:
            raise queue.Empty
        return self.items.pop(0)


class FakeProcess:
    def __init__(self, alive=True):
        self.alive = alive
        self.terminated = False

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        del timeout

    def terminate(self):
        self.terminated = True
        self.alive = False


def test_status_file_roundtrip_and_invalid_json(monkeypatch, tmp_path):
    status = tmp_path / "status.json"
    monkeypatch.setattr(inference, "MODEL_STATUS_PATH", status)
    monkeypatch.setattr(inference, "ensure_directories", lambda: None)
    assert inference.read_model_status()["state"] == "not_downloaded"
    inference._write_status("ready", device="cpu")
    assert inference.read_model_status()["device"] == "cpu"
    status.write_text("{broken", encoding="utf-8")
    assert inference.read_model_status()["state"] == "error"


def test_inference_service_generate_success_error_and_dead_process():
    service = inference.InferenceService()
    service.process = FakeProcess()
    service.responses = FakeQueue(
        [{"request_id": "other", "ok": True}, {"request_id": "fixed", "ok": True, "output_file": "done.wav"}]
    )
    original_uuid = inference.uuid.uuid4
    inference.uuid.uuid4 = lambda: "fixed"
    try:
        assert (
            service.generate(
                text="x",
                reference_audio="r.wav",
                reference_text="r",
                output_file="o.wav",
                nfe_step=16,
                speed=1,
                seed=1,
            )
            == "done.wav"
        )
        service.responses = FakeQueue([{"request_id": "fixed", "ok": False, "error": "bad"}])
        with pytest.raises(RuntimeError, match="bad"):
            service.generate(
                text="x",
                reference_audio="r.wav",
                reference_text="r",
                output_file="o.wav",
                nfe_step=16,
                speed=1,
                seed=1,
            )
        service.process.alive = False
        with pytest.raises(RuntimeError, match="ไม่ทำงาน"):
            service.generate(
                text="x",
                reference_audio="r.wav",
                reference_text="r",
                output_file="o.wav",
                nfe_step=16,
                speed=1,
                seed=1,
            )
    finally:
        inference.uuid.uuid4 = original_uuid


def test_service_status_detects_and_restarts_dead_child(monkeypatch):
    monkeypatch.delenv("JAI_TTS_DISABLE_MODEL_PROCESS", raising=False)
    service = inference.InferenceService()
    service.process = FakeProcess(False)
    monkeypatch.setattr(inference, "read_model_status", lambda: {"state": "ready", "device": "cuda"})
    restarted = []
    monkeypatch.setattr(service, "start", lambda: restarted.append(True))
    status = service.status()
    assert status["state"] == "loading" and restarted


def test_service_start_and_stop_lifecycle(monkeypatch):
    monkeypatch.delenv("JAI_TTS_DISABLE_MODEL_PROCESS", raising=False)
    created = FakeProcess(True)
    created.start = lambda: None
    context = SimpleNamespace(
        Queue=lambda: FakeQueue(),
        Event=lambda: SimpleNamespace(clear=lambda: None, set=lambda: None),
        Process=lambda **_kwargs: created,
    )
    monkeypatch.setattr(inference.mp, "get_context", lambda _method: context)
    service = inference.InferenceService()
    service.start()
    assert service.process is created
    service.stop()
    assert service._stopping is True


def test_torchaudio_compat_reads_frames(monkeypatch, tmp_path):
    audio = tmp_path / "audio.wav"
    import soundfile as sf

    sf.write(audio, [0.0, 0.5, -0.5], 24_000)
    torchaudio = types.ModuleType("torchaudio")
    monkeypatch.setitem(sys.modules, "torchaudio", torchaudio)
    fake_torch = SimpleNamespace(from_numpy=lambda value: value)
    inference._configure_torchaudio_compat(fake_torch)
    values, rate = torchaudio.load(audio, channels_first=False)
    assert rate == 24_000 and values.shape == (3, 1)


def test_process_main_loads_once_and_handles_generate(monkeypatch, tmp_path):
    checkpoint, vocab = tmp_path / "model.pt", tmp_path / "vocab.txt"
    checkpoint.write_bytes(b"model")
    vocab.write_text("vocab", encoding="utf-8")
    monkeypatch.setattr(inference, "MODEL_CHECKPOINT", str(checkpoint))
    monkeypatch.setattr(inference, "MODEL_VOCAB", str(vocab))
    monkeypatch.setattr(inference, "FLOWTTS_ROOT", tmp_path)
    monkeypatch.setattr(inference, "HF_CACHE_DIR", tmp_path)
    statuses = []
    monkeypatch.setattr(
        inference, "_write_status", lambda state, **details: statuses.append((state, details))
    )

    torch = types.ModuleType("torch")
    torch.__version__ = "test"
    torch.version = SimpleNamespace(cuda=None)
    torch.cuda = SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    torch.from_numpy = lambda value: value
    torchaudio = types.ModuleType("torchaudio")
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torchaudio", torchaudio)

    flowtts = types.ModuleType("flowtts")
    flowtts.__path__ = []
    flowtts_inference = types.ModuleType("flowtts.inference")

    class ModelConfig:
        def __init__(self, **kwargs):
            self.seed = kwargs["seed"]

    class AudioConfig:
        def __init__(self, **kwargs):
            self.nfe_step = kwargs["nfe_step"]

    class Pipeline:
        def __init__(self, model_config, audio_config, temp_dir):
            self.model_config = model_config
            self.audio_config = audio_config
            self.temp_dir = Path(temp_dir)

    flowtts_inference.ModelConfig = ModelConfig
    flowtts_inference.AudioConfig = AudioConfig
    flowtts_inference.FlowTTSPipeline = Pipeline
    monkeypatch.setitem(sys.modules, "flowtts", flowtts)
    monkeypatch.setitem(sys.modules, "flowtts.inference", flowtts_inference)
    prepared = SimpleNamespace(audio_path="prepared.wav", text="ref", duration_seconds=1)
    monkeypatch.setattr(inference, "prepare_reference", lambda *_args: prepared)
    generated = []
    monkeypatch.setattr(
        inference,
        "generate_speech",
        lambda _pipeline, **kwargs: generated.append(kwargs) or kwargs["output_file"],
    )

    requests = FakeQueue(
        [
            {
                "type": "generate",
                "request_id": "one",
                "text": "hello",
                "reference_audio": "ref.wav",
                "reference_text": "ref",
                "output_file": "out.wav",
                "nfe_step": 16,
                "seed": 7,
                "speed": 1.0,
                "duration_multiplier": 1.1,
            },
            {"type": "stop"},
        ]
    )
    responses = FakeQueue()
    inference._process_main(requests, responses, SimpleNamespace(is_set=lambda: False))
    assert [state for state, _ in statuses][-1] == "ready"
    assert responses.items == [{"request_id": "one", "ok": True, "output_file": "out.wav"}]
    assert generated[0]["prepared_reference"] is prepared


def test_process_main_reports_missing_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(inference, "MODEL_CHECKPOINT", "")
    monkeypatch.setattr(inference, "MODEL_VOCAB", "")
    monkeypatch.setattr(inference, "HF_CACHE_DIR", tmp_path)
    statuses = []
    monkeypatch.setattr(
        inference, "_write_status", lambda state, **details: statuses.append((state, details))
    )
    inference._process_main(FakeQueue(), FakeQueue(), SimpleNamespace(is_set=lambda: False))
    assert statuses[-1][0] == "error"
    assert "model.pt" in statuses[-1][1]["error"]
