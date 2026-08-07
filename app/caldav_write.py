"""CalDAV write client for the one-way Xalt -> MoreValue mirror sync.

This is the only module that WRITES into Roland's Nextcloud. It creates,
updates and deletes single VEVENT resources over plain CalDAV (PUT/DELETE),
reusing the read client's infrastructure (httpx, basic auth, the streamed
response-size limit in app.sources.limits and the SSRF URL validation in
app.url_validation) instead of duplicating it.

Hard security invariant (enforced here, covered by tests):

- Every resource the add-on writes carries the marker properties
  ``X-FAMILIENKALENDER-OWNER:1`` and ``X-FAMILIENKALENDER-MIRROR:<source-key>``
  in its VEVENT (constants in app.models, shared with the read client).
- Resources are written under OUR OWN, derived UID/filename
  (``familienkalender-mirror-<hash>.ics``) — never under a foreign
  appointment's UID, so a name collision with a real Nextcloud event is
  impossible.
- Every request URL must resolve INSIDE the configured calendar collection
  (``_ensure_own_url``) and must pass ``validate_source_url``. A server that
  answers a REPORT with an href pointing somewhere else can therefore never
  make the add-on write or delete outside that collection.
- Discovery of the add-on's own copies (``list_own_resources``) returns ONLY
  components that carry the owner marker AND a UID from the derived
  namespace; everything else is dropped while parsing, so a foreign
  appointment is never even offered to the caller as a deletion candidate.
  Both halves are required on purpose: the marker property name is public,
  so an injected marker must not be enough — the UID of a genuine, already
  existing appointment can never be one of ours.

Conditional requests: an update/delete sends ``If-Match`` with the ETag the
server last reported, a create sends ``If-None-Match: *``. A 412 therefore
means "somebody else changed/created this in the meantime" and is raised as
``CaldavConflictError`` — the sync logs it and re-reconciles on the next run
rather than blindly overwriting a foreign change.
"""

import hashlib
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import icalendar
from defusedxml import ElementTree as SafeET

from app.models import (
    MIRROR_MARKER_PROP,
    MIRROR_OWNER_PROP,
    MIRROR_OWNER_VALUE,
    CalendarEvent,
    as_local_datetime,
)
from app.sources import limits
from app.url_validation import validate_source_url

logger = logging.getLogger(__name__)

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"

PRODID = "-//Familienkalender//Spiegel//DE"

# Filename/UID prefix of every mirrored copy. Deliberately unmistakable: it
# makes an accidental collision with a real Nextcloud resource impossible and
# lets a human recognize the add-on's own objects in the calendar folder.
MIRROR_UID_PREFIX = "familienkalender-mirror-"

# Length of the hex digest in the derived UID. 32 hex chars = 128 bits of the
# SHA-256 over the source key — far beyond any collision concern for a few
# hundred appointments, while keeping the filename readable.
_UID_DIGEST_CHARS = 32

_MIRROR_QUERY_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT">
        <c:time-range start="{start}" end="{end}"/>
      </c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>
"""


class CaldavWriteError(Exception):
    """A CalDAV write/list operation failed (message is sanitized upstream)."""


class CaldavConflictError(CaldavWriteError):
    """The precondition failed (412): the resource changed underneath us."""


@dataclass(frozen=True, slots=True)
class OwnResource:
    """One mirrored copy discovered in the target calendar.

    Only ever built for components that carry the owner marker, so an
    instance of this class is by construction one of the add-on's own
    resources — never a foreign appointment.
    """

    url: str
    etag: str
    source_key: str
    summary: str


def mirror_uid(source_key: str) -> str:
    """The add-on's own, derived UID for the copy of ``source_key``.

    Deterministic (so a lost mapping row can rediscover the same resource)
    and derived — the foreign appointment's own UID is never reused as the
    UID or filename of a resource we write.
    """
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    return f"{MIRROR_UID_PREFIX}{digest[:_UID_DIGEST_CHARS]}"


def resource_name(source_key: str) -> str:
    """The .ics filename of the copy of ``source_key`` inside the collection."""
    return f"{mirror_uid(source_key)}.ics"


def collection_url(calendar_url: str) -> str:
    """The calendar collection URL, normalized to a trailing slash."""
    return calendar_url if calendar_url.endswith("/") else f"{calendar_url}/"


def resource_url(calendar_url: str, source_key: str) -> str:
    """Absolute URL of the copy of ``source_key`` inside the collection."""
    return f"{collection_url(calendar_url)}{resource_name(source_key)}"


def build_ical(
    source_key: str, event: CalendarEvent, *, now: datetime | None = None
) -> bytes:
    """The complete VCALENDAR document for one mirrored appointment.

    Carries title, times and location only. Description and attendees are
    deliberately NOT copied: the mirror exists so Roland sees his Xalt
    appointments in MoreValue, and copying meeting bodies/participant lists
    into a second system would spread data that nobody needs there.
    """
    calendar = icalendar.Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    component = icalendar.Event()
    component.add("uid", mirror_uid(source_key))
    component.add("dtstamp", (now or datetime.now(UTC)).astimezone(UTC))
    component.add("summary", event.title)
    if event.all_day:
        # Plain dates keep the iCalendar all-day semantics (VALUE=DATE with
        # an exclusive end date) exactly as stored.
        component.add("dtstart", event.start)
        component.add("dtend", event.end)
    else:
        component.add("dtstart", as_local_datetime(event.start).astimezone(UTC))
        component.add("dtend", as_local_datetime(event.end).astimezone(UTC))
    if event.location:
        component.add("location", event.location)
    component.add(MIRROR_MARKER_PROP, source_key)
    component.add(MIRROR_OWNER_PROP, MIRROR_OWNER_VALUE)
    calendar.add_component(component)
    return calendar.to_ical()


def _marked_component(ics_text: str) -> icalendar.cal.Component | None:
    """The first VEVENT that is one of the add-on's own copies, else None.

    This is the gate that decides whether a resource found in the target
    calendar belongs to the add-on, and it demands BOTH halves of the
    identity:

    - the owner marker property, and
    - a UID inside the derived namespace (``familienkalender-mirror-…``).

    The marker alone is not enough: the property name is public (the project
    is open source) and any X-property can ride into Roland's Nextcloud on a
    foreign invitation, an import or a shared calendar. The UID, in contrast,
    is fixed when an appointment is created — a genuine, pre-existing
    appointment can never carry ours, so a marker somebody else injected can
    never make a real appointment a write or delete candidate.
    """
    calendar = icalendar.Calendar.from_ical(ics_text)
    for component in calendar.walk("VEVENT"):
        if component.get(MIRROR_OWNER_PROP) is None:
            continue
        if not str(component.get("UID", "")).startswith(MIRROR_UID_PREFIX):
            continue
        return component
    return None


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


class CaldavWriteClient:
    """Authenticated CalDAV writer scoped to ONE calendar collection.

    One instance per sync run. The collection URL comes from a configured
    CalDAV source (already SSRF-validated when it was saved) and is
    re-validated here, defensively, exactly like the read client does.
    """

    def __init__(self, config: dict[str, Any], client: httpx.AsyncClient) -> None:
        self._collection = collection_url(config["calendar_url"])
        validate_source_url(self._collection)
        self._auth = (config["username"], config["app_password"])
        self._client = client

    @property
    def collection(self) -> str:
        return self._collection

    def _ensure_own_url(self, url: str) -> str:
        """Reject any URL outside the configured collection.

        Applied before every single request. Together with the marker check
        in ``list_own_resources`` this is what makes "the add-on only ever
        touches its own resources" a property of the transport layer and not
        just of the calling logic.
        """
        if not url.startswith(self._collection):
            raise CaldavWriteError(
                "Ziel-Adresse liegt außerhalb des konfigurierten Kalenders."
            )
        validate_source_url(url)
        return url

    async def _send(
        self, method: str, url: str, *, content: bytes | None = None, headers: dict
    ) -> tuple[httpx.Response, bytes]:
        request = self._client.build_request(
            method, url, content=content, headers=headers
        )
        return await limits.send_limited(self._client, request, auth=self._auth)

    async def _put(self, url: str, body: bytes, headers: dict) -> str:
        self._ensure_own_url(url)
        response, _ = await self._send(
            "PUT",
            url,
            content=body,
            headers={"Content-Type": "text/calendar; charset=utf-8", **headers},
        )
        if response.status_code == 412:
            raise CaldavConflictError(
                f"Termin wurde zwischenzeitlich geändert (HTTP {response.status_code})."
            )
        if response.status_code not in (200, 201, 204):
            raise CaldavWriteError(
                f"Termin konnte nicht geschrieben werden (HTTP {response.status_code})."
            )
        return response.headers.get("ETag", "")

    async def create_event(
        self, url: str, source_key: str, event: CalendarEvent
    ) -> str:
        """Create a new mirrored copy; returns the new ETag (may be empty).

        ``If-None-Match: *`` makes the server refuse if something already
        lives at that URL — the add-on never overwrites an existing resource
        while creating.
        """
        return await self._put(
            url, build_ical(source_key, event), {"If-None-Match": "*"}
        )

    async def update_event(
        self, url: str, source_key: str, event: CalendarEvent, *, etag: str
    ) -> str:
        """Overwrite an existing own copy; returns the new ETag.

        ``If-Match`` guards against clobbering a concurrent foreign change.
        An empty/unknown ETag falls back to ``*`` ("must still exist"), which
        is still stricter than an unconditional PUT.
        """
        return await self._put(
            url, build_ical(source_key, event), {"If-Match": etag or "*"}
        )

    async def delete_event(self, url: str, *, etag: str = "") -> None:
        """Delete an own copy. Already-gone (404/410) counts as success."""
        self._ensure_own_url(url)
        response, _ = await self._send(
            "DELETE", url, headers={"If-Match": etag or "*"}
        )
        if response.status_code == 412:
            raise CaldavConflictError(
                f"Termin wurde zwischenzeitlich geändert (HTTP {response.status_code})."
            )
        if response.status_code not in (200, 202, 204, 404, 410):
            raise CaldavWriteError(
                f"Termin konnte nicht gelöscht werden (HTTP {response.status_code})."
            )

    async def list_own_resources(
        self, window_start: datetime, window_end: datetime
    ) -> list[OwnResource]:
        """The add-on's own copies in [window_start, window_end).

        Uses the same time-range calendar-query the read client uses, then
        keeps ONLY components carrying the owner marker. The window matches
        the mirror window, i.e. exactly the range the add-on ever writes into.
        """
        body = _MIRROR_QUERY_TEMPLATE.format(
            start=_utc_stamp(window_start), end=_utc_stamp(window_end)
        )
        response, content = await self._send(
            "REPORT",
            self._collection,
            content=body.encode(),
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        if response.status_code not in (200, 207):
            raise CaldavWriteError(
                f"Kalender konnte nicht gelesen werden (HTTP {response.status_code})."
            )
        return self._parse_own_resources(content)

    def _parse_own_resources(self, multistatus_xml: bytes) -> list[OwnResource]:
        # defusedxml: the server response is untrusted XML (entity bombs,
        # external entities); plain ET is only used for type hints.
        root = SafeET.fromstring(multistatus_xml)
        found: list[OwnResource] = []
        for response_el in root.iter(f"{{{DAV_NS}}}response"):
            resource = self._parse_response_element(response_el)
            if resource is not None:
                found.append(resource)
        return found

    def _parse_response_element(self, response_el: ET.Element) -> OwnResource | None:
        href_el = response_el.find(f"{{{DAV_NS}}}href")
        if href_el is None or not href_el.text:
            return None
        data_el = next(response_el.iter(f"{{{CALDAV_NS}}}calendar-data"), None)
        if data_el is None or not data_el.text or not data_el.text.strip():
            return None
        try:
            component = _marked_component(data_el.text)
        except Exception as exc:
            # Only the exception type is logged: parser messages may quote
            # raw (foreign, untrusted) calendar data.
            logger.warning("Skipped unparseable calendar object (%s)", type(exc).__name__)
            return None
        if component is None:
            return None  # not ours — never a deletion candidate
        try:
            url = self._ensure_own_url(
                str(httpx.URL(self._collection).join(href_el.text.strip()))
            )
        except (CaldavWriteError, ValueError):
            # A server answering with an href outside the collection is not
            # something we follow — skip it rather than write there.
            logger.warning("Ignoring calendar resource outside the collection")
            return None
        etag_el = next(response_el.iter(f"{{{DAV_NS}}}getetag"), None)
        return OwnResource(
            url=url,
            etag=(etag_el.text or "").strip() if etag_el is not None else "",
            source_key=_text(component.get(MIRROR_MARKER_PROP)),
            summary=_text(component.get("SUMMARY")),
        )


def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
