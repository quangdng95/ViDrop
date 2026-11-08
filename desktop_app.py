import fcntl
import os
import subprocess
import sys
import threading
import webview
from server import app
from backend import paths, webscan

PORT = 5001
_LOCK_PATH = os.path.join(paths.APP_SUPPORT_DIR, "vidrop.lock")
# Module-level, not local: must stay open (and therefore locked) for the whole
# life of the process. A local variable would get garbage-collected - and the
# flock released - right after _acquire_singleton_lock() returns.
_lock_file = None


def _acquire_singleton_lock():
    global _lock_file
    os.makedirs(paths.APP_SUPPORT_DIR, exist_ok=True)
    _lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        _lock_file.close()
        _lock_file = None
        return False


def run_flask():
    app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    if not _acquire_singleton_lock():
        # ViDrop is already running. Without this guard, every extra launch -
        # double-clicking the Dock/Finder/Launchpad icon again, including once
        # while the first launch is still starting up a couple seconds in -
        # spawned a whole new OS process with its own window AND ITS OWN
        # PERMANENT DOCK ICON, piling up without limit over repeated opens.
        # Only the first process's Flask server actually binds PORT (every
        # later one fails inside its background thread, silently, since
        # run_flask() has no caller to propagate the bind error to) - but
        # pywebview still opens a window pointed at that same port regardless,
        # so it just shows the first instance's content and LOOKS fine, while
        # the zombie process + its Dock icon never go away on their own. An
        # ad-hoc-signed app isn't deduped by LaunchServices the way a
        # notarized one is, so this isn't a one-off fluke - it reproduces from
        # a completely ordinary double-click. Focus the one real window
        # instead of creating a second one, then exit immediately so this
        # process never gets as far as creating anything the Dock would keep.
        subprocess.run(["osascript", "-e", 'tell application "ViDrop" to activate'], capture_output=True)
        sys.exit(0)

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
