"""
Entry point used by the packaged executable (PyInstaller).

Starts the server (same as `python app.py`) and opens the browser
automatically. On Windows and macOS it runs hidden in the background with a
system tray / menu bar icon (no terminal window) — right-click it to reopen
the dashboard or quit. On Linux it still runs in a terminal window, since
tray icon support varies too much across desktop environments to promise a
consistent experience.
"""
import os
import sys
import threading
import time
import webbrowser

# When packaged by PyInstaller, data files (templates/, config.yaml.example)
# live in sys._MEIPASS; but config.yaml (writable) needs to live BESIDE the
# executable, not inside the read-only bundle.
if getattr(sys, "frozen", False):
    BUNDLE_DIR = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BUNDLE_DIR

os.chdir(APP_DIR)
sys.path.insert(0, BUNDLE_DIR)

import app as app_module  # noqa: E402

HAS_TRAY = sys.platform in ("win32", "darwin")


def _open_browser(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _open_browser_later(url, delay=1.5):
    time.sleep(delay)
    _open_browser(url)


def _make_tray_icon():
    """A simple generated icon (no external image files needed)."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(79, 163, 255, 255))
    draw.text((size / 2 - 10, size / 2 - 14), "A", fill=(6, 17, 31, 255))
    return img


def _run_with_tray(open_url, server_thread):
    import pystray
    from pystray import MenuItem as Item

    def on_open(icon, item):
        _open_browser(open_url)

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)  # server thread is a daemon; just end the process

    icon = pystray.Icon(
        "aprs_dashboard",
        icon=_make_tray_icon(),
        title="APRS Digipeater Dashboard",
        menu=pystray.Menu(
            Item("Open dashboard", on_open, default=True),
            Item("Quit", on_quit),
        ),
    )
    threading.Thread(target=_open_browser_later, args=(open_url,), daemon=True).start()
    icon.run()  # blocks on the main thread (required on macOS)


def main():
    import uvicorn

    web = app_module.config.get("web", {})
    host = web.get("host", "0.0.0.0")
    port = web.get("port", 8080)
    open_url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(app_module.app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if HAS_TRAY:
        try:
            _run_with_tray(open_url, server_thread)
            return
        except Exception:
            # If the tray icon can't start for some reason (missing display,
            # unsupported desktop, etc), fall back to the plain terminal mode
            # below instead of silently doing nothing.
            pass

    print("=" * 60)
    print(" APRS Digipeater Dashboard")
    print(f" Opening at {open_url}")
    print(" Close this window to stop the program.")
    print("=" * 60)
    threading.Thread(target=_open_browser_later, args=(open_url,), daemon=True).start()
    server_thread.join()


if __name__ == "__main__":
    main()
