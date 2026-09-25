import logging
import os
import shutil
import sys

import yaml
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import db
from aprs_client import AprsFeeds, MsgScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

# Bump isso a cada release (tem que bater com a tag "vX.Y.Z" no GitHub) — é o
# que a aba "Sobre" usa pra comparar com a última Release e avisar de
# atualização disponível.
APP_VERSION = "1.1.0"
GITHUB_REPO = "marceloferreirachile/aprs-dashboard"

# BASE_DIR: onde ficam os arquivos empacotados (templates, config.yaml.example)
# — read-only quando rodando como executável (PyInstaller).
# DATA_DIR: onde gravamos config.yaml e o banco — precisa ser gravável E
# sobreviver a atualizações (baixar um app novo não pode apagar o histórico).
#
# No Windows/Linux o executável é um arquivo solto, então "do lado dele"
# funciona. No macOS, porém, o executável fica DENTRO do bundle .app
# (Contents/MacOS/...) — gravar dados ali significa que cada novo download
# vem com um .app diferente e o histórico do antigo fica preso lá dentro,
# perdido. Por isso usamos a pasta padrão de dados de app de cada sistema
# (a mesma ideia em todos: um lugar fixo, fora do próprio executável).
def _default_data_dir():
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, "APRSDashboard")
    os.makedirs(path, exist_ok=True)
    return path


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    DATA_DIR = _default_data_dir()
else:
    DATA_DIR = BASE_DIR
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml")


def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


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


def _ensure_msg_tab_defaults(cfg):
    """config.yaml de quem já usava o app antes da Fase 2 não tem "msg_tab" -
    preenche com os defaults do example, sem tocar em nada que já existia."""
    if not cfg.get("msg_tab"):
        example_path = os.path.join(BASE_DIR, "config.yaml.example")
        with open(example_path, "r", encoding="utf-8") as f:
            example = yaml.safe_load(f) or {}
        cfg["msg_tab"] = example.get("msg_tab", {"bln": [], "objects": []})
    return cfg


config = load_config()
config = _ensure_msg_tab_defaults(config)
db.configure(os.path.join(DATA_DIR, config.get("database", {}).get("path", "aprs_dashboard.db")))
db.init_db()

app = FastAPI(title="APRS Digipeater Dashboard")


def _make_message_sender():
    """Não usa feeds.make_message_sender diretamente (bound method) porque
    /api/settings troca o objeto feeds inteiro quando a config muda - olhando
    sempre app.state.feeds na hora da chamada, o MsgScheduler nunca fica
    preso numa conexão/config velha."""
    feeds = getattr(app.state, "feeds", None)
    return feeds.make_message_sender() if feeds else None


def _save_config_to_disk():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


@app.on_event("startup")
def startup():
    feeds = AprsFeeds(config)
    feeds.start()
    app.state.feeds = feeds

    scheduler = MsgScheduler(config, make_sender=_make_message_sender)
    scheduler.on_change = _save_config_to_disk
    scheduler.start()
    app.state.msg_scheduler = scheduler

    log.info("Feeds e MsgScheduler iniciados para %s", config["digi"]["callsign"])


@app.on_event("shutdown")
def shutdown():
    feeds = getattr(app.state, "feeds", None)
    if feeds:
        feeds.stop()
    scheduler = getattr(app.state, "msg_scheduler", None)
    if scheduler:
        scheduler.stop()


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


@app.get("/api/version")
def api_version():
    return {"version": APP_VERSION, "github_repo": GITHUB_REPO}


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
        "aprs_is_radius_km": ai.get("radius_km", 20),
        "messaging_my_callsign": msg.get("my_callsign", "") or "",
        "messaging_my_ssid": msg.get("my_ssid", 0),
        "messaging_tx_host": tx.get("host", "") or "",
        "messaging_tx_port": tx.get("port"),
    }


@app.get("/api/settings")
def api_get_settings():
    return _settings_view(config)


@app.post("/api/detect_position")
def api_detect_position(payload: dict = Body(...)):
    """
    Conecta rapidinho na porta KISS informada e tenta achar sozinho o
    beacon de posição do próprio digi, pra preencher lat/lon sem o usuário
    ter que descobrir e digitar manualmente.
    """
    from aprs_client import detect_position

    host = (payload.get("host") or "").strip()
    callsign = (payload.get("callsign") or "").strip().upper()
    if not host or not callsign:
        raise HTTPException(400, "Informe o indicativo e o IP do digi.")
    try:
        port = int(payload.get("port"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Porta inválida.")

    result = detect_position(host, port, callsign, timeout=20)
    if not result:
        raise HTTPException(
            408,
            "Não recebi nenhum pacote de posição desse indicativo em 20s. "
            "O digi pode não estar enviando beacon próprio — preencha manualmente.",
        )
    if "error" in result:
        raise HTTPException(502, f"Erro conectando no digi: {result['error']}")
    return result


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
    digi_lat = new_config["digi"]["lat"]
    digi_lon = new_config["digi"]["lon"]
    try:
        radius_km = int(payload.get("aprs_is_radius_km") or 20)
    except (TypeError, ValueError):
        radius_km = 20
    # b/CALL* sempre garante ver a confirmação de alcance do próprio digi
    # (usada no ranking DX). r/lat/lon/raio, somado com um espaço, é "OU" no
    # protocolo APRS-IS — soma tráfego regional de outras estações também,
    # só quando já temos a posição do digi (senão não tem centro pro raio).
    # Equivalente ao "Server Filter" da aba IGATE do próprio digi, só que
    # essa é a conexão do DASHBOARD com a rede — independente da dele.
    default_filter = f"b/{digi_call}*" if digi_call else ""
    if digi_lat and digi_lon:
        default_filter = (default_filter + f" r/{digi_lat}/{digi_lon}/{radius_km}").strip()
    aprs_filter = s("aprs_is_filter") or default_filter
    new_config["aprs_is"] = {
        **config.get("aprs_is", {}),
        "enabled": bool(payload.get("aprs_is_enabled", True)),
        "filter": aprs_filter,
        "radius_km": radius_km,
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


@app.get("/api/last_heard")
def api_last_heard(
    direction: str | None = Query(None),
    page: int = Query(1, ge=1, le=10),
    page_size: int = Query(50, ge=1, le=50),
):
    """Pacotes mais recentes, paginado — igual ao painel LAST HEARD do firmware do digi."""
    total = db.recent_count(direction=direction)
    rows = db.recent(direction=direction, page=page, page_size=page_size)
    pages = max(1, min(10, -(-total // page_size)))
    return {"rows": rows, "page": page, "page_size": page_size, "total": total, "pages": pages}


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

    from_display = f"{sender.my_callsign}-{sender.my_ssid}" if sender.my_ssid else sender.my_callsign
    db.insert_message(to_station=to_call, from_station=from_display, text=text, msgid=msgid)
    return {"ok": True, "to": to_call, "text": text, "msgid": msgid}


# --- Fase 2: BLN Alerts/News + Objects (mesma estrutura/lógica do firmware,
# enviado por aqui pela porta de TX) -----------------------------------------
@app.get("/api/msg_tab")
def api_get_msg_tab():
    return config.get("msg_tab", {"bln": [], "objects": []})


@app.post("/api/msg_tab")
def api_save_msg_tab(payload: dict = Body(...)):
    bln_in = payload.get("bln")
    obj_in = payload.get("objects")
    if not isinstance(bln_in, list) or len(bln_in) != 9:
        raise HTTPException(400, "'bln' precisa ser uma lista com 9 posições (BLN1-4 Alerts + NEWS5-9).")
    if not isinstance(obj_in, list) or len(obj_in) != 4:
        raise HTTPException(400, "'objects' precisa ser uma lista com 4 posições (Object1-4).")

    msg_tab = config.setdefault("msg_tab", {"bln": [], "objects": []})
    bln_out = []
    for bi, incoming in enumerate(bln_in):
        prev = msg_tab["bln"][bi] if bi < len(msg_tab.get("bln", [])) else {}
        was_enabled = bool(prev.get("enabled"))
        slot = {
            "enabled": bool(incoming.get("enabled")),
            "text": str(incoming.get("text") or "")[:67],
            "interval": _safe_int(incoming.get("interval"), 1800),
            "limit": _safe_int(incoming.get("limit"), 0),
            "active_for": _safe_int(incoming.get("active_for"), 0),
            "sent": 0 if (not was_enabled and incoming.get("enabled")) else int(prev.get("sent") or 0),
        }
        bln_out.append(slot)
    msg_tab["bln"] = bln_out

    obj_out = []
    for oi, incoming in enumerate(obj_in):
        prev = msg_tab["objects"][oi] if oi < len(msg_tab.get("objects", [])) else {}
        was_enabled = bool(prev.get("enabled"))
        slot = {
            "enabled": bool(incoming.get("enabled")),
            "name": str(incoming.get("name") or "")[:9],
            "lat": float(incoming.get("lat") or 0.0),
            "lon": float(incoming.get("lon") or 0.0),
            "table": str(incoming.get("table") or "/")[:1],
            "symbol": str(incoming.get("symbol") or "r")[:1],
            "text": str(incoming.get("text") or "")[:43],
            "mode": _safe_int(incoming.get("mode"), 0),
            "interval": _safe_int(incoming.get("interval"), 900),
            "limit": _safe_int(incoming.get("limit"), 0),
            "active_for": _safe_int(incoming.get("active_for"), 0),
            "permanent": bool(incoming.get("permanent")),
            "sent": 0 if (not was_enabled and incoming.get("enabled")) else int(prev.get("sent") or 0),
        }
        obj_out.append(slot)
    msg_tab["objects"] = obj_out

    _save_config_to_disk()
    return {"ok": True, "msg_tab": msg_tab}


if os.path.isdir(os.path.join(BASE_DIR, "static")):
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def find_free_port(host, preferred_port, tries=15):
    """Se a porta preferida já estiver em uso por outro programa (Tomcat,
    outro servidor de dev, etc — 8080 é bem disputada), tenta as próximas
    (preferred, preferred+1, ...) até achar uma livre, em vez de simplesmente
    falhar ao abrir."""
    import socket as _socket

    bind_host = host if host not in ("0.0.0.0", "") else "127.0.0.1"
    for port in range(preferred_port, preferred_port + tries):
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            try:
                s.bind((bind_host, port))
                return port
            except OSError:
                continue
    return preferred_port  # nenhuma livre encontrada — deixa o uvicorn dar o erro real


if __name__ == "__main__":
    import uvicorn

    web = config.get("web", {})
    host = web.get("host", "0.0.0.0")
    preferred_port = web.get("port", 8080)
    port = find_free_port(host, preferred_port)
    if port != preferred_port:
        log.warning("Porta %s ocupada por outro programa — usando %s no lugar.", preferred_port, port)
    uvicorn.run("app:app", host=host, port=port, reload=False)
