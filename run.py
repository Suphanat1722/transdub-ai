import threading
import webbrowser

import uvicorn

from app.core.config import HOST, PORT


if __name__ == "__main__":
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")
