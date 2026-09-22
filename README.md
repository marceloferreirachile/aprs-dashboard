# APRS Digipeater Dashboard

Dashboard local para monitorar um digipeater/iGate APRS: histórico em banco
de dados (SQLite), gráficos, ranking das estações mais escutadas, ranking de
DX (outbound e inbound), classificação direto/repetido com o path de cada
pacote, links prontos pra QRZ.com e aprs.fi, pesquisa, histórico por data, e
envio/recebimento de mensagens APRS com confirmação de ACK.

Feito pra ser usado por qualquer pessoa com um digi na rede local — cada um
roda sua própria cópia, aponta pro seu próprio equipamento e indicativo.
100% Python + SQLite (um arquivo só) — roda igual em Windows, Mac e Linux.

## Como funciona (importante entender isso)

O dashboard usa **três canais TCP/IP diferentes**, porque cada um faz um
trabalho que os outros não fazem:

1. **RX local — "quem o digi ouviu" (inbound)**: conecta na sua **rede WiFi
   local**, direto na porta TCP **KISS** do digi (testado e confirmado com o
   firmware ESP32APRS_CDU/v2.0-lu6jmf: portas 8001/8002, sem senha,
   configuráveis na aba MOD do painel do digi). Cada frame é um pacote AX.25
   real que o digi ouviu por RF — com o path original, então dá pra saber se
   a estação foi ouvida **direto** ou **repetida** por outro digipeater antes
   de chegar até o seu.
2. **TX local — envio de mensagem**: uma porta **separada** da porta de RX
   (ex: a segunda porta KISS do mesmo digi), usada só na hora de mandar uma
   mensagem pelo dashboard. Formato confirmado em teste real com hardware:
   `:ENDERECO  :texto{msgid` — um pacote de status/posição solto NÃO é
   reconhecido como mensagem pelo rádio de quem recebe.
3. **Outbound — "quem ouviu o digi"**: isso só a rede pública **APRS-IS**
   sabe dizer, porque é preciso ver o beacon do seu digi sendo relatado por
   OUTRO iGate em outro lugar. Não existe jeito de descobrir isso escutando
   só localmente — por isso o app se conecta (com filtro, só pro seu
   indicativo) na rede pública `rotate.aprs2.net`.

Se o seu digi não fala KISS binário (é outro software, tipo um aprsc local
que fala o texto padrão APRS-IS/TNC2), me avisa — dá pra adaptar o parser.

## Instalação

```bash
cd aprs_dashboard
python3 -m venv venv
source venv/bin/activate        # Windows (PowerShell): venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuração

```bash
cp config.yaml.example config.yaml
```

Edite `config.yaml`:

- `digi.callsign`, `digi.lat`, `digi.lon` — indicativo e coordenadas do seu digi.
- `local_feed.host` / `local_feed.port` — IP e porta TCP **KISS de RX** do seu digi.
- `messaging.my_callsign` / `my_ssid` — indicativo que aparece como remetente das mensagens.
- `messaging.tx.host` / `tx.port` — porta TCP **KISS de TX** (separada da de RX).
- `aprs_is.filter` — troque `LU6JMF-10` pelo seu indicativo (ex: `b/PY2ABC-10*`).

Se você não tem (ou não quer usar) a fonte local ainda, pode deixar
`local_feed.enabled: false` — o dashboard funciona só com outbound até você
ligar isso.

## Rodando

```bash
python app.py
```

Acesse em `http://localhost:8080` (ou `http://IP-DO-SEU-COMPUTADOR:8080` de
qualquer outro aparelho na mesma rede WiFi).

## Rodar sempre ligado

**macOS (launchd)** — crie `~/Library/LaunchAgents/com.aprsdashboard.plist`
apontando pro `venv/bin/python app.py` com `WorkingDirectory` na pasta do
projeto, e rode `launchctl load` nele.

**Linux (systemd)** — crie `/etc/systemd/system/aprs-dashboard.service`:

```ini
[Unit]
Description=APRS Dashboard
After=network.target

[Service]
WorkingDirectory=/caminho/para/aprs_dashboard
ExecStart=/caminho/para/aprs_dashboard/venv/bin/python app.py
Restart=always
User=seu_usuario

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now aprs-dashboard
```

**Windows** — duas opções simples:
- **Agendador de Tarefas**: crie uma tarefa "Ao fazer logon", ação
  `venv\Scripts\python.exe app.py`, "Iniciar em" apontando pra pasta do
  projeto.
- **NSSM** (nssm.cc, roda como serviço de verdade, reinicia sozinho):
  `nssm install AprsDashboard "C:\caminho\aprs_dashboard\venv\Scripts\python.exe" app.py`

## Publicar num subdomínio (opcional)

Se você tem um domínio e quer expor isso na internet com um subdomínio
(ex: `dx.seudominio.com`), coloque um reverse proxy na frente do processo
(que continua rodando só em `localhost:8080` ou numa porta interna):

```nginx
server {
    listen 80;
    server_name dx.seudominio.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Depois rode `certbot --nginx -d dx.seudominio.com` pra HTTPS. Isso é
totalmente opcional — o dashboard funciona 100% só na rede local também.
Lembrete: isso só serve pro lado OUTBOUND (rede pública). O lado RX/TX local
continua precisando rodar na mesma rede WiFi do digi — ver a ideia da versão
com "agente leve local" discutida como próxima fase.

## Distribuição pra outras pessoas (executável, sem precisar instalar Python)

A ideia final é uma página de download (`docs/index.html`, dá pra publicar
com GitHub Pages) com um botão por sistema operacional — a pessoa baixa um
arquivo único e clica duas vezes, sem terminal, sem `pip install`.

Isso é gerado com PyInstaller (`aprs_dashboard.spec` já incluso). Como
PyInstaller **não faz cross-compile** (tem que rodar NA plataforma alvo), o
workflow `.github/workflows/build.yml` builda automaticamente nos 3 sistemas
via GitHub Actions toda vez que uma tag `v*` é criada:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Isso sobe `aprs_dashboard-windows.exe`, `aprs_dashboard-macos` e
`aprs_dashboard-linux` como assets da Release, e a página de download já
aponta pra `.../releases/latest/download/...` — sempre pega a versão mais
recente sozinha.

Pra buildar manualmente numa máquina (teste local):

```bash
pip install pyinstaller
pyinstaller --noconfirm aprs_dashboard.spec
# resultado em dist/aprs_dashboard (ou .exe no Windows)
```

**Importante:** troque `marceloferreirachile/aprs-dashboard` em
`docs/index.html` pelo nome real do repositório GitHub quando você criar
ele — hoje é um nome de exemplo.

## Estrutura

```
aprs_dashboard/
  app.py                     # FastAPI: rotas da API + serve o dashboard
  aprs_client.py              # KISS/AX.25 (RX+TX local) + APRS-IS (outbound)
  db.py                        # SQLite: schema e queries (rankings, busca, histórico, mensagens)
  launcher.py                  # ponto de entrada do executável (PyInstaller)
  aprs_dashboard.spec          # config do PyInstaller
  .github/workflows/build.yml  # builda Windows/Mac/Linux automaticamente
  docs/index.html              # página de download (GitHub Pages)
  config.yaml.example
  requirements.txt
  LICENSE                      # GPLv3
  templates/dashboard.html
```

## Licença

GPLv3 — veja o arquivo `LICENSE`. Qualquer modificação distribuída precisa
continuar aberta sob a mesma licença.

## Limitações conhecidas

- Distância outbound só é calculada quando a posição do iGate que relatou o
  seu digi já foi vista em algum pacote (é assim que qualquer ferramenta
  APRS calcula isso — não tem outro jeito sem mapear a rede inteira).
- Classificação direto/repetido é baseada no bit "usado" (H-bit) dos hops do
  path AX.25 — aliases genéricos (WIDEn-N, TRACEn-N, RELAY) não contam como
  repetidor real.
- O parser de texto (outbound/APRS-IS) usa a biblioteca `aprslib`, que cobre
  a grande maioria dos formatos (posição comprimida, Mic-E, texto simples).
- Isso não substitui o iGate/digi em si — é só um observador que escuta o
  que passa, guarda o histórico e manda mensagem quando pedido.
