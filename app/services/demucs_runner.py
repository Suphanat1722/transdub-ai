"""Run Demucs without TorchCodec, which requires shared FFmpeg DLLs on Windows."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _install_audio_compat() -> None:
    import soundfile as sf
    import torch
    import torchaudio

    def load(
        uri: str | Path,
        frame_offset: int = 0,
        num_frames: int = -1,
        normalize: bool = True,
        channels_first: bool = True,
        **_: Any,
    ):
        del normalize
        frames = -1 if num_frames is None or num_frames < 0 else int(num_frames)
        samples, sample_rate = sf.read(
            str(uri), start=int(frame_offset), frames=frames, dtype="float32", always_2d=True
        )
        waveform = torch.from_numpy(samples)
        return (waveform.transpose(0, 1) if channels_first else waveform), sample_rate

    def save(
        uri: str | Path,
        source,
        sample_rate: int,
        channels_first: bool = True,
        bits_per_sample: int | None = None,
        **_: Any,
    ) -> None:
        samples = source.detach().cpu().float().numpy()
        if channels_first:
            samples = samples.T
        subtype = "PCM_24" if bits_per_sample == 24 else "PCM_16"
        sf.write(str(uri), samples, int(sample_rate), subtype=subtype)

    torchaudio.load = load
    torchaudio.save = save


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    os.environ.setdefault("TORCH_HOME", str(project_root / "models" / "demucs"))
    _install_audio_compat()
    from demucs.separate import main as demucs_main

    demucs_main()


if __name__ == "__main__":
    main()
