import threading

# Demucs uses the GPU on constrained hardware.  Keeping one process in the
# critical section avoids intermittent OOMs and makes retries deterministic.
GPU_LOCK = threading.RLock()
