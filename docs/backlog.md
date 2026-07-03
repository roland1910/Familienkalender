# Backlog

Bewusst verschobene Punkte aus Quality- und Security-Reviews. Jeder Punkt nennt den Meilenstein, vor dem er spätestens umgesetzt werden muss.

## Security

- **AppArmor-Profil für das Add-on** — spätestens vor dem Credentials-Meilenstein (Google-OAuth/CalDAV-Zugänge), damit der Container bei kompromittiertem Prozess eingeschränkt bleibt.
- **`pip install --require-hashes` bzw. venv im Image** — spätestens vor Aufnahme der Kalender-Libs (google-api-python-client, caldav, …), damit die dann deutlich größere Dependency-Kette gegen Manipulation abgesichert ist.
- **Secrets-Speicherort:** OAuth-Tokens und Passwörter werden über die Admin-UI erfasst und unter `/data/` abgelegt (Dateirechte `chmod 600`, Besitzer `app`) — **nicht** in den Add-on-Options. Add-on-Options niemals loggen.

## Funktional

- **`homeassistant_api: true` reaktivieren**, sobald die Strom-Ansicht (Balkonkraftwerk/Steckdosen via HA-API) umgesetzt wird — wurde aus `config.yaml` entfernt, solange es keinen Nutzer der API gibt.
