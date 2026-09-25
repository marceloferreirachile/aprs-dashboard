"""Camada de banco de dados (SQLite) do APRS Dashboard."""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_local = threading.local()
_db_path = "aprs_dashboard.db"
_write_lock = threading.Lock()


def configure(path: str):
    global _db_path
    _db_path = path


def _get_conn():
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(_db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        _local.conn = conn
    return _local.conn


@contextmanager
def cursor():
    conn = _get_conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    finally:
        cur.close()


def init_db():
    with cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
                station TEXT NOT NULL,
                distance_km REAL,
                lat REAL,
                lon REAL,
                path TEXT,
                via TEXT,
                raw TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sightings_station ON sightings(station)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sightings_direction ON sightings(direction)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sightings_ts ON sightings(ts)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                station TEXT PRIMARY KEY,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                last_seen TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_sent TEXT NOT NULL,
                to_station TEXT NOT NULL,
                from_station TEXT NOT NULL,
                text TEXT NOT NULL,
                msgid TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'enviada',
                ts_acked TEXT
            )
            """
        )
        # Migração leve: bancos criados antes da coluna 'via' existir.
        cur.execute("PRAGMA table_info(sightings)")
        cols = {row["name"] for row in cur.fetchall()}
        if "via" not in cols:
            cur.execute("ALTER TABLE sightings ADD COLUMN via TEXT")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def update_position(station: str, lat: float, lon: float, ts: str = None):
    ts = ts or now_iso()
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO positions (station, lat, lon, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(station) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, last_seen=excluded.last_seen
            """,
            (station, lat, lon, ts),
        )


def get_position(station: str):
    with cursor() as cur:
        cur.execute("SELECT lat, lon FROM positions WHERE station = ?", (station,))
        row = cur.fetchone()
        return (row["lat"], row["lon"]) if row else None


def insert_sighting(direction, station, distance_km=None, lat=None, lon=None, path=None, via=None, raw=None, ts=None):
    ts = ts or now_iso()
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO sightings (ts, direction, station, distance_km, lat, lon, path, via, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, direction, station, distance_km, lat, lon, path, via, raw),
        )


def insert_message(to_station, from_station, text, msgid, ts=None):
    ts = ts or now_iso()
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (ts_sent, to_station, from_station, text, msgid, status)
            VALUES (?, ?, ?, ?, ?, 'enviada')
            """,
            (ts, to_station, from_station, text, msgid),
        )
        return cur.lastrowid


def insert_received_message(from_station, to_station, text, msgid, ts=None):
    """
    Mensagem de texto (não-ack) recebida por RF endereçada a nós. Sem isso,
    o dashboard só registrava mensagens que NÓS enviamos (e seus acks) —
    uma mensagem chegando de outra estação nunca aparecia na lista.
    Dedup simples por (from_station, msgid) pra não duplicar em cada retry
    do remetente.
    """
    ts = ts or now_iso()
    with cursor() as cur:
        if msgid:
            cur.execute(
                "SELECT id FROM messages WHERE from_station = ? AND msgid = ? AND status = 'recebida'",
                (from_station, msgid),
            )
            if cur.fetchone():
                return None
        cur.execute(
            """
            INSERT INTO messages (ts_sent, to_station, from_station, text, msgid, status)
            VALUES (?, ?, ?, ?, ?, 'recebida')
            """,
            (ts, to_station, from_station, text, msgid or ""),
        )
        return cur.lastrowid


def mark_message_acked(acker_station, msgid, ts=None):
    """
    Chamado quando um pacote 'ackNNN' chega de volta. 'acker_station' é quem
    mandou o ack (ou seja, o destinatário original da nossa mensagem) — marca
    a mensagem que NÓS enviamos pra essa estação com esse msgid.
    """
    ts = ts or now_iso()
    with cursor() as cur:
        cur.execute(
            """
            UPDATE messages SET status = 'confirmada (ack)', ts_acked = ?
            WHERE to_station = ? AND msgid = ? AND status = 'enviada'
            """,
            (ts, acker_station, msgid),
        )
        return cur.rowcount


def list_messages(limit=100):
    with cursor() as cur:
        cur.execute("SELECT * FROM messages ORDER BY ts_sent DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def summary():
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM sightings")
        total = cur.fetchone()["c"]
        cur.execute("SELECT direction, COUNT(*) c, COUNT(DISTINCT station) s FROM sightings GROUP BY direction")
        by_dir = {r["direction"]: {"packets": r["c"], "stations": r["s"]} for r in cur.fetchall()}
        cur.execute("SELECT MAX(ts) t FROM sightings")
        last = cur.fetchone()["t"]
    return {"total_packets": total, "by_direction": by_dir, "last_seen": last}


def timeseries(direction=None, days=30):
    q = """
        SELECT substr(ts,1,10) day, COUNT(*) c
        FROM sightings
        WHERE ts >= datetime('now', ?)
    """
    params = [f"-{days} days"]
    if direction:
        q += " AND direction = ?"
        params.append(direction)
    q += " GROUP BY day ORDER BY day"
    with cursor() as cur:
        cur.execute(q, params)
        return [{"day": r["day"], "count": r["c"]} for r in cur.fetchall()]


def most_heard(direction=None, limit=20):
    q = "SELECT station, COUNT(*) c, MAX(ts) last FROM sightings WHERE 1=1"
    params = []
    if direction:
        q += " AND direction = ?"
        params.append(direction)
        # No sentido "outbound", tráfego regional (não é sobre o seu digi)
        # não deve poluir esse ranking — só quem de fato ouviu seu digi.
        if direction == "outbound":
            q += " AND (via IS NULL OR via != 'rede (regional)')"
    q += " GROUP BY station ORDER BY c DESC LIMIT ?"
    params.append(limit)
    with cursor() as cur:
        cur.execute(q, params)
        return [{"station": r["station"], "packets": r["c"], "last_seen": r["last"]} for r in cur.fetchall()]


def dx_ranking(direction=None, limit=20):
    q = """
        SELECT station, MAX(distance_km) best_km, MIN(ts) first_seen, MAX(ts) last_seen, COUNT(*) c
        FROM sightings
        WHERE distance_km IS NOT NULL
    """
    params = []
    if direction:
        q += " AND direction = ?"
        params.append(direction)
        if direction == "outbound":
            q += " AND (via IS NULL OR via != 'rede (regional)')"
    q += " GROUP BY station ORDER BY best_km DESC LIMIT ?"
    params.append(limit)
    with cursor() as cur:
        cur.execute(q, params)
        return [
            {
                "station": r["station"],
                "best_km": round(r["best_km"], 2),
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "packets": r["c"],
            }
            for r in cur.fetchall()
        ]


def search(q=None, direction=None, via=None, date_from=None, date_to=None, limit=200):
    sql = "SELECT * FROM sightings WHERE 1=1"
    params = []
    if q:
        sql += " AND station LIKE ?"
        params.append(f"%{q.upper()}%")
    if direction:
        sql += " AND direction = ?"
        params.append(direction)
    if via:
        sql += " AND via = ?"
        params.append(via)
    if date_from:
        sql += " AND ts >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND ts <= ?"
        params.append(date_to + "T23:59:59")
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def recent(direction=None, page=1, page_size=50):
    """Pacotes mais recentes, paginado — estilo 'LAST HEARD' do firmware do digi."""
    offset = (page - 1) * page_size
    sql = "SELECT * FROM sightings WHERE 1=1"
    params = []
    if direction:
        sql += " AND direction = ?"
        params.append(direction)
    sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
    params += [page_size, offset]
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def recent_count(direction=None):
    sql = "SELECT COUNT(*) c FROM sightings WHERE 1=1"
    params = []
    if direction:
        sql += " AND direction = ?"
        params.append(direction)
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()["c"]


def history_by_date(date: str):
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM sightings WHERE substr(ts,1,10) = ? ORDER BY ts",
            (date,),
        )
        return [dict(r) for r in cur.fetchall()]


def available_dates():
    with cursor() as cur:
        cur.execute("SELECT DISTINCT substr(ts,1,10) day FROM sightings ORDER BY day DESC")
        return [r["day"] for r in cur.fetchall()]
