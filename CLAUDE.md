# Familienkalender

Aggregierter Familienkalender als lokales Home-Assistant-Add-on. Zeigt die Kalender der Familie auf einem Touch-Display (Waveshare 15.6" HDMI, am Raspberry Pi 5) und in einer Web-UI in der HA-Seitenleiste (Ingress).

## Fachlicher Kern

- **Quellen:**
  - Marina (Google Calendar API): wird **komplett** angezeigt
  - Kunde (Google Calendar API, Workspace-Konto): **gefiltert**
  - Firma (Nextcloud CalDAV): **gefiltert**
- **Filterregel** für Kunde + Firma: Es erscheinen nur Termine, die für die Familie relevant sind — Termine, die in die Abendstunden hineinreichen oder dort beginnen (Grenze: 17:00 Uhr, im Admin konfigurierbar) sowie mehrtägige/übernachtende Termine. Reine Untertags-Meetings werden ausgeblendet — aber nur an Arbeitstagen: an Wochenenden und an gesetzlichen Feiertagen in Bayern (`app/holidays_bavaria.py`, Gauß-Osterformel, keine externe Dependency) werden alle Termine gezeigt; es zählt das lokale Startdatum (Europe/Berlin). Echte Termintitel werden angezeigt.
- **Aggregation intern** (kein Zurückschreiben in externe Kalender). Sync-Intervall ~5 Minuten.
- **Tages-Tags:** Pro Tag können Symbole (Smileys/Icons, kleine Auswahl) gesetzt werden — per Touch und Web. Nur lokale Speicherung.
- **Ansichten:** Monats- und Wochenansicht, blätterbar, touch-optimiert. Zusätzlich eine Strom-Ansicht (Balkonkraftwerk/Steckdosen via HA-API), zwischen der und dem Kalender man am Display umschalten kann.
- **Adminbereich** (Web): Kalender-Zugänge, Google-OAuth-Verbindung, Abendgrenze, Icons, weitere Einstellungen.

## Architektur

- Python 3.12 / FastAPI-Backend, SQLite als lokaler Speicher
- Frontend: Vanilla JS/CSS, kein Framework, touch-optimiert (1920×1080)
- Verpackt als lokales HA-Add-on (`aarch64`, HA OS auf Raspberry Pi 5), Web-UI über **Ingress**
- Add-on-Root = Repo-Root (`config.yaml`, `Dockerfile` auf oberster Ebene), damit Deployment = Repo nach `/addons/familienkalender` auf den Pi synchronisieren

## Rechtemodell (Etappe 8)

- **Normale HA-Benutzer** (z. B. Marina, `familiendisplay` — darf Nicht-Admin sein): sehen das Panel (`panel_admin: false` in config.yaml) und nutzen Kalender, Tages-Symbole und Strom-Ansicht.
- **HA-Admins:** zusätzlich Verwaltung (`/admin`, `/api/admin/*`). Nicht-Admins bekommen dort 403 „Nur für Administratoren." (auf `/admin` als deutsche HTML-Seite); das Zahnrad im Header erscheint nur nach Bestätigung durch `GET /api/me`.
- **Vertrauenskette** (app/auth.py): Die IP-Allowlist lässt nur den Ingress-Proxy (172.30.32.2) und 127.0.0.1 zu → deshalb sind die vom Supervisor gesetzten Header `X-Remote-User-Id/-Name/-Display-Name` glaubwürdig (Quelle: supervisor/api/ingress.py `_init_header`). Einen Admin-Header gibt es nicht; die Admin-Gruppe wird per HA-WebSocket `config/auth/list` (`ws://supervisor/core/websocket`) aufgelöst, 60 s gecacht, Fehler = Nicht-Admin (fail closed). Anfragen von 127.0.0.1 **ohne** User-Header gelten als Admin (lokale Entwicklung, E2E, Healthcheck — echter Ingress setzt den Header immer) — **aber nur, wenn `SUPERVISOR_TOKEN` nicht gesetzt ist.** `SUPERVISOR_TOKEN` ist der zuverlässige Marker für „läuft im Add-on-Container"; ist er gesetzt, ist der Localhost-Fallback deaktiviert und header-lose 127.0.0.1-Anfragen gelten als Nicht-Admin (fail closed) — betrifft nur den Container-Healthcheck, echter Ingress-Traffic trägt immer den Header.
- **Bewusste Ausnahmen vom Admin-Gate** (Begründung in `docs/backlog.md`, Abschnitt „Bewusste Entscheidungen im Admin-Gate"): `POST /api/sync` bleibt für alle HA-Nutzer offen (schreibt nur in den internen Cache, gegen Mehrfachlauf per `SYNC_LOCK`/409 abgesichert, Ziele sind admin-konfiguriert); `/static/admin/*` bleibt ohne Gate lesbar (reine UI-Struktur, keine Secrets); der 60-s-Cache lässt einen Admin-Entzug entsprechend leicht verzögert wirken.

## Arbeitsregeln (verbindlich für alle Agenten und Sessions)

1. **TDD:** Erst Test schreiben, dann implementieren. Keine Etappe ist fertig ohne grüne Tests (`pytest`).
2. **Commits:** Jeder abgeschlossene Schritt wird **einzeln committet und gepusht** — direkt auf `main` (`git@github.com:roland1910/Familienkalender.git`).
3. **Quality-Gate:** `ruff check` (und Frontend-Lint, sobald vorhanden) muss vor jedem Commit sauber sein. Code gut lesbar und wartbar halten; nach jedem größeren Arbeitspaket läuft ein separater Quality-Review-Agent.
4. **Security:** Keine Secrets im Repo, in Commits oder im Chat. Credentials kommen zur Laufzeit aus der Admin-UI/HA-Optionen; für lokale Entwicklung aus `secrets.local.json` (gitignored). Security-Review bei jedem Meilenstein, der Credentials, Netzwerk oder Nutzereingaben berührt. **Frontend/Stored-XSS:** Event-Titel und -Location stammen aus fremden Kalendern/Einladungen — sie werden im Frontend ausschließlich via `textContent` gerendert, niemals via `innerHTML`.
5. **Sprache:** UI-Texte und Nutzer-Doku Deutsch; Code, Bezeichner und Kommentare Englisch.
6. **Deployment** auf den Pi macht ausschließlich der Orchestrator (Hauptsession), nicht Implementierungs-Agenten. Erprobte Prozedur:
   ```
   git archive main | ssh -i ~/.ssh/id_ed25519 root@192.168.1.3 "tar -x -C /addons/familienkalender"
   ssh ... "ha store reload && ha addons rebuild local_familienkalender && ha addons restart local_familienkalender"
   ```
   Wichtig: `ha store reload` (nicht `ha addons reload`) macht Änderungen an config.yaml/neuen Add-ons sichtbar. Danach Logs prüfen: `ha addons logs local_familienkalender`.
7. **Rollen & Modelle:** Implementierung komplexer Logik → Fable/inherit; mechanische Arbeit & Quality-Review → Sonnet; Boilerplate/Doku → Haiku; Security-Review → Fable. Implementierungs-Agenten laufen im Hintergrund.

## Kommandos

- Tests (Unit, schnell): `pytest`
- E2E-Tests (Browser, Playwright): `pytest -m e2e` — einmalig vorher `python -m playwright install chromium`
- Integrationstests: `pytest -m integration` (brauchen `secrets.local.json`)
- Lint Backend: `ruff check .`
- Lint Frontend: `npm run lint` (Biome; einmalig vorher `npm install`)
- JS-Unit-Tests: `npm run test:js` (node --test gegen die ES-Module, Tests in `tests/js/`)
- Demo-Daten für lokale Entwicklung: `python scripts/seed_demo.py` (legt Quellen + Events in DATA_DIR bzw. `./data` an)

## Frontend

- Vanilla-ES-Module in `app/static/js/` (api, state, dates, events, colors, dom, popover, month-view, week-view, tag-picker, power-view, power-format, gestures, legend, view-memory, screensaver-memory, slideshow-view, main), CSS in `app/static/css/`. **Kein Build-Schritt** — die Module laufen direkt im Browser.
- Quellen-Legende (`legend.js`): Farbpunkt + Name je aktivierter Quelle unter Monats-/Wochenansicht (nicht in der Strom-Ansicht), Farben identisch zu den Event-Chips (Auflösung siehe unten), Daten aus `GET /api/sources`.
- Strom-Ansicht: Umschalter oben rechts (`#mode-slot`), Daten von `GET /api/power` (`app/power.py`, HA Core API via `http://supervisor/core/api` + SUPERVISOR_TOKEN; lokal per `HA_API_URL`/`HA_API_TOKEN` übersteuerbar). Geräteliste als Setting `power_devices`, gepflegt im Admin unter Einstellungen.
- Adminbereich unter `/admin` (`app/static/admin/`: admin.html, main.js, api.js; Stile in `app/static/css/admin.css`), erreichbar über das Zahnrad im Kalender-Header. Gleiche Regeln: relative URLs, `textContent`-only, Deutsch. Backend-Endpunkte unter `/api/admin/*` (`app/admin.py`).
- Quellen-Farben: je Quelle optional konfigurierbar (Admin-Quellenzeile, `<input type="color">` + „Standardfarbe"), sonst Palette nach id. Auflösung zentral in `colors.js` (`colorForSource`/`colorForEvent`) — Chips, Wochenbalken, Popover und Legende nutzen dieselbe. **CSS-Injection-Regel:** Die Farbe wird in eine CSS-Variable interpoliert; server- (strikt `#rrggbb`, `is_valid_source_color`) UND clientseitig (Regex in colors.js, sonst Palette) validiert.
- Wochenansicht: leere Nachtstunden sind eingeklappt — das Raster beginnt um 08:00 bzw. zur vollen Stunde des frühesten Zeit-Termins der Woche (`gridStartHour` in week-view.js); kein Initial-Scroll mehr. Auto-Zoom: die Stundenhöhe ist die CSS-Variable `--hour-height` auf `.week-view` (alle vertikalen Positionen per `calc()`), von `applyWeekAutoZoom` so gesetzt, dass der sichtbare Bereich die Höhe füllt (Kiosk scrollt nicht); Minimum 24px, darunter Scroll-Fallback.
- Ansichts-Persistenz (`view-memory.js`): Ansicht (Monat/Woche), Anker-Datum und Modus (Kalender/Strom) werden pro Gerät in localStorage gemerkt und beim Laden wiederhergestellt; der Heute-Button setzt auch den gespeicherten Anker. Gespeicherte Werte gelten als untrusted — ungültige fallen auf Defaults zurück.
- JS-Lint/-Format: **Biome** (einzelne Dev-Dependency in `package.json`, `biome.json` als Konfiguration). `npx biome check --write app/static` formatiert.
- Alle URLs im Frontend sind relativ (Ingress!). Fremde Strings (Titel, Ort) ausschließlich via `textContent` — abgesichert durch `tests/test_frontend_static.py` (verbietet HTML-Injection-Sinks) und den XSS-E2E-Test.

## Foto-Diashow (Kiosk-Bildschirmschoner)

- **Zweck:** Am Touch-Display läuft nach Leerlauf eine Vollbild-Foto-Diashow als Bildschirmschoner. Fotos liegen auf der CIFS-Netzwerkfreigabe „Photos", die Home Assistant unter `/media` einbindet; das Add-on liest sie **schreibgeschützt** (config.yaml `map: media:ro`, apparmor.txt: `/media` nur `r`, `deny /media/** wklx`).
- **Backend** (`app/slideshow.py`, Muster wie `tags.py`): Setting `slideshow_dirs` (JSON-Liste von Ordnern unterhalb `/media`). Pfad-Validierung gegen Traversal per `os.path.realpath` + `commonpath` (`normalize_media_dir` — fängt `../` und Symlinks ab, die aus `/media` herauszeigen); auf Lese- **und** Schreibpfad validiert. Foto-Index: rekursiver `os.scandir`-Scan (jpg/jpeg/png/webp case-insensitive; Symlinks **nicht** gefolgt; `#recycle`/`@eaDir`/versteckte Ordner übersprungen; Fehler je Ordner isoliert; Obergrenze `MAX_INDEXED_PHOTOS = 100000`). Der Scan läuft in `asyncio.to_thread` (blockiert den Event-Loop nicht — die reale Freigabe hat ~114k Dateien) und ist per Lock serialisiert; ausgelöst beim Speichern und stündlich (`periodic_photo_scan`, nur mit `SLIDESHOW_SCAN=1` in der Lifespan aktiv, damit Tests/Dev ohne `/media` nicht scannen). Tabelle `photos (id, path UNIQUE, mtime, shown)`.
- **Rotation mit Gedächtnis:** `GET /api/slideshow/next` liefert ein zufälliges Foto mit `shown=0`, markiert es `shown=1` (`{id, name}`); sind alle gezeigt, wird `shown` zurückgesetzt und neu gezogen — ein voller Durchlauf vor Wiederholung, überlebt Neustart (Zustand in der DB). `MEDIA_ROOT` env übersteuert den Wurzelpfad für Tests.
- **Bild-Endpoint:** `GET /api/slideshow/image/{id}` streamt via `FileResponse` (Content-Type nach Endung, `Cache-Control: private, max-age=60`). Zugriff **nur über DB-id**, nie Pfad-Parameter; unbekannte id oder verschwundene Datei → 404 (verschwundene Datei wird aus dem Index entfernt). Läuft hinter Ingress + IP-Allowlist.
- **Frontend** (`slideshow-view.js`): Vollbild-Overlay, `object-fit: contain` auf Schwarz, zwei gestapelte Layer für sanfte Überblendung, nächstes Bild vorab geladen (`Image`-Preload, kein schwarzer Blitz), Wechsel alle `SLIDE_INTERVAL_MS` (30s; per `window.SLIDESHOW_INTERVAL_MS` für Tests übersteuerbar). Dateiname nur via `textContent`.
- **Bildschirmschoner pro Gerät** (`screensaver-memory.js`, localStorage wie view-memory): Toggle (Foto-Symbol `#btn-screensaver` im `#mode-slot`), Default **AUS**. Wenn AN: nach `IDLE_TIMEOUT_MS` (180s, per `window.SCREENSAVER_IDLE_MS` für Tests übersteuerbar) ohne pointerdown/keydown/wheel startet die Diashow; jede Interaktion beendet sie → Kalender im vorigen Zustand. Idle-Watcher pollt sekündlich (kein Timer-Reset pro Event).
- **Admin** (`app/static/admin/slideshow.js`, Sektion „Diashow (Kiosk)"): Ordnerliste (hinzufügen per Verzeichnis-Browser `GET /api/admin/slideshow/dirs?path=` durch `/media`, entfernen), Foto-Anzahl im Index + „Neu einlesen", Hinweistext zur CIFS-Einbindung. Admin-API `GET/PUT /api/admin/slideshow` (+ `POST .../rescan`), alle admin-gated.
- **Tests:** `tests/test_slideshow.py` (Pfad-Traversal inkl. Symlink, Index-Scan, Rotation, Admin-API, Bild-Endpoint), E2E `tests/e2e/test_slideshow_e2e.py` (Toggle + Idle-Start mit gefaktem kurzem Timeout, Touch beendet) und `tests/e2e/test_admin_slideshow_e2e.py` (Admin-Sektion). JS-Unit `tests/js/screensaver-memory.test.mjs`.

## Kalender-Abo (ICS-Feed)

- `GET /feed/<token>.ics` (`app/feed.py` baut das ICS, Route in `app/feed_app.py`): abonnierbarer Kalender „Familie – Roland" für Marinas Handy (ICSx⁵/DAVx⁵). Inhalt: nur Quellen mit `include_in_feed=true` (Schalter „Im Kalender-Abo" je Quelle in der Admin-Quellenzeile; Default bei neuen Quellen: an für `filtered`, aus für `full` — Migration übernimmt das historische Verhalten), jeweils gefiltert mit derselben Familienlogik wie die Ansichten (`filter_events` nach dem `display_mode` der Quelle, Abendgrenze), Fenster −7/+90 Tage; Titel mit Quellen-Kürzel (`shortcode`, max. 6 Zeichen A–Z/0–9, gepflegt in der Admin-Quellenzeile) als Präfix. Stabile UIDs (Hash über source_id|uid|start), REFRESH-INTERVAL/X-PUBLISHED-TTL PT15M.
- **Eigener TLS-Listener, extern erreichbar:** Der Feed läuft als separater, minimaler ASGI-Baum (`app/feed_app.py`) auf Container-Port 8100 → Host-Port 8098 (`ports` in config.yaml); der Router darf extern 8098 dorthin weiterleiten (`https://rnd.ignorelist.com:8098/…`). `app/serve.py` (`python -m app.serve`, gestartet von run.sh) fährt Haupt-App (8099, Ingress) und Feed-Listener in EINEM Prozess/asyncio-Loop; TLS mit den HA-Zertifikaten aus `/ssl` (Options `ssl_certfile`/`ssl_keyfile`, Default fullchain/privkey; fehlen sie, startet nur der Feed nicht, stündlicher mtime-Check erkennt Erneuerung und startet den Listener neu). Die Haupt-App ist strikt ingress-only (keine Feed-Ausnahme mehr in der IP-Allowlist).
- **Härtung im Feed-Listener** (`app/feed_app.py`, Tests in `tests/test_feed_app.py`): URL-Token (settings-Key `feed_token`, `secrets.token_urlsafe(32)`, Vergleich per `compare_digest`) als einzige Auth, falsches/fehlendes Token → 404; nur GET/HEAD (405 sonst); Security-Header auf jeder Antwort; Rate-Limit 30/min pro IP + 300/min global; 15-Minuten-Lockout nach 10 Token-Fehlversuchen (absolut, auch bei gültigem Token); kein Access-Log (Token in der URL). Abo-URL (https, Setting `feed_public_host`, Default Request-Host) + Rotation in der Admin-Sektion „Kalender-Abo" (`/api/admin/feed`).
- Risiko-Abwägung (bewusst extern, Restrisiko URL-Token in Client-Historien) in `docs/backlog.md`.
