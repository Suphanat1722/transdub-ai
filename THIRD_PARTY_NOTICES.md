# Third-party notices

The MIT license in this repository covers only the application code authored for TransDub AI. It does not relicense third-party services, model weights, or packages.

## Microsoft Edge TTS

- Service: Microsoft Edge Text-to-Speech (`https://speech.platform.bing.com`), accessed via the Python package `edge-tts`
- Nature: a cloud service offering preset neural voices. TransDub AI does not perform voice cloning with it.
- Licensing: use of Microsoft's online TTS voices and their audio output is subject to Microsoft's own terms of service. Audio synthesized from Edge voices may carry usage restrictions; verify before distributing outputs commercially or publicly.
- Network: synthesis requires internet access to Microsoft's endpoint.

## Other dependencies

Python packages installed from `pyproject.toml` retain their respective upstream licenses. The Demucs model, when downloaded and used for background separation, retains its upstream (MIT) license.