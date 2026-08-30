from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENERATION_END_MARGIN_SECONDS = 0.25


def apply_glossary(text: str, glossary: list[dict[str, str]]) -> str:
    """Apply only explicit project rules; JaiCue never guesses English pronunciation."""
    result = text
    for rule in sorted(glossary, key=lambda item: len(item["source"]), reverse=True):
        source, spoken = rule["source"], rule["spoken"]
        if source.isalnum():
            literal_replacement = spoken.replace("\\", r"\\")
            result = re.sub(rf"(?<!\w){re.escape(source)}(?!\w)", literal_replacement, result)
        else:
            result = result.replace(source, spoken)
    return result


@dataclass(slots=True)
class PreparedReference:
    audio_path: str
    text: str
    duration_seconds: float


def prepare_reference(pipeline: Any, reference_audio: str, reference_text: str) -> PreparedReference:
    """Preprocess a profile once in the inference process and reuse it for every cue."""
    import soundfile as sf
    from flowtts.infer.utils_infer import preprocess_ref_audio_text
    from flowtts.inference import convert_to_wav

    reference_file = reference_audio
    if not reference_audio.lower().endswith(".wav"):
        converted_reference = pipeline.temp_dir / f"ref-{abs(hash(reference_audio))}.wav"
        convert_to_wav(reference_audio, str(converted_reference))
        reference_file = str(converted_reference)
    inference_audio, inference_text = preprocess_ref_audio_text(
        reference_file, reference_text, clip_short=False
    )
    info = sf.info(inference_audio)
    return PreparedReference(inference_audio, inference_text, info.frames / info.samplerate)


def count_spoken_characters(text: str) -> int:
    """Count Unicode characters instead of UTF-8 bytes for mixed-script speech timing."""
    return max(1, sum(not character.isspace() for character in text))


def legacy_byte_timing_ratio(reference_text: str, generated_text: str) -> float:
    """Compare the old UTF-8 byte estimate with the Unicode character estimate."""
    byte_ratio = len(generated_text.encode("utf-8")) / max(1, len(reference_text.encode("utf-8")))
    character_ratio = count_spoken_characters(generated_text) / count_spoken_characters(reference_text)
    return byte_ratio / character_ratio


def needs_mixed_script_duration_retry(reference_text: str, generated_text: str) -> bool:
    """Identify text the former byte-based formula would under-allocate by at least 10%."""
    return legacy_byte_timing_ratio(reference_text, generated_text) < 0.90


def estimate_total_duration_seconds(
    reference_duration_seconds: float,
    reference_text: str,
    generated_text: str,
    speech_speed: float,
    duration_multiplier: float = 1.0,
) -> float:
    """Estimate the total reference-plus-generated duration expected by F5-TTS."""
    if reference_duration_seconds <= 0:
        raise ValueError("reference_duration_seconds must be greater than zero")
    if speech_speed <= 0:
        raise ValueError("speech_speed must be greater than zero")
    if duration_multiplier <= 0:
        raise ValueError("duration_multiplier must be greater than zero")
    reference_units = count_spoken_characters(reference_text)
    generated_units = count_spoken_characters(generated_text)
    generated_duration_seconds = (
        reference_duration_seconds * generated_units / reference_units / speech_speed * duration_multiplier
    )
    generated_duration_seconds += GENERATION_END_MARGIN_SECONDS / speech_speed
    return reference_duration_seconds + generated_duration_seconds


def split_text_for_inference(reference: PreparedReference, text: str) -> list[str]:
    """Match FlowTTS batching before inference so every batch gets its own duration."""
    from flowtts.infer.utils_infer import chunk_text

    available_seconds = 22 - reference.duration_seconds
    if available_seconds <= 0:
        raise ValueError("reference audio ยาวเกินไปสำหรับการสร้างเสียง กรุณาใช้เสียงอ้างอิงไม่เกิน 22 วินาที")
    reference_bytes_per_second = len(reference.text.encode("utf-8")) / reference.duration_seconds
    max_batch_bytes = max(1, int(reference_bytes_per_second * available_seconds))
    batches = chunk_text(text, max_chars=max_batch_bytes)
    if not batches:
        raise ValueError("ไม่มีข้อความสำหรับสร้างเสียง")
    return batches


def crossfade_waveforms(waveforms: list[Any], sample_rate: int, duration_seconds: float) -> Any:
    """Join generated-only batches with the same linear crossfade used by FlowTTS."""
    import numpy as np

    combined = waveforms[0]
    for waveform in waveforms[1:]:
        crossfade_samples = min(int(duration_seconds * sample_rate), len(combined), len(waveform))
        if crossfade_samples <= 0:
            combined = np.concatenate((combined, waveform))
            continue
        fade_out = np.linspace(1, 0, crossfade_samples)
        fade_in = np.linspace(0, 1, crossfade_samples)
        overlap = combined[-crossfade_samples:] * fade_out + waveform[:crossfade_samples] * fade_in
        combined = np.concatenate((combined[:-crossfade_samples], overlap, waveform[crossfade_samples:]))
    return combined


def generate_speech(
    pipeline: Any,
    *,
    text: str,
    reference_audio: str,
    reference_text: str,
    output_file: str,
    speech_speed: float,
    duration_multiplier: float,
    prepared_reference: PreparedReference | None = None,
) -> str:
    """Generate only the continuation after preprocessing the reference exactly once."""
    from f5_tts.model.utils import seed_everything
    from flowtts.infer.utils_infer import infer_process

    prepared = prepared_reference or prepare_reference(pipeline, reference_audio, reference_text)
    try:
        text_batches = split_text_for_inference(prepared, text)
        seed = int(pipeline.model_config.seed)
        if seed == -1:
            seed = random.randrange(4294967295)
        seed_everything(seed)
        pipeline.model.seed = seed
        crossfade_seconds = float(pipeline.audio_config.cross_fade_duration)
        shared_end_padding = (
            GENERATION_END_MARGIN_SECONDS / speech_speed + crossfade_seconds * (len(text_batches) - 1)
        ) / len(text_batches)
        waveforms: list[Any] = []
        sample_rate: int | None = None
        for batch in text_batches:
            batch_total_seconds = estimate_total_duration_seconds(
                prepared.duration_seconds,
                prepared.text,
                batch,
                speech_speed,
                duration_multiplier,
            )
            batch_total_seconds -= GENERATION_END_MARGIN_SECONDS / speech_speed
            batch_total_seconds += shared_end_padding
            waveform, batch_sample_rate, _ = infer_process(
                prepared.audio_path,
                prepared.text,
                batch,
                pipeline.model.ema_model,
                pipeline.model.vocoder,
                pipeline.model.mel_spec_type,
                target_rms=pipeline.audio_config.target_rms,
                cross_fade_duration=0,
                nfe_step=pipeline.audio_config.nfe_step,
                cfg_strength=pipeline.audio_config.cfg_strength,
                sway_sampling_coef=0.0,
                speed=speech_speed,
                fix_duration=batch_total_seconds,
                device=pipeline.model.device,
            )
            if waveform is None:
                raise RuntimeError("JaiTTS ไม่คืนข้อมูลเสียง")
            if sample_rate is not None and sample_rate != batch_sample_rate:
                raise RuntimeError("JaiTTS คืน sample rate ของแต่ละ batch ไม่ตรงกัน")
            sample_rate = int(batch_sample_rate)
            waveforms.append(waveform)
        combined = crossfade_waveforms(waveforms, sample_rate or 24_000, crossfade_seconds)
        pipeline.model.export_wav(combined, output_file)
    finally:
        if prepared_reference is None:
            generated_reference = Path(prepared.audio_path)
            if generated_reference.resolve() != Path(reference_audio).resolve():
                generated_reference.unlink(missing_ok=True)
    return str(Path(output_file))
