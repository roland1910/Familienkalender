# Familienkalender

Aggregierter Familienkalender als lokales Home-Assistant-Add-on. Zeigt die Kalender der Familie auf einem Touch-Display (Waveshare 15.6" HDMI, am Raspberry Pi 5) und in einer Web-UI in der HA-Seitenleiste (Ingress).

## Fachlicher Kern

- **Quellen:**
  - Marina (Google Calendar API): wird **komplett** angezeigt
  - Kunde (Google Calendar API, Workspace-Konto): **gefiltert**
  - Firma (Nextcloud CalDAV): **gefiltert**
- **Filterregel** für Kunde + Firma: Es erscheinen nur Termine, die für die Familie relevant sind — Termine, die in die Abendstunden hineinreichen oder dort beginnen (Grenze: 17:00 Uhr, im Admin konfigurierbar) sowie mehrtägige/übernachtende Termine. Reine Untertags-Meetings werden ausgeblendet. Echte Termintitel werden angezeigt.
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

- Vanilla-ES-Module in `app/static/js/` (api, state, dates, events, colors, dom, popover, month-view, week-view, tag-picker, power-view, power-format, gestures, main), CSS in `app/static/css/`. **Kein Build-Schritt** — die Module laufen direkt im Browser.
- Strom-Ansicht: Umschalter oben rechts (`#mode-slot`), Daten von `GET /api/power` (`app/power.py`, HA Core API via `http://supervisor/core/api` + SUPERVISOR_TOKEN; lokal per `HA_API_URL`/`HA_API_TOKEN` übersteuerbar). Geräteliste als Setting `power_devices`, gepflegt im Admin unter Einstellungen.
- Adminbereich unter `/admin` (`app/static/admin/`: admin.html, main.js, api.js; Stile in `app/static/css/admin.css`), erreichbar über das Zahnrad im Kalender-Header. Gleiche Regeln: relative URLs, `textContent`-only, Deutsch. Backend-Endpunkte unter `/api/admin/*` (`app/admin.py`).
- JS-Lint/-Format: **Biome** (einzelne Dev-Dependency in `package.json`, `biome.json` als Konfiguration). `npx biome check --write app/static` formatiert.
- Alle URLs im Frontend sind relativ (Ingress!). Fremde Strings (Titel, Ort) ausschließlich via `textContent` — abgesichert durch `tests/test_frontend_static.py` (verbietet HTML-Injection-Sinks) und den XSS-E2E-Test.
