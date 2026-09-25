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



# q-constructs que realmente significam "um iGate de verdade gateou isso do
# RF pra internet" (qAR = client verificado, qAo = terceiro formato). qAC
# ("login direto no servidor") e variantes de servidor NÃO confirmam que
# alguém ouviu por rádio — é comum aparecer até no próprio uplink do digi.
_REAL_GATE_QCONSTRUCTS = {"QAR", "QAO"}

# Nomes que aparecem no caminho mas são infraestrutura da rede (servidores
# Tier-2, roteamento interno), não estações de rádio de verdade.
_INFRA_PREFIXES = ("TCPIP", "TCPXX", "APRS", "GATE", "SERVER", "NOCALL", "N0CALL", "T2", "3RD")


def looks_like_real_station(call: str) -> bool:
    call = (call or "").split("-")[0].upper().strip()
    if not call or len(call) < 3:
        return False
    return not any(call.startswith(p) for p in _INFRA_PREFIXES)


def extract_gate(path_list):
    """Acha o callsign do iGate que colocou o pacote na internet (após o
    q-construct) — só quando o q-construct indica um gate de verdade."""
    for i, hop in enumerate(path_list):
        h = hop.upper()
        if h.startswith("QA") and i + 1 < len(path_list):
            if h not in _REAL_GATE_QCONSTRUCTS:
                return None
            gate = path_list[i + 1].split("*")[0].upper()
            return gate if looks_like_real_station(gate) else None
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


def format_aprs_latlon(lat: float, lon: float) -> str:
    """DDMM.hhN/DDDMM.hhW - mesma conta do firmware (DD_DDDDDtoDDMMSS): graus
    inteiros + minutos.centesimos, nunca minutos/segundos."""
    lat_ns = "N" if lat >= 0 else "S"
    lon_ew = "E" if lon >= 0 else "W"
    lat = abs(lat)
    lon = abs(lon)
    lat_dd = int(lat)
    lat_mm = (lat - lat_dd) * 60
    lon_dd = int(lon)
    lon_mm = (lon - lon_dd) * 60
    return f"{lat_dd:02d}{lat_mm:05.2f}{lat_ns}", f"{lon_dd:03d}{lon_mm:05.2f}{lon_ew}"


def aprs_timestamp() -> str:
    """DDHHMMz em UTC - mesmo formato que getTimeStamp() no firmware."""
    return time.strftime("%d%H%M", time.gmtime()) + "z"


class MessageSender:
    """Abre uma conexão curta na porta de TX e manda um pacote APRS (mensagem,
    boletim/BLN-News - mesmo formato, só muda o endereço - ou Object Report)."""

    def __init__(self, host, port, my_callsign, my_ssid=0, path=None):
        self.host = host
        self.port = port
        self.my_callsign = my_callsign
        self.my_ssid = my_ssid
        self.path = path or [("WIDE1", 1), ("WIDE2", 1)]

    def _send_info(self, info: bytes, dest_call: str = "APRS") -> None:
        """Monta o quadro AX.25 (dest/src/path/control/pid/info), empacota em
        KISS e manda pela porta de TX. Reaproveitado por send()/send_object()
        - dest_call (TOCALL) é só identificador de software, não afeta se o
        pacote é reconhecido como mensagem/boletim/objeto (isso é o Data Type
        Identifier + endereço dentro do campo de informação)."""
        dest = encode_callsign(dest_call, 0, last=(len(self.path) == 0))
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

    def send(self, to_call: str, text: str) -> str:
        """Mensagem APRS de verdade (com ACK) OU boletim/News (endereço tipo
        "BLN1" ou "BLN5NEWS" - mesmo formato de pacote, o rádio que decide
        que é boletim pelo prefixo "BLN" do endereço, não por nada aqui)."""
        to_call = to_call.upper()
        if "-" in to_call:
            base, ssid_s = to_call.split("-", 1)
            addressee = f"{base}-{ssid_s}"
        else:
            addressee = to_call
        addressee_field = addressee.ljust(9)[:9]
        msgid = str(random.randint(1, 999))
        info = f":{addressee_field}:{text}{{{msgid}".encode("utf-8")
        self._send_info(info)
        log.info("Mensagem enviada: %s-%s -> %s: %r (msgid %s)", self.my_callsign, self.my_ssid, addressee, text, msgid)
        return msgid

    def send_object(self, name: str, lat: float, lon: float, table: str, symbol: str, comment: str) -> None:
        """Object Report (;NAME*TIMESTAMPlat/lonSYM+comment) - mesmo formato
        que sendAPRSObject() no firmware. name é só o rótulo no payload (não
        precisa bater com nenhum indicativo real); posição é a que você
        digitou, independente de onde o dashboard está rodando."""
        obj_name = (name or "").ljust(9)[:9]
        lat_str, lon_str = format_aprs_latlon(lat, lon)
        ts = aprs_timestamp()
        table_ch = (table or "/")[:1]
        symbol_ch = (symbol or "r")[:1]
        info = f";{obj_name}*{ts}{lat_str}{table_ch}{lon_str}{symbol_ch}{comment or ''}".encode("utf-8")
        self._send_info(info)
        log.info("Object enviado: %s @ %s,%s: %r", name, lat, lon, comment)


def detect_position(host, port, callsign, timeout=20):
    """
    Conecta rapidinho na porta KISS informada e escuta até achar um pacote
    de POSIÇÃO transmitido pelo próprio `callsign` (o beacon que o digi
    manda de si mesmo) — usado pelo botão "Detectar posição" da tela de
    Configurações, pra preencher lat/lon sem o usuário ter que digitar.
    Devolve {"lat", "lon", "source"} se achar, ou None se estourar o tempo
    sem ver nenhum beacon de posição desse indicativo.
    """
    callsign = (callsign or "").upper().strip()
    deadline = time.time() + timeout
    buf = bytearray()
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.settimeout(2)
            while time.time() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
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
                    if (unescaped[0] & 0x0F) != 0x00:
                        continue
                    decoded = decode_ax25_to_tnc2(unescaped[1:])
                    if not decoded:
                        continue
                    tnc2_line, _ = decoded
                    try:
                        packet = aprslib.parse(tnc2_line)
                    except (aprslib.ParseError, aprslib.UnknownFormat):
                        continue
                    src = (packet.get("from") or "").upper()
                    if callsign and src != callsign:
                        continue
                    lat = packet.get("latitude")
                    lon = packet.get("longitude")
                    if lat is not None and lon is not None:
                        return {"lat": lat, "lon": lon, "source": src}
    except OSError as e:
        return {"error": str(e)}
    return None


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
        else:  # outbound: tudo que chega pela rede pública (APRS-IS)
            if src == self.digi_call:
                # o PRÓPRIO digi visto por outro iGate — confirma até onde o
                # sinal dele chegou. Isso é o que alimenta o ranking DX.
                gate = extract_gate(path_list)
                if not gate or gate == self.digi_call:
                    return
                station = gate
                via = "rede (confirma alcance)"
                pos = db.get_position(gate)
            else:
                # tráfego regional: outras estações vistas na rede, sem
                # relação com o seu digi. Só aparece se o filtro do APRS-IS
                # estiver aberto além do seu indicativo (raio configurado em
                # Configurações). Entra no "Últimas estações escutadas" pra
                # dar volume de teste, mas NÃO conta pro ranking DX — aquele
                # continua sendo só "quem ouviu meu digi de verdade".
                if not looks_like_real_station(src):
                    return  # servidor/infra da rede, não é uma estação de verdade
                station = src
                via = "rede (regional)"
                pos = (lat, lon) if lat is not None else db.get_position(src)

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


class MsgScheduler(threading.Thread):
    """Fase 2: espelha a lógica da aba MSG do firmware (BLN Alerts, BLN News
    e Objects) aqui no dashboard, mandando pela mesma porta de TX usada pelo
    Chat Message. Config vem de config["msg_tab"] (bln: lista de 9 slots -
    bi 0-3 = Alerts BLN1-4, bi 4-8 = News BLN5NEWS-BLN9NEWS/rótulo NEWS5-9;
    objects: lista de 4 slots Object1-4) - mesmos nomes de campo em ambos os
    lados pra facilitar comparação com main.cpp/webservice.cpp do firmware.

    Estado de runtime (ativação, gap crescente, "sent") é só em memória e
    reseta a cada reinício do app - igual o firmware reseta esses contadores
    (não fazem parte da Configuration struct) a cada reboot do ESP32.
    """

    ALERT_OPTS = (300, 600, 900, 1800, 3600)  # 5/10/15/30/60 min - Alerts e News usam o mesmo dropdown
    OBJ_FIXED_OPTS = (900, 1800, 3600)  # 15/30/60 min
    ACTIVE_FOR_OPTS = (24, 36, 48, 72)

    def __init__(self, config, make_sender):
        super().__init__(name="msg-scheduler", daemon=True)
        self.config = config  # dict; msg_tab sub-dict é mutado in place (enabled/sent)
        self.make_sender = make_sender  # callable() -> MessageSender | None
        self.on_change = None  # callable(), chamado só quando enabled vira False (auto-disable) p/ persistir
        self._stop_evt = threading.Event()
        self._bln_activated_at = [0.0] * 9
        self._bln_news_gap = [0.0] * 9
        self._bln_news_first_sent = [False] * 9
        self._bln_next_send = [0.0] * 9
        self._obj_activated_at = [0.0] * 4
        self._obj_next_send = [0.0] * 4

    def stop(self):
        self._stop_evt.set()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("MsgScheduler: erro no ciclo, continuando")
            self._stop_evt.wait(1.0)

    @staticmethod
    def bln_label(bi: int) -> str:
        """Endereço real transmitido no ar - NEWS usa o formato Group Bulletin
        (BLN + dígito + nome do grupo) pra ser reconhecido como boletim em
        qualquer rádio, igual o firmware faz desde a v2.1-lu6jmf."""
        return f"BLN{bi + 1}" if bi < 4 else f"BLN{bi + 1}NEWS"

    @staticmethod
    def bln_ui_label(bi: int) -> str:
        """Rótulo só de exibição na UI - NEWS5-NEWS9, igual a tela do firmware."""
        return f"BLN{bi + 1}" if bi < 4 else f"NEWS{bi + 1}"

    def _tick(self):
        now = time.time()
        msg_tab = self.config.get("msg_tab") or {}
        for bi, slot in enumerate(msg_tab.get("bln") or []):
            if bi > 8:
                break
            self._tick_bln(bi, slot, now)
        for oi, slot in enumerate(msg_tab.get("objects") or []):
            if oi > 3:
                break
            self._tick_object(oi, slot, now)

    def _tick_bln(self, bi, slot, now):
        text = (slot.get("text") or "").strip()
        if not slot.get("enabled") or not text:
            self._bln_activated_at[bi] = 0
            if bi >= 4:
                self._bln_news_first_sent[bi] = False
                self._bln_news_gap[bi] = 0
            return

        if self._bln_activated_at[bi] == 0:
            self._bln_activated_at[bi] = now

        # 0/ausente = default 24h (unificado Alerts+News); nunca passa de 72h
        active_for_hours = slot.get("active_for") or 0
        if active_for_hours == 0:
            active_for_hours = 24
        if active_for_hours > 72:
            active_for_hours = 72

        if now - self._bln_activated_at[bi] >= active_for_hours * 3600:
            slot["enabled"] = False
            self._bln_activated_at[bi] = 0
            self._bln_news_first_sent[bi] = False
            self._bln_news_gap[bi] = 0
            log.info("%s (%s) atingiu o prazo Active-for (%dh), desativado", self.bln_label(bi), self.bln_ui_label(bi), active_for_hours)
            if self.on_change:
                self.on_change()
            return

        base_t = slot.get("interval") or 1800
        if base_t < 300:
            base_t = 300  # piso: 5 min mínimo, igual o firmware

        if bi < 4:
            # Alerts: intervalo fixo
            if now >= self._bln_next_send[bi]:
                if self._send_bln(bi, slot, text):
                    self._bln_next_send[bi] = now + base_t
        else:
            # News: gap crescente. msg1 imediato, depois +2T, +3T, +4T...
            if not self._bln_news_first_sent[bi]:
                if self._send_bln(bi, slot, text):
                    self._bln_news_first_sent[bi] = True
                    self._bln_news_gap[bi] = base_t * 2
                    self._bln_next_send[bi] = now + self._bln_news_gap[bi]
            elif now >= self._bln_next_send[bi]:
                if self._send_bln(bi, slot, text):
                    self._bln_news_gap[bi] += base_t
                    self._bln_next_send[bi] = now + self._bln_news_gap[bi]

    def _send_bln(self, bi, slot, text) -> bool:
        """Manda e atualiza o contador/limite. Devolve False sem avançar
        nenhum relógio se o envio falhar (porta de TX indisponível etc.) -
        assim tenta de novo no próximo ciclo (1s depois) em vez de pular o
        intervalo inteiro, igual ao retry-sem-bloquear que fizemos no firmware."""
        sender = self.make_sender()
        if not sender:
            log.warning("%s: porta de TX não configurada, envio pulado", self.bln_label(bi))
            return False
        addr = self.bln_label(bi)
        try:
            sender.send(addr, text)
        except Exception as e:
            log.warning("%s: falha ao enviar (%s)", addr, e)
            return False
        slot["sent"] = int(slot.get("sent") or 0) + 1
        log.info("%s (%s) enviado (#%d): %r", addr, self.bln_ui_label(bi), slot["sent"], text)
        limit = int(slot.get("limit") or 0)
        if limit > 0 and slot["sent"] >= limit:
            slot["enabled"] = False
            self._bln_activated_at[bi] = 0
            log.info("%s atingiu o limite de envios (%d), desativado", addr, limit)
            if self.on_change:
                self.on_change()
        return True

    def _tick_object(self, oi, slot, now):
        name = (slot.get("name") or "").strip()
        if not slot.get("enabled") or len(name) < 3:
            self._obj_activated_at[oi] = 0
            self._obj_next_send[oi] = 0
            slot["sent"] = 0
            return

        if self._obj_activated_at[oi] == 0:
            self._obj_activated_at[oi] = now

        permanent = bool(slot.get("permanent"))
        if not permanent:
            hours = slot.get("active_for") or 0
            if hours == 0 or hours > 72:
                hours = 72  # teto rígido - Objects não tem "default", só o teto
            if now - self._obj_activated_at[oi] >= hours * 3600:
                slot["enabled"] = False
                self._obj_activated_at[oi] = 0
                log.info("Object%d atingiu o prazo Active-for, desativado", oi + 1)
                if self.on_change:
                    self.on_change()
                return

        mode = int(slot.get("mode") or 0)
        if mode == 0:
            ivl = slot.get("interval") or 900
            if ivl < 900:
                ivl = 900
        else:
            ivl = 900  # modo Active-for: piso fixo de 15 min pro envio, o teto é o Active-for acima

        if now < self._obj_next_send[oi]:
            return

        sender = self.make_sender()
        if not sender:
            return
        try:
            sender.send_object(
                name,
                float(slot.get("lat") or 0.0),
                float(slot.get("lon") or 0.0),
                slot.get("table") or "/",
                slot.get("symbol") or "r",
                slot.get("text") or "",
            )
        except Exception as e:
            log.warning("Object%d: falha ao enviar (%s)", oi + 1, e)
            return  # tenta de novo no próximo ciclo, não avança o relógio

        self._obj_next_send[oi] = now + ivl
        slot["sent"] = int(slot.get("sent") or 0) + 1
        log.info("Object%d (%s) enviado (#%d)", oi + 1, name, slot["sent"])
        limit = int(slot.get("limit") or 0)
        if not permanent and mode == 0 and limit > 0 and slot["sent"] >= limit:
            slot["enabled"] = False
            self._obj_activated_at[oi] = 0
            log.info("Object%d atingiu o limite de envios (%d), desativado", oi + 1, limit)
            if self.on_change:
                self.on_change()
