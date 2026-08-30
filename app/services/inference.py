from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import signal
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from ..core.config import (
    FLOWTTS_ROOT,
    HF_CACHE_DIR,
    MODEL_CHECKPOINT,
    MODEL_NAME,
    MODEL_REVISION,
    MODEL_SOURCE,
    MODEL_STATUS_PATH,
    MODEL_VOCAB,
    ensure_directories,
)
from .speech_generation import generate_speech, prepare_reference


def _configure_torchaudio_compat(torch) -> None:
    """Use libsndfile for WAV input so Windows does not require TorchCodec FFmpeg DLLs."""
    import soundfile as sf
    import torchaudio

    def load_audio(uri, frame_offset=0, num_frames=-1, normalize=True, channels_first=True, **_kwargs):
        del normalize  # soundfile returns normalized float32 samples for PCM input.
        frames = -1 if num_frames is None or num_frames < 0 else int(num_frames)
        audio, sample_rate = sf.read(
            str(uri), start=int(frame_offset), frames=frames, dtype="float32", always_2d=True
        )
        tensor = torch.from_numpy(audio)
        if channels_first:
            tensor = tensor.transpose(0, 1)
        return tensor, sample_rate

    torchaudio.load = load_audio


def _write_status(state: str, **details) -> None:
    ensure_directories()
    payload = {
        "state": state,
        "model": MODEL_NAME,
        "revision": MODEL_REVISION,
        "source": MODEL_SOURCE,
        "updated_at": datetime.now(UTC).isoformat(),
        **details,
    }
    temp = MODEL_STATUS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(MODEL_STATUS_PATH)


def read_model_status() -> dict:
    if not MODEL_STATUS_PATH.is_file():
        return {"state": "not_downloaded", "model": MODEL_NAME, "revision": MODEL_REVISION}
    try:
        return json.loads(MODEL_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "error", "model": MODEL_NAME, "error": "อ่านสถานะโมเดลไม่ได้"}


def _process_main(requests: Any, responses: Any, stop: Any) -> None:
    # Let the parent Uvicorn process own Ctrl+C and shut this child down cleanly.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if os.name == "nt":
        import ctypes

        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
    os.environ["HF_HOME"] = str(HF_CACHE_DIR)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, str(FLOWTTS_ROOT))
    try:
        if not MODEL_CHECKPOINT or not MODEL_VOCAB:
            raise FileNotFoundError(
                "ไม่พบ model.pt และ vocab.txt ใน models/JaiTTS-F5TTS "
                "หรือ models--JTS-AI--JaiTTS-F5TTS/snapshots/*"
            )
        _write_status("loading", message="พบ checkpoint และ vocab ใน workspace กำลังโหลดโมเดล local")
        import torch

        _configure_torchaudio_compat(torch)
        from flowtts.inference import AudioConfig, FlowTTSPipeline, ModelConfig

        checkpoint = MODEL_CHECKPOINT
        vocab = MODEL_VOCAB
        device = "cuda" if torch.cuda.is_available() else "cpu"
        gpu = None
        vram_mb = 0
        if device == "cuda":
            props = torch.cuda.get_device_properties(0)
            gpu, vram_mb = props.name, round(props.total_memory / 1024 / 1024)
        _write_status("loading", device=device, gpu=gpu, vram_mb=vram_mb, message="กำลังโหลด JaiTTS และ Vocos")
        model_config = ModelConfig(
            language="th",
            model_type="F5",
            checkpoint=checkpoint,
            vocab_file=vocab,
            vocoder="vocos",
            device=device,
            seed=0,
        )
        audio_config = AudioConfig(silence_threshold=-45, cfg_strength=2.5, nfe_step=32, speed=1.0)
        pipeline = FlowTTSPipeline(
            model_config=model_config, audio_config=audio_config, temp_dir=str(HF_CACHE_DIR / "temp")
        )
        prepared_references: dict[tuple[str, str], Any] = {}
        _write_status(
            "ready",
            device=device,
            gpu=gpu,
            vram_mb=vram_mb,
            torch_version=torch.__version__,
            cuda_version=torch.version.cuda,
        )
        while not stop.is_set():
            try:
                command = requests.get(timeout=0.5)
            except queue.Empty:
                continue
            if command.get("type") == "stop":
                break
            request_id = command["request_id"]
            try:
                pipeline.audio_config.nfe_step = int(command["nfe_step"])
                pipeline.model_config.seed = int(command["seed"])
                reference_key = (command["reference_audio"], command["reference_text"])
                prepared = prepared_references.get(reference_key)
                if prepared is None:
                    prepared = prepare_reference(pipeline, *reference_key)
                    prepared_references[reference_key] = prepared
                output = generate_speech(
                    pipeline,
                    text=command["text"],
                    reference_audio=command["reference_audio"],
                    reference_text=command["reference_text"],
                    output_file=command["output_file"],
                    speech_speed=float(command["speed"]),
                    duration_multiplier=float(command["duration_multiplier"]),
                    prepared_reference=prepared,
                )
                responses.put({"request_id": request_id, "ok": True, "output_file": str(output)})
            except Exception as exc:
                if "out of memory" in str(exc).lower() and device == "cuda":
                    torch.cuda.empty_cache()
                responses.put({"request_id": request_id, "ok": False, "error": str(exc)})
    except Exception as exc:
        message = str(exc)
        hint = "วาง model.pt และ vocab.txt ในโฟลเดอร์ models/JaiTTS-F5TTS แล้วเปิดแอปใหม่"
        _write_status("error", error=message, hint=hint)


class InferenceService:
    def __init__(self) -> None:
        context = mp.get_context("spawn")
        self.requests = context.Queue()
        self.responses = context.Queue()
        self.stop_event = context.Event()
        self.process: Any = None
        self._stopping = False
        self._last_restart = 0.0

    def start(self) -> None:
        if os.getenv("JAI_TTS_DISABLE_MODEL_PROCESS") == "1":
            return
        if self.process and self.process.is_alive():
            return
        self._stopping = False
        self.stop_event.clear()
        self.process = mp.get_context("spawn").Process(
            target=_process_main,
            args=(self.requests, self.responses, self.stop_event),
            daemon=True,
            name="jaitts-inference",
        )
        self.process.start()

    def stop(self) -> None:
        if not self.process:
            return
        self._stopping = True
        self.stop_event.set()
        self.requests.put({"type": "stop"})
        self.process.join(timeout=10)
        if self.process.is_alive():
            self.process.terminate()

    def status(self) -> dict:
        status = read_model_status()
        disabled = os.getenv("JAI_TTS_DISABLE_MODEL_PROCESS") == "1"
        if (
            not disabled
            and status.get("state") == "ready"
            and (not self.process or not self.process.is_alive())
        ):
            status = {**status, "state": "error", "error": "inference process หยุดทำงาน", "recoverable": True}
            if not self._stopping and time.monotonic() - self._last_restart >= 10:
                self._last_restart = time.monotonic()
                self.start()
                status["state"] = "loading"
                status["message"] = "กำลัง restart inference process"
        status["process_alive"] = bool(self.process and self.process.is_alive())
        return status

    def generate(
        self,
        *,
        text: str,
        reference_audio: str,
        reference_text: str,
        output_file: str,
        nfe_step: int,
        speed: float,
        seed: int,
        duration_multiplier: float = 1.0,
        timeout: int = 3600,
    ) -> str:
        if not self.process or not self.process.is_alive():
            raise RuntimeError("JaiTTS inference process ไม่ทำงาน")
        request_id = str(uuid.uuid4())
        self.requests.put(
            {
                "type": "generate",
                "request_id": request_id,
                "text": text,
                "reference_audio": reference_audio,
                "reference_text": reference_text,
                "output_file": output_file,
                "nfe_step": nfe_step,
                "speed": speed,
                "seed": seed,
                "duration_multiplier": duration_multiplier,
            }
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = self.responses.get(timeout=1)
            except queue.Empty:
                continue
            if response.get("request_id") != request_id:
                continue
            if not response["ok"]:
                raise RuntimeError(response["error"])
            return response["output_file"]
        raise TimeoutError("JaiTTS ใช้เวลาสร้างเสียงเกินกำหนด")


inference_service = InferenceService()
