# APRS Digipeater Dashboard

A local dashboard for monitoring an APRS digipeater/iGate: full history in a
local database, charts, most-heard station rankings, DX rankings (inbound
and outbound), direct-vs-relayed classification with the AX.25 path for
every packet, ready-made links to QRZ.com and aprs.fi, search, history by
date, and sending/receiving APRS messages with ACK confirmation.

Built so anyone with a digi on their own local network can use it — each
person runs their own copy and points it at their own hardware and
callsign. No account, no cloud service, no data leaving your network.

## Download

**[Get it here](https://marceloferreirachile.github.io/aprs-dashboard/)** —
pick your operating system (Windows, macOS, or Linux), download the file,
and run it. No Python, no terminal, no dependencies to install.

The app opens a browser window at `http://localhost:8080` and everything —
your digi's IP address, ports, callsign — is configured from the
**Settings** tab inside the dashboard itself.

## How it works

The dashboard uses **three separate TCP/IP connections**, because each one
does a job the others can't:

1. **Local RX — "who the digi heard" (inbound)**: connects over your local
   WiFi network directly to the digi's **KISS** TCP port. Every frame is a
   real AX.25 packet the digi heard over RF, with its original path — so
   the dashboard can tell whether a station was heard **directly** or
   **relayed** through another digipeater before reaching yours.
2. **Local TX — sending a message**: a **separate** port from RX (e.g. a
   second KISS port on the same digi), used only when sending a message
   from the dashboard.
3. **Outbound — "who heard the digi"**: only the public **APRS-IS**
   network can answer this, since it requires seeing your digi's beacon
   reported by another iGate elsewhere. The app connects (filtered to your
   own callsign) to the public `rotate.aprs2.net` network for this.

If your digi doesn't speak binary KISS (e.g. it's running different
software that only speaks plain APRS-IS/TNC2 text), open an issue — the
parser can be adapted.

## Publishing on your own domain (optional)

If you want to expose the outbound/rankings side of the dashboard on the
internet, put a reverse proxy in front of the local process. This only
covers the OUTBOUND side (public network) — the local RX/TX side still
needs to run on the same WiFi network as the digi.

## Project structure

```
aprs_dashboard/
  app.py                        # FastAPI: API routes + serves the dashboard
  aprs_client.py                 # KISS/AX.25 (local RX+TX) + APRS-IS (outbound)
  db.py                           # SQLite: schema and queries
  launcher.py                     # executable entry point (PyInstaller)
  aprs_dashboard.spec             # PyInstaller build config
  .github/workflows/build.yml     # builds Windows/macOS/Linux automatically
  docs/index.html                 # download page (GitHub Pages)
  config.yaml.example
  requirements.txt
  LICENSE                         # GPLv3
  templates/dashboard.html
```

## License

GPLv3 — see the `LICENSE` file. Any distributed modification must remain
open under the same license.

## Known limitations

- Outbound distance is only calculated once the position of the iGate that
  reported your digi has been seen in some packet — that's how any APRS
  tool calculates this, there's no way around it without mapping the
  entire network.
- Direct/relayed classification is based on the "used" bit (H-bit) of the
  AX.25 path hops — generic aliases (WIDEn-N, TRACEn-N, RELAY) don't count
  as a real digipeater.
- Text parsing (outbound/APRS-IS) uses the `aprslib` library, which covers
  the vast majority of formats (compressed position, Mic-E, plain text).
- This doesn't replace the iGate/digi itself — it's an observer that
  listens to what passes through, keeps history, and sends messages on
  request.
