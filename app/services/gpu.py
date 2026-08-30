import threading

# Demucs and JaiTTS share an 8 GB GPU.  Keeping one process in the critical
# section avoids intermittent CUDA OOMs and makes retries deterministic.
GPU_LOCK = threading.RLock()
