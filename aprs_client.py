"""
Clientes TCP/IP que alimentam o dashboard com pacotes APRS reais.

Três canais, porque cada um faz um trabalho diferente:

- RX LOCAL (inbound): conecta direto na porta TCP KISS do digi na rede local
  (confirmado com o hardware real do LU6JMF-10 — ESP32APRS_CDU expõe KISS
  binário nas portas 8001/8002, sem senha). Cada frame é um pacote AX.25 que
  o digi ouviu de verdade por RF -> "quem o digi ouviu", com o path original
  (dá pra ver se veio DIRETO da estação ou REPETIDO por outro digipeater).

- APRS-IS público (outbound): só a rede ampla (outros iGates) sabe dizer
  quem ouviu o SEU digi por RF — não tem como descobrir isso só localmente.
  Esse canal fala o protocolo texto padrão APRS-IS (login + linhas TNC2).

- TX LOCAL (envio de mensagem): conecta numa porta KISS separada (pode ser
  a mesma porta da RX ou outra, dependendo de como você configurar o digi)
  só quando precisa mandar uma mensagem — abre, manda o frame, fecha.
  Formato confirmado em teste real: ":ENDERECO  :texto{msgid" (não um
  pacote de status/posição solto — isso não é reconhecido como mensagem).
"""
import logging
import random
import socket
import threading
import time

import aprslib

import db

log = logging.getLogger("aprs_client")

EARTH_RADIUS_KM = 6371.0088

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD


def haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, sqrt, atan2

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return EARTH_RADIUS_KM * 2 * atan2(sqrt(a), sqrt(1 - a))


def extract_gate(path_list):
    """Acha o callsign do iGate que colocou o pacote na internet (após o q-construct)."""
    for i, hop in enumerate(path_list):
        if hop.upper().startswith("QA") and i + 1 < len(path_list):
            return path_list[i + 1].split("*")[0].upper()
    return None


def classify_via(path_list):
    """
    Classifica um pacote INBOUND como 'direto' (ouvido direto da estação
    origem, sem nenhum digipeater no meio) ou 'repetido' (algum digipeater
    real -- não um alias WIDEn-N não consumido -- já tinha repetido o
    pacote antes de chegar no nosso digi).
    """
    if not path_list:
        return "direto"
    for hop in path_list:
        used = hop.endswith("*")
        base = hop.rstrip("*").upper()
        is_generic_alias = base.startswith("WIDE") or base.startswith("TRACE") or base.startswith("RELAY")
        if used and not is_generic_alias:
            return "repetido"
    return "direto"


# --------------------------------------------------------------------------
# KISS / AX.25 — decodificação (RX) e codificação (TX)
# --------------------------------------------------------------------------

def kiss_escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(b)
    return bytes(out)


def kiss_unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == FESC and i + 1 < len(data):
            nxt = data[i + 1]
            if nxt == TFEND:
                out.append(FEND)
                i += 2
                continue
            elif nxt == TFESC:
                out.append(FESC)
                i += 2
                continue
        out.append(b)
        i += 1
    return bytes(out)


def encode_callsign(callsign: str, ssid: int, last: bool) -> bytes:
    call = callsign.upper().strip()[:6].ljust(6)
    addr = bytearray(ord(ch) << 1 for ch in call)
    ssid_byte = 0b01100000 | ((ssid & 0x0F) << 1) | (1 if last else 0)
    addr.append(ssid_byte)
    return bytes(addr)


def build_kiss_frame(ax25_payload: bytes, port: int = 0) -> bytes:
    kiss_cmd = bytes([(port & 0x0F) << 4])
    escaped = kiss_escape(kiss_cmd + ax25_payload)
    return bytes([FEND]) + escaped + bytes([FEND])


def decode_ax25_to_tnc2(frame: bytes):
    """
    Decodifica um frame AX.25 UI cru (já sem o byte de comando KISS) pro
    formato texto TNC2 ("ORIGEM>DEST,PATH:info") que o resto do pipeline
    (aprslib) já sabe processar. Retorna None se não parecer um frame válido.
    """
    if len(frame) < 15:
        return None
    try:
        addrs = []
        pos = 0
        while pos + 7 <= len(frame):
            addr_bytes = frame[pos:pos + 7]
            call = "".join(chr(b >> 1) for b in addr_bytes[:6]).strip()
            ssid_byte = addr_bytes[6]
            ssid = (ssid_byte >> 1) & 0x0F
            used = bool(ssid_byte & 0x80)
            last = bool(ssid_byte & 0x01)
            full = f"{call}-{ssid}" if ssid else call
            addrs.append((full, used))
            pos += 7
            if last:
                break
        else:
            return None  # nunca achou o bit 'last' -> frame corrompido

        if len(addrs) < 2:
            return None

        dest = addrs[0][0]
        src = addrs[1][0]
        path_hops = []
        for call, used in addrs[2:]:
            path_hops.append(call + ("*" if used else ""))

        if pos + 2 > len(frame):
            return None
        control = frame[pos]
        pid = frame[pos + 1]
        if control != 0x03:
            return None  # só nos interessam frames UI
        info = frame[pos + 2:]

        path_str = ",".join(path_hops)
        header = f"{src}>{dest}" + (f",{path_str}" if path_str else "")
        info_str = info.decode("utf-8", errors="replace")
        return f"{header}:{info_str}", path_hops
    except Exception:
        return None


class _KissLocalFeed(threading.Thread):
    """Conecta na porta TCP KISS do digi (rede local) e decodifica cada frame RX real."""

    def __init__(self, name, host, port, on_tnc2):
        super().__init__(daemon=True, name=name)
        self.host = host
        self.port = port
        self.on_tnc2 = on_tnc2
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        backoff = 5
        while not self._stop.is_set():
            try:
                log.info("[%s] conectando (KISS) em %s:%s ...", self.name, self.host, self.port)
                with socket.create_connection((self.host, self.port), timeout=30) as sock:
                    sock.settimeout(60)
                    log.info("[%s] conectado", self.name)
                    backoff = 5
                    buf = bytearray()
                    while not self._stop.is_set():
                        chunk = sock.recv(4096)
                        if not chunk:
                            raise ConnectionError("conexão fechada pelo servidor")
                        buf += chunk
                        while FEND in buf:
                            first = buf.find(FEND)
                            second = buf.find(FEND, first + 1)
                            if second == -1:
                                break
                            raw = bytes(buf[first + 1:second])
                            del buf[:second + 1]
                            if len(raw) < 2:
                                continue
                            unescaped = kiss_unescape(raw)
                            cmd = unescaped[0] & 0x0F
                            if cmd != 0x00:
                                continue  # só nos interessam frames de dados
                            ax25 = unescaped[1:]
                            try:
                                self.on_tnc2(ax25)
                            except Exception:
                                log.exception("[%s] erro processando frame KISS", self.name)
            except Exception as e:
                log.warning("[%s] desconectado (%s), tentando de novo em %ss", self.name, e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)


class _AprsIsFeed(threading.Thread):
    """Conexão TCP com a rede pública APRS-IS (protocolo texto, com login)."""

    def __init__(self, name, host, port, login_callsign, passcode, on_line, aprs_filter=None):
        super().__init__(daemon=True, name=name)
        self.host = host
        self.port = port
        self.login_callsign = login_callsign
        self.passcode = passcode
        self.aprs_filter = aprs_filter
        self.on_line = on_line
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        backoff = 5
        while not self._stop.is_set():
            try:
                log.info("[%s] conectando em %s:%s ...", self.name, self.host, self.port)
                with socket.create_connection((self.host, self.port), timeout=30) as sock:
                    sock.settimeout(60)
                    login = f"user {self.login_callsign} pass {self.passcode} vers PyAprsDash 1.0"
                    if self.aprs_filter:
                        login += f" filter {self.aprs_filter}"
                    login += "\r\n"
                    sock.sendall(login.encode("ascii", errors="ignore"))
                    log.info("[%s] conectado, login enviado", self.name)
                    backoff = 5
                    buf = b""
                    while not self._stop.is_set():
                        chunk = sock.recv(4096)
                        if not chunk:
                            raise ConnectionError("conexão fechada pelo servidor")
                        buf += chunk
                        while b"\r\n" in buf or b"\n" in buf:
                            sep = b"\r\n" if b"\r\n" in buf else b"\n"
                            line, buf = buf.split(sep, 1)
                            line = line.decode("utf-8", errors="ignore").strip()
                            if not line or line.startswith("#"):
                                continue
                            try:
                                self.on_line(line)
                            except Exception:
                                log.exception("[%s] erro processando linha: %s", self.name, line)
            except Exception as e:
                log.warning("[%s] desconectado (%s), tentando de novo em %ss", self.name, e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)


class MessageSender:
    """Abre uma conexão curta na porta de TX e manda uma mensagem APRS."""

    def __init__(self, host, port, my_callsign, my_ssid=0, path=None):
        self.host = host
        self.port = port
        self.my_callsign = my_callsign
        self.my_ssid = my_ssid
        self.path = path or [("WIDE1", 1), ("WIDE2", 1)]

    def send(self, to_call: str, text: str) -> str:
        to_call = to_call.upper()
        if "-" in to_call:
            base, ssid_s = to_call.split("-", 1)
            addressee = f"{base}-{ssid_s}"
        else:
            addressee = to_call
        addressee_field = addressee.ljust(9)[:9]
        msgid = str(random.randint(1, 999))
        info = f":{addressee_field}:{text}{{{msgid}".encode("utf-8")

        dest = encode_callsign("APRS", 0, last=(len(self.path) == 0))
        src = encode_callsign(self.my_callsign, self.my_ssid, last=(len(self.path) == 0))
        frame = bytearray()
        frame += dest
        frame += src
        for idx, (pcall, pssid) in enumerate(self.path):
            frame += encode_callsign(pcall, pssid, last=(idx == len(self.path) - 1))
        frame.append(0x03)
        frame.append(0xF0)
        frame += info

        kiss_frame = build_kiss_frame(bytes(frame))
        with socket.create_connection((self.host, self.port), timeout=10) as sock:
            sock.sendall(kiss_frame)
        log.info("Mensagem enviada: %s-%s -> %s: %r (msgid %s)", self.my_callsign, self.my_ssid, addressee, text, msgid)
        return msgid


class AprsFeeds:
    def __init__(self, config):
        self.config = config
        self.digi_call = config["digi"]["callsign"].upper()
        self.digi_lat = config["digi"]["lat"]
        self.digi_lon = config["digi"]["lon"]
        self.my_callsign = config.get("messaging", {}).get("my_callsign", self.digi_call.split("-")[0])
        self._threads = []

    def _handle_line(self, direction, line):
        try:
            packet = aprslib.parse(line)
        except (aprslib.ParseError, aprslib.UnknownFormat):
            return

        src = packet.get("from", "").upper()
        path_list = [p.upper() for p in packet.get("path", [])]
        lat = packet.get("latitude")
        lon = packet.get("longitude")

        if lat is not None and lon is not None:
            db.update_position(src, lat, lon)

        # Mensagem endereçada a nós? Ver se é um ACK pendente.
        # aprslib usa 'response'/'msgNo' pra ACK (":ENDERECO:ackNNN"), e
        # 'message_text'/'msgNo' pra mensagem de texto normal.
        if packet.get("format") == "message":
            to_call = (packet.get("addresse") or packet.get("to") or "").upper().strip()
            is_ack = packet.get("response") == "ack"
            msgid = str(packet.get("msgNo") or "").strip()
            if is_ack and to_call and to_call.split("-")[0] == self.my_callsign.split("-")[0] and msgid:
                db.mark_message_acked(acker_station=src, msgid=msgid)

        via = None
        if direction == "inbound":
            if src == self.digi_call:
                return  # é o próprio digi transmitindo, não é "algo que ele ouviu"
            station = src
            via = classify_via(path_list)
            pos = (lat, lon) if lat is not None else db.get_position(station)
        else:  # outbound: procuramos o beacon do PRÓPRIO digi visto por outro iGate
            if src != self.digi_call:
                return
            gate = extract_gate(path_list)
            if not gate or gate == self.digi_call:
                return
            station = gate
            via = "rede (APRS-IS)"
            pos = db.get_position(gate)

        distance = None
        plat = plon = None
        if pos and pos[0] is not None and pos[1] is not None:
            plat, plon = pos
            distance = haversine_km(self.digi_lat, self.digi_lon, plat, plon)

        db.insert_sighting(
            direction=direction,
            station=station,
            distance_km=distance,
            lat=plat,
            lon=plon,
            path=",".join(path_list),
            via=via,
            raw=line,
        )

    def _handle_kiss_frame(self, ax25_bytes):
        decoded = decode_ax25_to_tnc2(ax25_bytes)
        if not decoded:
            return
        tnc2_line, _path_hops = decoded
        self._handle_line("inbound", tnc2_line)

    def start(self):
        cfg = self.config
        lf = cfg.get("local_feed", {})
        if lf.get("enabled"):
            t = _KissLocalFeed(
                "inbound-kiss-local",
                lf["host"],
                lf["port"],
                on_tnc2=self._handle_kiss_frame,
            )
            t.start()
            self._threads.append(t)
        else:
            log.warning("local_feed desabilitado no config.yaml — sem captura inbound em tempo real")

        ai = cfg.get("aprs_is", {})
        if ai.get("enabled"):
            t = _AprsIsFeed(
                "outbound-aprsis",
                ai["host"],
                ai["port"],
                ai.get("login_callsign", "N0CALL"),
                ai.get("passcode", "-1"),
                aprs_filter=ai.get("filter"),
                on_line=lambda line: self._handle_line("outbound", line),
            )
            t.start()
            self._threads.append(t)
        else:
            log.warning("aprs_is desabilitado no config.yaml — sem captura outbound")

    def stop(self):
        for t in self._threads:
            t.stop()

    def make_message_sender(self):
        msg_cfg = self.config.get("messaging", {})
        tx = msg_cfg.get("tx", {})
        if not tx.get("host") or not tx.get("port"):
            return None
        return MessageSender(
            host=tx["host"],
            port=tx["port"],
            my_callsign=msg_cfg.get("my_callsign", self.digi_call.split("-")[0]),
            my_ssid=msg_cfg.get("my_ssid", 0),
        )
