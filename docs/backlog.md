# Backlog

Bewusst verschobene Punkte aus Quality- und Security-Reviews. Jeder Punkt nennt den Meilenstein, vor dem er spätestens umgesetzt werden muss.

## Security

- **AppArmor-Profil für das Add-on** — spätestens vor dem Credentials-Meilenstein (Google-OAuth/CalDAV-Zugänge), damit der Container bei kompromittiertem Prozess eingeschränkt bleibt.
- ~~**`pip install --require-hashes` bzw. venv im Image**~~ — **ERLEDIGT:** requirements.txt wird aus requirements.in per `uv pip compile --universal --generate-hashes --python-version 3.12` erzeugt (alle transitiven Deps gepinnt, Hashes aller PyPI-Artefakte inkl. musllinux-aarch64), Dockerfile installiert mit `--require-hashes`.
- **Secrets-Speicherort:** OAuth-Tokens und Passwörter werden über die Admin-UI erfasst und unter `/data/` abgelegt (Dateirechte `chmod 600`, Besitzer `app`) — **nicht** in den Add-on-Options. Add-on-Options niemals loggen.
- **Zentrale `validate_source_url()`** — spätestens beim Admin-UI-Meilenstein (sobald Quellen-URLs über die UI erfasst werden): nur `https`, keine Userinfo in der URL, keine Ziele im Container-/Link-Local-Netz (SSRF-Schutz). Validierung sowohl **beim Speichern** einer Quelle als auch **vor jedem Fetch** (die Config kann auch auf anderem Weg in die DB gelangt sein).
- **Serverseitige Limits beim Sync** — spätestens beim nächsten Sync-Meilenstein: Titel und Location beim Übernehmen aus fremden Kalendern auf 1000 Zeichen kürzen und die Event-Anzahl pro Quelle und Sync-Fenster deckeln. Das Frontend kappt seit Etappe 3 nur die Darstellung (Lanes, Chips, Popover-Liste); ohne serverseitige Grenze kann eine feindliche Quelle weiterhin Speicher und API-Antworten aufblähen.
- ~~**Frontend-Regel Stored-XSS**~~ — **UMGESETZT (Etappe 3):** Alle fremden Strings gehen ausschließlich via `textContent` ins DOM. Abgesichert doppelt: `tests/test_frontend_static.py` verbietet HTML-Injection-Sinks (innerHTML & Co.) in allen JS-Dateien; E2E-Test rendert Events mit `<script>`-/`<img onerror>`-Titeln und prüft, dass sie als Text erscheinen (kein Dialog, kein injiziertes Element). Regel bleibt für alle künftigen Frontend-Arbeiten verbindlich.

## Funktional

- **`homeassistant_api: true` reaktivieren**, sobald die Strom-Ansicht (Balkonkraftwerk/Steckdosen via HA-API) umgesetzt wird — wurde aus `config.yaml` entfernt, solange es keinen Nutzer der API gibt.
