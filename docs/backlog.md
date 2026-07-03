# Backlog

Bewusst verschobene Punkte aus Quality- und Security-Reviews. Jeder Punkt nennt den Meilenstein, vor dem er spätestens umgesetzt werden muss.

## Security

- **AppArmor-Profil für das Add-on** — spätestens vor dem Credentials-Meilenstein (Google-OAuth/CalDAV-Zugänge), damit der Container bei kompromittiertem Prozess eingeschränkt bleibt.
- ~~**`pip install --require-hashes` bzw. venv im Image**~~ — **ERLEDIGT:** requirements.txt wird aus requirements.in per `uv pip compile --universal --generate-hashes --python-version 3.12` erzeugt (alle transitiven Deps gepinnt, Hashes aller PyPI-Artefakte inkl. musllinux-aarch64), Dockerfile installiert mit `--require-hashes`.
- **Secrets-Speicherort:** OAuth-Tokens und Passwörter werden über die Admin-UI erfasst und unter `/data/` abgelegt (Dateirechte `chmod 600`, Besitzer `app`) — **nicht** in den Add-on-Options. Add-on-Options niemals loggen.
- **Zentrale `validate_source_url()`** — spätestens beim Admin-UI-Meilenstein (sobald Quellen-URLs über die UI erfasst werden): nur `https`, keine Userinfo in der URL, keine Ziele im Container-/Link-Local-Netz (SSRF-Schutz). Validierung sowohl **beim Speichern** einer Quelle als auch **vor jedem Fetch** (die Config kann auch auf anderem Weg in die DB gelangt sein).
- **Frontend-Regel Stored-XSS** — sobald das Frontend Events rendert: Event-Titel und -Location stammen aus fremden Kalendern/Einladungen und werden **ausschließlich via `textContent`** ins DOM geschrieben, nie via `innerHTML` (verbindliche Regel, auch in CLAUDE.md unter Arbeitsregeln verankert).

## Funktional

- **`homeassistant_api: true` reaktivieren**, sobald die Strom-Ansicht (Balkonkraftwerk/Steckdosen via HA-API) umgesetzt wird — wurde aus `config.yaml` entfernt, solange es keinen Nutzer der API gibt.
