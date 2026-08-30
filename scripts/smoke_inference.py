from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.config import GENERATION_DURATION_MULTIPLIERS
from app.services.audio import has_active_audio_tail, wav_duration_ms
from app.services.inference import InferenceService
from app.services.speech_generation import needs_mixed_script_duration_retry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate one isolated JaiTTS sample without creating a job")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--reference-text", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nfe-step", type=int, choices=(16, 32), default=32)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    service = InferenceService()
    service.start()
    try:
        possible_truncation = False
        used_multiplier = GENERATION_DURATION_MULTIPLIERS[0]
        duration_multipliers = (
            GENERATION_DURATION_MULTIPLIERS
            if needs_mixed_script_duration_retry(args.reference_text, args.text)
            else GENERATION_DURATION_MULTIPLIERS[:1]
        )
        for used_multiplier in duration_multipliers:
            service.generate(
                text=args.text,
                reference_audio=str(args.reference.resolve()),
                reference_text=args.reference_text,
                output_file=str(args.output.resolve()),
                nfe_step=args.nfe_step,
                speed=args.speed,
                seed=args.seed,
                duration_multiplier=used_multiplier,
            )
            possible_truncation = len(duration_multipliers) > 1 and has_active_audio_tail(args.output)
            if not possible_truncation:
                break
        print(
            json.dumps(
                {
                    "output": str(args.output.resolve()),
                    "duration_ms": wav_duration_ms(args.output),
                    "duration_multiplier": used_multiplier,
                    "possible_truncation": possible_truncation,
                },
                ensure_ascii=False,
            )
        )
    finally:
        service.stop()


if __name__ == "__main__":
    main()
