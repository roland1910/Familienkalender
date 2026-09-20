"""Importing a manually downloaded GKD history archive (Etappe 46, path B).

The GKD download centre hands out the FULL history of a monitoring station —
for one Munich station that is 17368 daily values going back to 1979, where
the scraped table page (see :mod:`app.groundwater_history`) only offers the
last two months. Downloading is deliberately HANDWORK: the download-centre
paths are disallowed in robots.txt and the form requires a personal
acceptance of the terms of use, so this module is only the importer for the
result. There is no client, no admin endpoint and no upload — Roland runs
``scripts/import_groundwater_history.py`` on the Pi.

Format of the CSV files inside the ZIP (semicolon separated, UTF-8 with BOM,
German decimal comma, ISO dates in the data rows)::

    Quelle:;"Bayerisches Landesamt für Umwelt, www.gkd.bayern.de"
    Datenbankabfrage:;"20.09.2026 00:00"
    Zeitbezug:;MEZ
    Messstellen-Name:;"JOHANNESKIRCHEN KPA 222"
    Messstellen-Nr.:;16277
    Ostwert:;696784;Nordwert:;5338502;"ETRS89 / UTM Zone 32N"

    Datum;"Grundwasserstand [m ü. NN]";Prüfstatus
    2026-09-01;506,04;Rohdaten

The archive holds ONE CSV per station and part-period, so several files can
belong to the same station and are merged. The station number is read from
the HEADER, never from the file name — the name is decoration, the header is
data. The ``Prüfstatus`` column (``Rohdaten``/``Geprueft``) is ignored on
purpose: the GKD only certifies older values, and refusing raw data would
drop exactly the recent months.

SECURITY — the archive is foreign data:

* **Never ``extractall``.** Nothing is written to disk at all; every member
  is read straight out of the stream. Entries with an absolute path, a drive
  letter or a ``..`` segment are skipped anyway (zip slip, defence in depth).
* **Zip bombs:** the number of entries (``MAX_ENTRIES``), the size of a
  single member (``MAX_MEMBER_BYTES``, enforced while reading rather than
  trusting the declared size) and the total uncompressed volume
  (``MAX_TOTAL_BYTES``) are all capped.
* Only ``.csv``/``.txt`` members are read; everything else is ignored.
* Unknown station numbers (not in ``groundwater_stations``) are skipped and
  counted rather than creating rows for stations the map does not show.
* Broken rows are skipped one by one, dates and values are validated
  strictly, and a broken or missing archive is reported, never raised.

Only ``zipfile``, ``csv`` and the standard library — the requirements are
hash-pinned and this must not add a dependency.
"""

import csv
import datetime as dt
import logging
import ntpath
import re
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.groundwater import parse_decimal
from app.storage import Storage

logger = logging.getLogger(__name__)

# Caps against a hostile or simply absurd archive (see the docstring).
MAX_ENTRIES = 5000
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024

# Only these members are read at all.
DATA_SUFFIXES = (".csv", ".txt")

_DELIMITER = ";"
# The header line carrying the station number, normalized (lower case, the
# trailing colon stripped).
_NUMBER_LABEL = "messstellen-nr."
# Station numbers are plain digits; anything else is not a station number.
_NUMBER = re.compile(r"^[0-9]{1,12}$")
# The data rows use ISO dates (unlike the table page's "TT.MM.JJJJ").
_ISO_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
# Plausibility window for a reading. The oldest Bavarian entry dates from
# 1947, so anything before 1900 is a parsing accident rather than history.
MIN_YEAR = 1900
MAX_YEAR = 2100


@dataclass
class ImportResult:
    """What one import run did — the numbers the summary line reports."""

    archives: int = 0
    files: int = 0
    stations: int = 0
    readings: int = 0
    skipped_files: int = 0
    skipped_rows: int = 0
    unknown_stations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# -- parsing one CSV ---------------------------------------------------------


def _cell(value: str) -> str:
    """One cell without surrounding whitespace and stray quotes."""
    return value.strip().strip('"').strip()


def parse_station_number(value: str) -> str | None:
    """The station number out of a header cell, or ``None``."""
    text = _cell(value)
    return text if _NUMBER.fullmatch(text) else None


def looks_like_day(value: str) -> bool:
    """Whether a first cell has the SHAPE of a data row's date.

    Shape and validity are told apart on purpose: a cell that does not even
    look like a date is a header or separator line and is silently ignored,
    while a date-shaped but impossible value (``2026-13-45``) is a broken
    data row and gets counted as skipped.
    """
    return _ISO_DAY.fullmatch(_cell(value)) is not None


def parse_iso_day(value: str) -> str | None:
    """An ISO date inside the plausibility window, or ``None``."""
    text = _cell(value)
    if not _ISO_DAY.fullmatch(text):
        return None
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        return None
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        return None
    return day.isoformat()


def parse_csv(text: str) -> tuple[str | None, dict[str, float], int]:
    """One export file → ``(station number, {ISO day: level}, skipped rows)``.

    Total by design: a file without a readable station number or without a
    single usable row yields ``(None, {}, …)`` instead of raising. The header
    block and the data rows are told apart by shape, not by position — the
    number of preamble lines is not something to rely on.
    """
    number: str | None = None
    readings: dict[str, float] = {}
    skipped = 0
    for row in csv.reader(text.splitlines(), delimiter=_DELIMITER):
        if len(row) < 2:
            continue
        label = _cell(row[0]).lower().rstrip(":")
        if number is None and label == _NUMBER_LABEL:
            number = parse_station_number(row[1])
            continue
        if not looks_like_day(row[0]):
            continue  # a header or separator line, not a broken reading
        day = parse_iso_day(row[0])
        if day is None:
            skipped += 1
            continue
        level = parse_decimal(row[1])
        if level is None:
            skipped += 1
            continue
        readings[day] = level
    return number, readings, skipped


# -- walking the archive -----------------------------------------------------


def is_safe_member_name(name: str) -> bool:
    """Whether a ZIP entry name is harmless (zip slip, defence in depth).

    Nothing is ever written to disk, so this cannot be exploited here — but
    an archive that tries is not one whose contents should be trusted, and
    the check keeps the guarantee local instead of depending on the caller.
    """
    if not name or name.startswith(("/", "\\")):
        return False
    if ntpath.splitdrive(name)[0]:  # "C:\…"
        return False
    segments = name.replace("\\", "/").split("/")
    return not any(segment in ("", ".", "..") for segment in segments)


def _iter_members(archive: zipfile.ZipFile, result: ImportResult) -> Iterator[tuple[str, str]]:
    """Yield ``(name, text)`` for every readable data member of the archive."""
    total = 0
    for index, info in enumerate(archive.infolist()):
        if index >= MAX_ENTRIES:
            logger.warning("Archive holds more than %d entries; the rest is ignored", MAX_ENTRIES)
            return
        if info.is_dir():
            continue
        name = info.filename
        if not is_safe_member_name(name):
            logger.warning("Ignoring an archive entry with an unsafe path")
            result.skipped_files += 1
            continue
        if not name.lower().endswith(DATA_SUFFIXES):
            continue
        if info.file_size > MAX_MEMBER_BYTES:
            logger.warning("Ignoring oversized archive entry %s", name)
            result.skipped_files += 1
            continue
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            logger.warning("Archive exceeds the total size limit; the rest is ignored")
            return
        # Read with a cap of our own: the declared file_size may lie.
        with archive.open(info) as handle:
            raw = handle.read(MAX_MEMBER_BYTES + 1)
        if len(raw) > MAX_MEMBER_BYTES:
            logger.warning("Ignoring oversized archive entry %s", name)
            result.skipped_files += 1
            continue
        # The export is UTF-8 WITH a BOM; utf-8-sig also reads one without.
        yield name, raw.decode("utf-8-sig", errors="replace")


def import_zip_archives(storage: Storage, paths: Sequence[Path | str]) -> ImportResult:
    """Import one or more downloaded history archives into the database.

    Everything is collected first and written per station afterwards: several
    files of the same station belong to one upsert, and a run that hits a
    broken archive halfway through still stores what it could read.
    """
    result = ImportResult()
    known = {station.number for station in storage.list_groundwater_stations()}
    collected: dict[str, dict[str, float]] = {}
    unknown: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        try:
            with zipfile.ZipFile(path) as archive:
                result.archives += 1
                for name, text in _iter_members(archive, result):
                    _collect_member(name, text, known, collected, unknown, result)
        except (OSError, zipfile.BadZipFile) as exc:
            message = f"{path.name}: {exc.__class__.__name__}"
            logger.warning("Archive %s could not be read: %s", path.name, exc.__class__.__name__)
            result.errors.append(message)
    for number, days in sorted(collected.items()):
        rows = sorted(days.items())
        storage.upsert_groundwater_readings(number, rows)
        result.readings += len(rows)
    result.stations = len(collected)
    result.unknown_stations = sorted(unknown)
    return result


def _collect_member(
    name: str,
    text: str,
    known: set[str],
    collected: dict[str, dict[str, float]],
    unknown: set[str],
    result: ImportResult,
) -> None:
    """Fold one CSV member into the collected readings."""
    number, readings, skipped = parse_csv(text)
    result.skipped_rows += skipped
    if number is None or not readings:
        logger.warning("No usable readings in archive entry %s", name)
        result.skipped_files += 1
        return
    if number not in known:
        # Not an error: Roland may well download more than the stations the
        # map shows. Counted so the summary can say so.
        unknown.add(number)
        result.skipped_files += 1
        return
    result.files += 1
    collected.setdefault(number, {}).update(readings)


# -- reporting ---------------------------------------------------------------


def summary_text(result: ImportResult) -> str:
    """A short German summary of an import run (printed by the script)."""
    archives = "1 Archiv" if result.archives == 1 else f"{result.archives} Archive"
    files = "1 Datei" if result.files == 1 else f"{result.files} Dateien"
    stations = "1 Messstelle" if result.stations == 1 else f"{result.stations} Messstellen"
    parts = [f"{archives}, {files}, {stations}, {result.readings} Werte gespeichert."]
    if result.skipped_files:
        word = "Datei" if result.skipped_files == 1 else "Dateien"
        parts.append(f"{result.skipped_files} {word} übersprungen.")
    if result.skipped_rows:
        word = "Zeile" if result.skipped_rows == 1 else "Zeilen"
        parts.append(f"{result.skipped_rows} {word} übersprungen.")
    if result.unknown_stations:
        parts.append("Unbekannte Messstellen: " + ", ".join(result.unknown_stations) + ".")
    if result.errors:
        parts.append("Nicht lesbar: " + ", ".join(result.errors) + ".")
    return " ".join(parts)
