import logging
import os
import shutil
import sys

import yaml
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import db
from aprs_client import AprsFeeds

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

# BASE_DIR: onde ficam os arquivos empacotados (templates, config.yaml.example)
# — read-only quando rodando como executável (PyInstaller).
# DATA_DIR: onde gravamos config.yaml e o banco — precisa ser gravável, então
# no executável empacotado usamos a pasta AO LADO do .exe/binário, não a
# pasta temporária somente-leitura onde o PyInstaller extrai tudo.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    DATA_DIR = os.path.dirname(sys.executable)
else:
    DATA_DIR = BASE_DIR
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        example = os.path.join(BASE_DIR, "config.yaml.example")
        shutil.copy(example, CONFIG_PATH)
        log.warning(
            "config.yaml não existia — criei uma cópia de config.yaml.example. "
            "Edite %s com os dados do SEU digi antes de usar de verdade.",
            CONFIG_PATH,
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


config = load_config()
db.configure(os.path.join(DATA_DIR, config.get("database", {}).get("path", "aprs_dashboard.db")))
db.init_db()

app = FastAPI(title="APRS Digipeater Dashboard")


@app.on_event("startup")
def startup():
    feeds = AprsFeeds(config)
    feeds.start()
    app.state.feeds = feeds
    log.info("Feeds iniciados para %s", config["digi"]["callsign"])


@app.on_event("shutdown")
def shutdown():
    feeds = getattr(app.state, "feeds", None)
    if feeds:
        feeds.stop()


@app.get("/", response_class=HTMLResponse)
def index():
    path = os.path.join(BASE_DIR, "templates", "dashboard.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    return html.replace("__DIGI_CALLSIGN__", config["digi"]["callsign"])


@app.get("/api/config")
def api_config():
    return {
        "digi_callsign": config["digi"]["callsign"],
        "digi_lat": config["digi"]["lat"],
        "digi_lon": config["digi"]["lon"],
    }


def _settings_view(cfg):
    """Só os campos editáveis pela tela de Configurações (sem caminho de banco etc)."""
    lf = cfg.get("local_feed", {}) or {}
    ai = cfg.get("aprs_is", {}) or {}
    msg = cfg.get("messaging", {}) or {}
    tx = msg.get("tx", {}) or {}
    return {
        "digi_callsign": cfg.get("digi", {}).get("callsign", ""),
        "digi_lat": cfg.get("digi", {}).get("lat"),
        "digi_lon": cfg.get("digi", {}).get("lon"),
        "local_feed_enabled": bool(lf.get("enabled")),
        "local_feed_host": lf.get("host", "") or "",
        "local_feed_port": lf.get("port"),
        "aprs_is_enabled": bool(ai.get("enabled", True)),
        "aprs_is_filter": ai.get("filter", "") or "",
        "messaging_my_callsign": msg.get("my_callsign", "") or "",
        "messaging_my_ssid": msg.get("my_ssid", 0),
        "messaging_tx_host": tx.get("host", "") or "",
        "messaging_tx_port": tx.get("port"),
    }


@app.get("/api/settings")
def api_get_settings():
    return _settings_view(config)


@app.post("/api/settings")
def api_save_settings(payload: dict = Body(...)):
    global config

    def s(key, default=""):
        v = payload.get(key, default)
        return v.strip() if isinstance(v, str) else v

    def port_or_none(v):
        if v in (None, "", "null"):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"Porta inválida: {v!r}")

    new_config = dict(config)
    new_config["digi"] = {
        "callsign": s("digi_callsign", config.get("digi", {}).get("callsign", "")).upper(),
        "lat": float(payload.get("digi_lat") or 0),
        "lon": float(payload.get("digi_lon") or 0),
    }
    local_host = s("local_feed_host")
    local_port = port_or_none(payload.get("local_feed_port"))
    new_config["local_feed"] = {
        "enabled": bool(payload.get("local_feed_enabled")) and bool(local_host) and bool(local_port),
        "host": local_host,
        "port": local_port,
    }
    digi_call = new_config["digi"]["callsign"]
    aprs_filter = s("aprs_is_filter") or (f"b/{digi_call}*" if digi_call else "")
    new_config["aprs_is"] = {
        **config.get("aprs_is", {}),
        "enabled": bool(payload.get("aprs_is_enabled", True)),
        "filter": aprs_filter,
    }
    tx_host = s("messaging_tx_host")
    tx_port = port_or_none(payload.get("messaging_tx_port"))
    new_config["messaging"] = {
        "my_callsign": s("messaging_my_callsign").upper(),
        "my_ssid": int(payload.get("messaging_my_ssid") or 0),
        "tx": {"host": tx_host, "port": tx_port},
    }

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(new_config, f, allow_unicode=True, sort_keys=False)

    config = new_config

    feeds = getattr(app.state, "feeds", None)
    if feeds:
        feeds.stop()
    new_feeds = AprsFeeds(config)
    new_feeds.start()
    app.state.feeds = new_feeds
    log.info("Configurações salvas e feeds reiniciados para %s", config["digi"]["callsign"])

    return {"ok": True, "settings": _settings_view(config)}


@app.get("/api/stats/summary")
def api_summary():
    return db.summary()


@app.get("/api/stats/timeseries")
def api_timeseries(direction: str | None = Query(None), days: int = Query(30, ge=1, le=365)):
    return db.timeseries(direction=direction, days=days)


@app.get("/api/rankings/most_heard")
def api_most_heard(direction: str | None = Query(None), limit: int = Query(20, ge=1, le=200)):
    return db.most_heard(direction=direction, limit=limit)


@app.get("/api/rankings/dx")
def api_dx(direction: str | None = Query(None), limit: int = Query(20, ge=1, le=200)):
    return db.dx_ranking(direction=direction, limit=limit)


@app.get("/api/search")
def api_search(
    q: str | None = Query(None),
    direction: str | None = Query(None),
    via: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
):
    return db.search(q=q, direction=direction, via=via, date_from=date_from, date_to=date_to, limit=limit)


@app.get("/api/history/dates")
def api_history_dates():
    return db.available_dates()


@app.get("/api/history/{date}")
def api_history_date(date: str):
    return db.history_by_date(date)


@app.get("/api/links/{station}")
def api_links(station: str):
    """Links prontos pra QRZ.com e aprs.fi, iguais aos usados no LAST HEARD do firmware."""
    base_call = station.split("-")[0].upper()
    return {
        "qrz": f"https://www.qrz.com/db/{base_call}",
        "aprsfi": f"https://aprs.fi/{station.upper()}",
    }


@app.get("/api/messages")
def api_messages(limit: int = Query(100, ge=1, le=500)):
    return db.list_messages(limit=limit)


@app.post("/api/messages/send")
def api_send_message(payload: dict = Body(...)):
    to_call = (payload.get("to") or "").strip().upper()
    text = (payload.get("text") or "").strip()
    if not to_call or not text:
        raise HTTPException(400, "Informe 'to' (indicativo) e 'text' (mensagem).")
    if len(text) > 60:
        raise HTTPException(400, "Mensagem muito longa (máx. ~60 caracteres, padrão APRS).")

    feeds = getattr(app.state, "feeds", None)
    sender = feeds.make_message_sender() if feeds else None
    if not sender:
        raise HTTPException(
            409,
            "Porta de TX não configurada em config.yaml (messaging.tx.host / messaging.tx.port).",
        )
    try:
        msgid = sender.send(to_call, text)
    except Exception as e:
        raise HTTPException(502, f"Falha ao enviar: {e}")

    db.insert_message(to_station=to_call, from_station=sender.my_callsign, text=text, msgid=msgid)
    return {"ok": True, "to": to_call, "text": text, "msgid": msgid}


if os.path.isdir(os.path.join(BASE_DIR, "static")):
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    web = config.get("web", {})
    uvicorn.run("app:app", host=web.get("host", "0.0.0.0"), port=web.get("port", 8080), reload=False)
