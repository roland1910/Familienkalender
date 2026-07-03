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

- Tests: `pytest`
- Lint: `ruff check .`
