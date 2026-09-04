import socket
import threading
import webbrowser

import uvicorn

from app.core.config import HOST, PORT


def _another_instance_running() -> bool:
    """Check whether a server already listens (double-click guard).

    Two servers share one SQLite file: the second one's startup recovery
    re-queues the first one's live job, so both work the same job at once.
    """
    try:
        with socket.create_connection((HOST, PORT), timeout=1.5):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    if _another_instance_running():
        print(f"TransDub AI is already running at http://{HOST}:{PORT} - opening it instead.")
        webbrowser.open(f"http://{HOST}:{PORT}")
        raise SystemExit(0)
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")
