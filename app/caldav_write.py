"""CalDAV write client for the add-on's outgoing syncs into Nextcloud.

This is the only module that WRITES into Roland's Nextcloud. It creates,
updates and deletes single VEVENT resources over plain CalDAV (PUT/DELETE),
reusing the read client's infrastructure (httpx, basic auth, the streamed
response-size limit in app.sources.limits and the SSRF URL validation in
app.url_validation) instead of duplicating it.

Two different syncs write into the same collection — the mirror sync
(Xalt → MoreValue, app.mirror_sync) and the birthday sync (contact birthdays
→ MoreValue, app.birthday_sync). They are kept apart by a ``WriteNamespace``:
each owns a UID prefix and a marker property of its own, and both the
listing and the "is this ours?" test are scoped to the instance's namespace.
Neither sync can therefore see — let alone delete as an orphan — anything the
other one wrote.

Hard security invariant (enforced here, covered by tests):

- Every resource the add-on writes carries the marker properties
  ``X-FAMILIENKALENDER-OWNER:1`` (constant, shared by all namespaces — it is
  what the READ client skips on, so nothing we wrote is ever read back) and
  the namespace's own ``X-FAMILIENKALENDER-<PURPOSE>:<key>``.
- Resources are written under OUR OWN, derived UID/filename
  (``familienkalender-<purpose>-<hash>.ics``) — never under a foreign
  appointment's UID, so a name collision with a real Nextcloud event is
  impossible.
- Every request URL must resolve INSIDE the configured calendar collection
  (``_ensure_own_url``) and must pass ``validate_source_url``. A server that
  answers a REPORT with an href pointing somewhere else can therefore never
  make the add-on write or delete outside that collection. The check looks at
  the DECODED path too, so a percent-encoded traversal (``%2e%2e``, which
  ``httpx.URL.join`` does not normalize) cannot slip past the prefix test.
- Discovery of the add-on's own copies (``list_own_resources``) returns ONLY
  components that carry the owner marker AND a UID from THIS INSTANCE's
  derived namespace; everything else is dropped while parsing, so neither a
  foreign appointment nor the other sync's objects are ever offered to the
  caller as a deletion candidate. Both halves are required on purpose: the
  marker property name is public, so an injected marker must not be enough —
  the UID of a genuine, already existing appointment can never be one of ours.

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
from datetime import UTC, date, datetime
from typing import Any

import httpx
import icalendar
from defusedxml import ElementTree as SafeET

from app.models import (
    BIRTHDAY_MARKER_PROP,
    MIRROR_MARKER_PROP,
    MIRROR_OWNER_PROP,
    MIRROR_OWNER_VALUE,
    CalendarEvent,
    as_local_datetime,
)
from app.sources import limits
from app.url_validation import SourceURLError, validate_source_url

logger = logging.getLogger(__name__)

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"

PRODID = "-//Familienkalender//Spiegel//DE"

# Filename/UID prefix of every mirrored copy. Deliberately unmistakable: it
# makes an accidental collision with a real Nextcloud resource impossible and
# lets a human recognize the add-on's own objects in the calendar folder.
MIRROR_UID_PREFIX = "familienkalender-mirror-"

# ... and of every yearly birthday series. A SEPARATE prefix is what keeps the
# two syncs from treating each other's objects as their own orphans.
BIRTHDAY_UID_PREFIX = "familienkalender-birthday-"

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
class WriteNamespace:
    """Identity of ONE outgoing sync inside a CalDAV collection.

    Three sync directions write into Roland's calendars, two of them into the
    same Nextcloud collection. Each gets its own UID prefix and marker
    property here, so "is this object mine?" is answered per sync and never
    per add-on: the mirror sync can never delete a birthday series as an
    orphan, and the birthday sync can never delete a mirrored copy.
    """

    uid_prefix: str
    marker_prop: str


MIRROR_NAMESPACE = WriteNamespace(MIRROR_UID_PREFIX, MIRROR_MARKER_PROP)
BIRTHDAY_NAMESPACE = WriteNamespace(BIRTHDAY_UID_PREFIX, BIRTHDAY_MARKER_PROP)


@dataclass(frozen=True, slots=True)
class OwnResource:
    """One of the add-on's own resources discovered in the target calendar.

    Only ever built for components that carry the owner marker AND a UID from
    the discovering client's namespace, so an instance of this class is by
    construction one of that sync's own resources — never a foreign
    appointment and never the other sync's.

    ``start`` is the component's DTSTART (a plain date for all-day/recurring
    entries). The birthday sync needs it to decide whether an unmapped series
    may be deleted at all; the mirror sync ignores it.
    """

    url: str
    etag: str
    source_key: str
    summary: str
    start: datetime | date | None = None


def derived_uid(source_key: str, *, namespace: WriteNamespace = MIRROR_NAMESPACE) -> str:
    """The add-on's own, derived UID for the object of ``source_key``.

    Deterministic (so a lost mapping row can rediscover the same resource)
    and derived — the foreign appointment's own UID is never reused as the
    UID or filename of a resource we write.
    """
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    return f"{namespace.uid_prefix}{digest[:_UID_DIGEST_CHARS]}"


def resource_name(
    source_key: str, *, namespace: WriteNamespace = MIRROR_NAMESPACE
) -> str:
    """The .ics filename of the object of ``source_key`` inside the collection."""
    return f"{derived_uid(source_key, namespace=namespace)}.ics"


def collection_url(calendar_url: str) -> str:
    """The calendar collection URL, normalized to a trailing slash."""
    return calendar_url if calendar_url.endswith("/") else f"{calendar_url}/"


def resource_url(
    calendar_url: str, source_key: str, *, namespace: WriteNamespace = MIRROR_NAMESPACE
) -> str:
    """Absolute URL of the object of ``source_key`` inside the collection."""
    return (
        f"{collection_url(calendar_url)}"
        f"{resource_name(source_key, namespace=namespace)}"
    )


def build_ical(
    source_key: str,
    event: CalendarEvent,
    *,
    now: datetime | None = None,
    namespace: WriteNamespace = MIRROR_NAMESPACE,
    yearly: bool = False,
) -> bytes:
    """The complete VCALENDAR document for one written object.

    Carries title, times and location only. Description and attendees are
    deliberately NOT copied: the mirror exists so Roland sees his Xalt
    appointments in MoreValue, and copying meeting bodies/participant lists
    into a second system would spread data that nobody needs there.

    ``yearly`` adds ``RRULE:FREQ=YEARLY``: birthdays are recurring by nature,
    so one series per person replaces a fresh copy every year.
    """
    calendar = icalendar.Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    component = icalendar.Event()
    component.add("uid", derived_uid(source_key, namespace=namespace))
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
    if yearly:
        component.add("rrule", {"freq": "yearly"})
    if event.location:
        component.add("location", event.location)
    component.add(namespace.marker_prop, source_key)
    component.add(MIRROR_OWNER_PROP, MIRROR_OWNER_VALUE)
    calendar.add_component(component)
    return calendar.to_ical()


def _marked_component(
    ics_text: str, namespace: WriteNamespace
) -> icalendar.cal.Component | None:
    """The first VEVENT belonging to ``namespace``, else None.

    This is the gate that decides whether a resource found in the target
    calendar belongs to THIS sync, and it demands BOTH halves of the
    identity:

    - the owner marker property, and
    - a UID inside the namespace's derived prefix
      (``familienkalender-mirror-…`` resp. ``familienkalender-birthday-…``).

    The marker alone is not enough: the property name is public (the project
    is open source) and any X-property can ride into Roland's Nextcloud on a
    foreign invitation, an import or a shared calendar. The UID, in contrast,
    is fixed when an appointment is created — a genuine, pre-existing
    appointment can never carry ours, so a marker somebody else injected can
    never make a real appointment a write or delete candidate. The prefix
    test additionally separates the two syncs from each other.
    """
    calendar = icalendar.Calendar.from_ical(ics_text)
    for component in calendar.walk("VEVENT"):
        if component.get(MIRROR_OWNER_PROP) is None:
            continue
        if not str(component.get("UID", "")).startswith(namespace.uid_prefix):
            continue
        return component
    return None


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


def _has_dot_segment(url: str) -> bool:
    """Whether the DECODED path of ``url`` contains a "." or ".." segment.

    ``httpx.URL.path`` percent-decodes, so ``%2e%2e`` surfaces here as ``..``
    — which is exactly the case a raw string prefix check cannot see.
    """
    try:
        path = httpx.URL(url).path
    except (ValueError, TypeError):
        return True  # unparseable: treat as unsafe
    return any(segment in (".", "..") for segment in path.split("/"))


class CaldavWriteClient:
    """Authenticated CalDAV writer scoped to ONE calendar collection.

    One instance per sync run. The collection URL comes from a configured
    CalDAV source (already SSRF-validated when it was saved) and is
    re-validated here, defensively, exactly like the read client does.
    """

    def __init__(
        self,
        config: dict[str, Any],
        client: httpx.AsyncClient,
        *,
        namespace: WriteNamespace = MIRROR_NAMESPACE,
    ) -> None:
        self._collection = collection_url(config["calendar_url"])
        validate_source_url(self._collection)
        self._auth = (config["username"], config["app_password"])
        self._client = client
        self._namespace = namespace

    @property
    def collection(self) -> str:
        return self._collection

    @property
    def namespace(self) -> WriteNamespace:
        return self._namespace

    def _ensure_own_url(self, url: str) -> str:
        """Reject any URL outside the configured collection.

        Applied before every single request. Together with the marker check
        in ``list_own_resources`` this is what makes "the add-on only ever
        touches its own resources" a property of the transport layer and not
        just of the calling logic.

        The plain prefix check is not sufficient on its own: ``httpx.URL.join``
        normalizes a literal ``..`` away, but leaves a PERCENT-ENCODED
        ``%2e%2e`` untouched — such a URL passes ``startswith`` while the
        server resolves it one level up, i.e. outside the collection. The
        decoded path is therefore checked for dot segments as well.
        """
        if not url.startswith(self._collection) or _has_dot_segment(url):
            raise CaldavWriteError(
                "Ziel-Adresse liegt außerhalb des konfigurierten Kalenders."
            )
        validate_source_url(url)
        return url

    def owns_url(self, url: str) -> bool:
        """Whether ``url`` is a valid address inside this collection.

        The non-raising twin of ``_ensure_own_url``, for callers that want to
        DISCARD a stale address (e.g. a mapping row left over from a previous
        target calendar) instead of aborting their run.
        """
        try:
            self._ensure_own_url(url)
        except (CaldavWriteError, SourceURLError, ValueError):
            return False
        return True

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

    def _body(self, source_key: str, event: CalendarEvent, yearly: bool) -> bytes:
        return build_ical(
            source_key, event, namespace=self._namespace, yearly=yearly
        )

    async def create_event(
        self, url: str, source_key: str, event: CalendarEvent, *, yearly: bool = False
    ) -> str:
        """Create a new own resource; returns the new ETag (may be empty).

        ``If-None-Match: *`` makes the server refuse if something already
        lives at that URL — the add-on never overwrites an existing resource
        while creating.
        """
        return await self._put(
            url, self._body(source_key, event, yearly), {"If-None-Match": "*"}
        )

    async def update_event(
        self,
        url: str,
        source_key: str,
        event: CalendarEvent,
        *,
        etag: str,
        yearly: bool = False,
    ) -> str:
        """Overwrite an existing own resource; returns the new ETag.

        ``If-Match`` guards against clobbering a concurrent foreign change.
        An empty/unknown ETag falls back to ``*`` ("must still exist"), which
        is still stricter than an unconditional PUT.
        """
        return await self._put(
            url, self._body(source_key, event, yearly), {"If-Match": etag or "*"}
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
        """This namespace's own resources in [window_start, window_end).

        Uses the same time-range calendar-query the read client uses, then
        keeps ONLY components carrying the owner marker AND a UID from this
        client's namespace. Callers pass a window that certainly contains an
        occurrence of everything they wrote: the mirror window for one-off
        copies, and a full year for the yearly birthday series (a recurring
        entry only matches a time-range that one of its occurrences falls in).
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
            component = _marked_component(data_el.text, self._namespace)
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
            source_key=_text(component.get(self._namespace.marker_prop)),
            summary=_text(component.get("SUMMARY")),
            start=_component_start(component),
        )


def _component_start(component: icalendar.cal.Component) -> datetime | date | None:
    """The component's DTSTART, or None when it is missing/unreadable.

    Foreign-shaped data must never abort the listing: a resource without a
    readable start is still reported, just without one.
    """
    try:
        value = component.get("DTSTART")
        return value.dt if value is not None else None
    except (AttributeError, ValueError, TypeError):
        return None


def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
