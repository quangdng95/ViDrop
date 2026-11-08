import threading
import webview
from server import app
from backend import webscan

PORT = 5001


def run_flask():
    app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    # Fire-and-forget: downloads Playwright's Chromium (~150MB, one-time) in the
    # background so the first real "scan this webpage" use isn't the one paying
    # for it. webscan.fetch_generic_page_media() does this check itself too, so
    # this is purely an optimization, not a correctness requirement.
    threading.Thread(target=webscan.warm_up_chromium, daemon=True).start()
    webview.create_window("ViDrop", f"http://127.0.0.1:{PORT}", width=720, height=1024)
    # private_mode=False: pywebview's macOS default (True) wipes the web view's
    # saved data on every launch, which erased the History tab and the language
    # choice each time the app was closed.
    webview.start(private_mode=False)
