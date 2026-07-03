# Backlog

Bewusst verschobene Punkte aus Quality- und Security-Reviews. Jeder Punkt nennt den Meilenstein, vor dem er spätestens umgesetzt werden muss.

## Security

- **AppArmor-Profil für das Add-on** — spätestens vor dem Credentials-Meilenstein (Google-OAuth/CalDAV-Zugänge), damit der Container bei kompromittiertem Prozess eingeschränkt bleibt.
- ~~**`pip install --require-hashes` bzw. venv im Image**~~ — **ERLEDIGT:** requirements.txt wird aus requirements.in per `uv pip compile --universal --generate-hashes --python-version 3.12` erzeugt (alle transitiven Deps gepinnt, Hashes aller PyPI-Artefakte inkl. musllinux-aarch64), Dockerfile installiert mit `--require-hashes`.
- **Secrets-Speicherort:** OAuth-Tokens und Passwörter werden über die Admin-UI erfasst und unter `/data/` abgelegt (Dateirechte `chmod 600`, Besitzer `app`) — **nicht** in den Add-on-Options. Add-on-Options niemals loggen.
- ~~**Zentrale `validate_source_url()`**~~ — **UMGESETZT (Etappe 4):** `app/url_validation.py` erzwingt https (http nur per `FAMILIENKALENDER_ALLOW_HTTP` für lokale Tests), verbietet Userinfo in der URL sowie IP-Literale in Link-Local-/Multicast-Bereichen und dem HA-internen Netz 172.30.32.0/23. Validiert wird beim Speichern (Admin-API) **und** defensiv vor jedem Fetch/list_calendars im CalDAV-Client.
- ~~**Serverseitige Limits beim Sync**~~ — **UMGESETZT (Etappe 4):** `limits.clamp_event_text()` kürzt Titel/Location zentral in der Sync-Engine auf 1000 Zeichen; Event-Anzahl pro Quelle/Fenster (`MAX_EVENTS_PER_SOURCE`), Antwortgröße (`MAX_RESPONSE_BYTES`) und Seitenzahl waren bereits gedeckelt.
- ~~**Frontend-Regel Stored-XSS**~~ — **UMGESETZT (Etappe 3):** Alle fremden Strings gehen ausschließlich via `textContent` ins DOM. Abgesichert doppelt: `tests/test_frontend_static.py` verbietet HTML-Injection-Sinks (innerHTML & Co.) in allen JS-Dateien; E2E-Test rendert Events mit `<script>`-/`<img onerror>`-Titeln und prüft, dass sie als Text erscheinen (kein Dialog, kein injiziertes Element). Regel bleibt für alle künftigen Frontend-Arbeiten verbindlich.

## Funktional

- **`homeassistant_api: true` reaktivieren**, sobald die Strom-Ansicht (Balkonkraftwerk/Steckdosen via HA-API) umgesetzt wird — wurde aus `config.yaml` entfernt, solange es keinen Nutzer der API gibt.
