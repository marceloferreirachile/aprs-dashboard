"""
Listener UDP para o log de diagnostico do LU6JMF-10 (ESP32APRS_CDU).

O firmware (a partir da build "2.1.3-netlog") manda cada linha de log
(as mesmas que ja saiam pelo serial USB, incluindo os log_d() de fila/RF/TX)
como um pacote UDP em BROADCAST na porta 9999 - sem precisar saber o IP
deste dashboard, e sem precisar que o dashboard saiba o IP do radio.

Uso standalone (so pra ver o log ao vivo no terminal):
    python3 esp32_log_listener.py

Uso embutido no aprs-dashboard (app.py):
    from esp32_log_listener import start_listener
    start_listener(on_line=lambda line, ip: log.info("[ESP32 %s] %s", ip, line))
    # roda numa thread daemon separada, nao bloqueia o resto do app
"""
import socket
import threading
from datetime import datetime

UDP_PORT = 9999
BUFFER_SIZE = 512


def _listen_loop(on_line, stop_event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Escuta em todas as interfaces - recebe o broadcast independente
    # de qual IP este Mac tiver hoje (DHCP pode mudar, nao importa).
    sock.bind(("0.0.0.0", UDP_PORT))
    sock.settimeout(1.0)  # permite checar stop_event periodicamente

    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
        except socket.timeout:
            continue
        except OSError:
            break
        line = data.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            continue
        on_line(line, addr[0])

    sock.close()


def start_listener(on_line=None):
    """Inicia o listener numa thread daemon separada (nao bloqueia).

    on_line: callback(line: str, source_ip: str) chamado a cada linha
    recebida. Se omitido, imprime no stdout com timestamp.
    """
    if on_line is None:
        def on_line(line, source_ip):
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] ({source_ip}) {line}")

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_listen_loop,
        args=(on_line, stop_event),
        daemon=True,
        name="esp32-log-listener",
    )
    thread.start()
    return stop_event  # chame .set() pra parar, se precisar


if __name__ == "__main__":
    print(f"Escutando log do LU6JMF-10 via UDP broadcast na porta {UDP_PORT}...")
    print("Ctrl+C para sair.\n")
    stop = start_listener()
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        stop.set()
        print("\nEncerrado.")
