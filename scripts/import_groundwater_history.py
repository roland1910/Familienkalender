"""Import der von Hand heruntergeladenen GKD-Grundwasser-Historie.

Aufruf:
    python scripts/import_groundwater_history.py <archiv.zip> [<archiv2.zip> …]

Die vollständige Historie einer Messstelle (zurück bis in die 1970er/80er)
gibt es nur über das Downloadcenter der GKD Bayern — und das ist bewusst
Handarbeit: die Pfade sind in der robots.txt gesperrt und das Formular
verlangt eine persönliche Zustimmung zu den Nutzungsbedingungen. Dieses
Skript importiert nur das Ergebnis (ein ZIP mit je einer CSV pro Messstelle
und Teilzeitraum) in die Datenbank unter DATA_DIR.

Der Import ist idempotent: der Primärschlüssel ist (Messstelle, Tag), ein
zweiter Lauf ändert also nichts. Messstellennummern, die nicht im
Messstellen-Index stehen, werden übersprungen und am Ende genannt.
"""

import sys
from pathlib import Path

# Allow running as a plain script: `python scripts/import_groundwater_history.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.groundwater_import import import_zip_archives, summary_text
from app.storage import DB_FILENAME, Storage, resolve_data_dir


def main(argv: list[str] | None = None) -> int:
    paths = [Path(item) for item in (argv if argv is not None else sys.argv[1:])]
    if not paths:
        print(__doc__)
        return 2
    missing = [path for path in paths if not path.is_file()]
    if missing:
        for path in missing:
            print(f"Nicht gefunden: {path}")
        return 2

    data_dir = resolve_data_dir()
    # Announce the target before anything is written, so an aborted run still
    # tells the user where it was about to write.
    print(f"Ziel-Datenverzeichnis: {data_dir}")
    storage = Storage(data_dir / DB_FILENAME)
    before = storage.count_groundwater_readings()
    result = import_zip_archives(storage, paths)
    after = storage.count_groundwater_readings()
    print(summary_text(result))
    # No fancy arrow here: the summary must also survive a console that is
    # not running in UTF-8.
    print(f"Messwerte in der Datenbank: vorher {before}, jetzt {after} (neu: {after - before}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
