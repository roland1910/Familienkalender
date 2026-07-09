# Google OAuth für den Familienkalender einrichten

Einmalige Klickarbeit (~10 Minuten). Danach läuft alles Weitere (Konten verbinden, Kalender auswählen) über den Adminbereich des Add-ons.

## Welches Konto?

**Empfehlung: dein privates Google-Konto** als Inhaber des Cloud-Projekts. Gründe:

- Unabhängig vom Kunden — endet das Kundenverhältnis, bleibt die Familien-Infrastruktur intakt.
- Workspace-Konten (Kunde) unterliegen oft Admin-Richtlinien, die das Anlegen von OAuth-Apps oder deren Nutzung einschränken.
- Marina und dein Kundenkonto werden später einfach als Nutzer **verbunden** — dafür müssen sie das Projekt nicht besitzen.

## Schritt für Schritt

1. Mit dem **privaten Konto** anmelden auf <https://console.cloud.google.com>
2. Oben in der Projektauswahl → **Neues Projekt** → Name `Familienkalender` → Erstellen (und danach auswählen).
3. **APIs & Dienste → Bibliothek** → nach „Google Calendar API" suchen → **Aktivieren**. Für die Geburtstage-Funktion zusätzlich nach „People API" suchen → **Aktivieren** (der Google-„Geburtstage"-Kalender ist über die Calendar API nicht abrufbar, die Kontakt-Geburtstage kommen aus der People API).
4. **APIs & Dienste → OAuth-Zustimmungsbildschirm** (bzw. „Google Auth Platform → Branding"):
   - Nutzertyp: **Extern**
   - App-Name: `Familienkalender`, Support-E-Mail: dein privates Konto
   - Kontaktdaten: dein privates Konto → Speichern
5. **Zielgruppe/Publishing-Status**: App auf **„In Produktion"** stellen (nicht im Status „Test" lassen!).
   - Wichtig: Im Test-Status laufen die Zugriffstoken nach **7 Tagen** ab — der Kalender würde wöchentlich die Verbindung verlieren.
   - Die App bleibt „unverifiziert" — das ist für eine private Eigenbau-App in Ordnung. Beim Verbinden zeigt Google eine Warnung („Google hat diese App nicht überprüft"), die man über „Erweitert → Fortfahren" bestätigt.
6. **Anmeldedaten → Anmeldedaten erstellen → OAuth-Client-ID**:
   - Anwendungstyp: **Desktop-App**, Name: `Familienkalender Addon`
   - **Client-ID** und **Client-Secret** anzeigen lassen und sicher notieren (z. B. Passwortmanager). Diese beiden Werte trägst du später im Adminbereich ein — nicht ins Repo, nicht in den Chat.

## Konten verbinden (später, im Adminbereich)

Der Adminbereich führt durch die Verbindung — einmal für **Marina** (an ihrem Handy oder in einem Browser, in dem ihr Konto angemeldet ist) und einmal für dein **Kundenkonto**. Benötigter Zugriff: nur **Kalender lesen** (`calendar.readonly`).

**Geburtstage aus Google-Kontakten:** Über den Button „Geburtstage (Google-Kontakte) hinzufügen" lassen sich zusätzlich die Geburtstage aus den Kontakten eines Google-Kontos einbinden (ganztägige Termine „🎂 Name"). Dieser Assistent fordert einen anderen Zugriff an — nur **Kontakte lesen** (`contacts.readonly`) — und setzt voraus, dass die **People API** im Cloud-Projekt aktiviert ist (siehe Schritt 3). Kein Alter wird angezeigt; Geburtstage ohne Jahr werden ebenso übernommen.

**Belegt-Sync (MoreValue → Xalt):** Die Sektion „Belegt-Sync" verbindet – als **einziger schreibender** Zugriff des Add-ons – das **Xalt-Konto** mit dem Zugriff **Kalender-Termine lesen und schreiben** (`calendar.events`). Damit schreibt das Add-on neutrale „Busy MV"-Blöcke in Rolands **primären** Xalt-Kalender (Frei/Belegt für die Kollegen). Der Schreib-Token wird getrennt vom Lese-Token gespeichert; das Add-on verwaltet **ausschließlich eigene, markierte Blöcke**. Da `calendar.events` mehr als Lesezugriff ist, kann ein **Workspace-Administrator** diesen Scope blockieren (dann scheitert das Verbinden mit einer deutschen Fehlermeldung — kein Absturz); ggf. muss die App vom Workspace-Admin freigegeben werden.

## Möglicher Stolperstein: Kundenkonto (Workspace)

Manche Workspace-Administratoren blockieren „nicht überprüfte" Dritt-Apps. Falls die Verbindung mit dem Kundenkonto scheitert:

**Fallback:** Im Kundenkalender → Einstellungen → „Für bestimmte Personen freigeben" → dein privates Konto mit „Alle Termindetails anzeigen" eintragen. Dann liest der Familienkalender den Kundenkalender einfach über dein privates Konto mit — gleiche Filterung, kein Workspace-Zugriff nötig.
