"""
Ponto de entrada usado no executável empacotado (PyInstaller).

Sobe o servidor (igual `python app.py`) e abre o navegador sozinho depois de
um instante — pensado pra quem só quer dar duplo-clique e já usar, sem saber
o que é terminal.
"""
import os
import sys
import threading
import time
import webbrowser

# Quando empacotado pelo PyInstaller, os arquivos de dados (templates/,
# config.yaml.example) ficam em sys._MEIPASS; mas config.yaml (gravável)
# precisa morar do LADO do executável, não dentro do pacote read-only.
if getattr(sys, "frozen", False):
    BUNDLE_DIR = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BUNDLE_DIR

os.chdir(APP_DIR)
sys.path.insert(0, BUNDLE_DIR)

import app as app_module  # noqa: E402


def _open_browser_later(url, delay=1.5):
    time.sleep(delay)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    import uvicorn

    web = app_module.config.get("web", {})
    host = web.get("host", "0.0.0.0")
    port = web.get("port", 8080)
    open_url = f"http://127.0.0.1:{port}"

    print("=" * 60)
    print(" APRS Digipeater Dashboard")
    print(f" Abrindo em {open_url}")
    print(" Feche esta janela pra parar o programa.")
    print("=" * 60)

    threading.Thread(target=_open_browser_later, args=(open_url,), daemon=True).start()
    uvicorn.run(app_module.app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
