import os
import threading
import webbrowser

import uvicorn

HOST = os.environ.get("LOGO_STUDIO_HOST", "127.0.0.1")
PORT = int(os.environ.get("LOGO_STUDIO_PORT", "8765"))

if __name__ == "__main__":
    if HOST in ("127.0.0.1", "localhost") and not os.environ.get("LOGO_STUDIO_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")
