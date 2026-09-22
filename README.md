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

The app opens a browser window (normally at `http://localhost:8080` — if
that port is already taken by something else, it automatically picks the
next free one) and everything —
your digi's IP address, ports, callsign — is configured from the
**Settings** tab inside the dashboard itself.

**Windows and macOS:** no terminal window stays open — the app runs quietly
in the system tray / menu bar. Right-click its icon there to reopen the
dashboard or quit. **Linux:** it runs in the terminal window you opened it
from; leave that open, and close it (or Ctrl+C) to stop the server.

**macOS:** releases are code-signed with an Apple Developer ID certificate
and notarized by Apple, so the app just opens normally the first time — no
"unidentified developer" block. You may see a one-time "downloaded from the
internet, are you sure?" prompt; that's normal, just click **Open**. If
Gatekeeper still blocks it for any reason, right-click the app → **Open**,
or go to **System Settings → Privacy & Security**, scroll down, and click
**"Open Anyway"**.

**Windows:** the app isn't code-signed yet (no Authenticode certificate),
so SmartScreen will show a "Windows protected your PC" warning the first
time. This is expected — click **"More info"** → **"Run anyway"**.

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

## Signing & notarizing macOS releases (maintainer setup, one-time)

The build workflow (`.github/workflows/build.yml`) automatically signs and
notarizes the macOS app with a Developer ID Application certificate — but
only once these repository secrets are set (Settings → Secrets and
variables → Actions → New repository secret):

**New secrets needed** (specific to this repo — a `.p12` for a **Developer
ID Application** certificate, which is different from the "Apple
Distribution" certificate used for App Store submissions):

| Secret | What it is |
|---|---|
| `MACOS_CERTIFICATE` | A **Developer ID Application** certificate (not "Apple Distribution" — that one won't work here), exported from Keychain Access as a `.p12` file, then base64-encoded. In Keychain Access: find the certificate (and its private key) under "My Certificates" → right-click → **Export** → save as `.p12` with a password. Then run `base64 -i certificate.p12 \| pbcopy` in Terminal and paste the result as the secret value. |
| `MACOS_CERTIFICATE_PWD` | The password you set when exporting the `.p12` above. |
| `MACOS_CI_KEYCHAIN_PWD` | Any password you make up — it only protects a temporary keychain created during the CI build, and is thrown away right after. |

**Reused from the `ftapp` repo** (same App Store Connect API key works for
notarization here — just copy the same secret values into this repo's
Settings, or move them to GitHub **Organization** secrets and grant this
repo access so both point at the same value without duplicating):

| Secret | What it is |
|---|---|
| `ASC_KEY_ID` | App Store Connect API key ID (same value as in `ftapp`). |
| `ASC_ISSUER_ID` | App Store Connect API issuer ID (same value as in `ftapp`). |
| `ASC_KEY_CONTENT` | The `.p8` private key content for that API key (same value as in `ftapp`). |

`APPLE_TEAM_ID` from `ftapp` is **not** needed here — authenticating
notarization with an API key doesn't require it.

Once the new secrets are set (and the reused ones copied or shared via
Organization secrets), every tag push (`git tag vX.Y.Z && git push origin
vX.Y.Z`) builds, code-signs, and notarizes the macOS app automatically —
nothing else to do. If a secret is missing or wrong, the macOS build job
fails with a clear error at the signing/notarization step (the Windows and
Linux builds are unaffected either way).

**Windows note:** code signing isn't set up yet — there's no existing
Authenticode certificate to reuse. Until one is purchased/generated (a
`.pfx`/`.p12` code signing certificate + password, added as new secrets and
a signing step), the Windows build stays unsigned and SmartScreen will keep
showing its warning on first run.

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
