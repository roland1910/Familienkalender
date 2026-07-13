"""CalDAV client for Nextcloud calendars.

Talks CalDAV directly over httpx (REPORT calendar-query with a time-range
filter) instead of using the ``caldav`` library: we only need two requests
(list calendars, fetch events), httpx is already a dependency, and the
``caldav`` package would pull in requests + lxml and a synchronous API.

Recurrence expansion happens client side with ``recurring-ical-events``
(Nextcloud's time-range filter returns the master event with its RRULE and
override instances, not expanded occurrences).

Source config keys: ``url`` (server base URL), ``username``,
``app_password``, ``calendar_url`` (the calendar collection to sync).
"""

import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import icalendar
import recurring_ical_events
from defusedxml import ElementTree as SafeET

from app.models import LOCAL_TZ, CalendarEvent, is_busy_block_title
from app.sources import limits
from app.url_validation import validate_source_url

logger = logging.getLogger(__name__)

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"

_CALENDAR_QUERY_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT">
        <c:time-range start="{start}" end="{end}"/>
      </c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>
"""

_PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
    <c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>
"""


def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _ensure_aware(value: datetime) -> datetime:
    """Attach the local timezone to floating (naive) iCalendar times."""
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TZ)
    return value


def _component_to_event(component: icalendar.cal.Component) -> CalendarEvent:
    start = component.decoded("DTSTART")
    end = component.decoded("DTEND") if "DTEND" in component else None
    all_day = not isinstance(start, datetime)
    if end is None:
        # RFC 5545: without DTEND/DURATION an all-day event lasts one day,
        # a timed event has zero duration.
        end = start + timedelta(days=1) if all_day else start
    if not all_day:
        start = _ensure_aware(start)
        end = _ensure_aware(end)
    location = component.get("LOCATION")
    return CalendarEvent(
        uid=str(component.get("UID", "")),
        title=str(component.get("SUMMARY", "")),
        start=start,
        end=end,
        all_day=all_day,
        location=str(location) if location else None,
    )


def _extract_events(
    ics_text: str, window_start: datetime, window_end: datetime
) -> list[CalendarEvent]:
    calendar = icalendar.Calendar.from_ical(ics_text)
    occurrences = recurring_ical_events.of(calendar).between(window_start, window_end)
    events: list[CalendarEvent] = []
    for component in occurrences:
        event = _component_to_event(component)
        # Skip the add-on's own "Busy MV" blocks. Roland runs an external tool
        # that syncs his Xalt Google calendar back into MoreValue (Nextcloud),
        # so the "Busy MV" blocks we write into Xalt reappear here. Reading them
        # back would duplicate the MoreValue appointments in the views/feed and,
        # worse, feed them into the busy-sync again (mirror loop). CalDAV/iCal
        # carries no private marker like Google, so the skip is purely by the
        # fixed title (matched exactly, normalized) — see app.models.
        if is_busy_block_title(event.title):
            continue
        events.append(event)
    return events


def _calendar_data_texts(multistatus_xml: bytes) -> list[str]:
    # defusedxml: server responses are untrusted XML (entity-expansion
    # bombs, external entities); plain ET is only used for type hints.
    root = SafeET.fromstring(multistatus_xml)
    return [
        element.text
        for element in root.iter(f"{{{CALDAV_NS}}}calendar-data")
        if element.text and element.text.strip()
    ]


def _auth(config: dict[str, Any]) -> tuple[str, str]:
    return (config["username"], config["app_password"])


async def fetch_events(
    config: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[CalendarEvent]:
    """Fetch all event occurrences in [window_start, window_end).

    Recurring events are expanded into individual occurrences; overridden
    instances (RECURRENCE-ID) replace their regular occurrence.
    """
    # Defensive: the config may have reached the DB without passing the
    # admin API, so the target is validated before every single fetch.
    validate_source_url(config["calendar_url"])
    body = _CALENDAR_QUERY_TEMPLATE.format(
        start=_utc_stamp(window_start), end=_utc_stamp(window_end)
    )
    if client is None:
        async with httpx.AsyncClient(timeout=30) as own_client:
            return await fetch_events(config, window_start, window_end, client=own_client)
    request = client.build_request(
        "REPORT",
        config["calendar_url"],
        content=body.encode(),
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
    )
    response, content = await limits.send_limited(client, request, auth=_auth(config))
    response.raise_for_status()
    events: list[CalendarEvent] = []
    parse_errors: list[str] = []
    for ics_text in _calendar_data_texts(content):
        # One broken object (e.g. a malformed foreign invitation) must not
        # abort the whole calendar fetch: skip it and keep the rest.
        try:
            extracted = _extract_events(ics_text, window_start, window_end)
        except Exception as exc:
            # Only the exception type is logged — messages of parser
            # exceptions may quote raw (foreign, untrusted) event data.
            parse_errors.append(type(exc).__name__)
            continue
        events.extend(extracted)
        limits.check_event_count(len(events))
    if parse_errors:
        logger.warning(
            "Skipped %d unparseable calendar object(s) (%s)",
            len(parse_errors),
            ", ".join(sorted(set(parse_errors))),
        )
    return events


def _calendar_home_url(config: dict[str, Any]) -> str:
    base = config["url"].rstrip("/")
    return f"{base}/remote.php/dav/calendars/{config['username']}/"


def _supports_events(prop: ET.Element) -> bool:
    component_set = prop.find(f"{{{CALDAV_NS}}}supported-calendar-component-set")
    if component_set is None:
        return True
    names = {comp.get("name") for comp in component_set.iter(f"{{{CALDAV_NS}}}comp")}
    return "VEVENT" in names


async def list_calendars(
    config: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, str]]:
    """List the user's event calendars (name and collection URL)."""
    # Defensive, like in fetch_events: never talk to forbidden targets.
    validate_source_url(config["url"])
    if client is None:
        async with httpx.AsyncClient(timeout=30) as own_client:
            return await list_calendars(config, client=own_client)
    home_url = _calendar_home_url(config)
    request = client.build_request(
        "PROPFIND",
        home_url,
        content=_PROPFIND_CALENDARS.encode(),
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
    )
    response, content = await limits.send_limited(client, request, auth=_auth(config))
    response.raise_for_status()
    root = SafeET.fromstring(content)
    calendars = []
    for response_el in root.iter(f"{{{DAV_NS}}}response"):
        prop = response_el.find(f"{{{DAV_NS}}}propstat/{{{DAV_NS}}}prop")
        href_el = response_el.find(f"{{{DAV_NS}}}href")
        if prop is None or href_el is None or not href_el.text:
            continue
        resourcetype = prop.find(f"{{{DAV_NS}}}resourcetype")
        is_calendar = resourcetype is not None and (
            resourcetype.find(f"{{{CALDAV_NS}}}calendar") is not None
        )
        if not is_calendar or not _supports_events(prop):
            continue
        name_el = prop.find(f"{{{DAV_NS}}}displayname")
        href = href_el.text.strip()
        name = (name_el.text or "").strip() if name_el is not None else ""
        if not name:
            name = href.rstrip("/").rsplit("/", 1)[-1]
        calendars.append({"name": name, "url": str(httpx.URL(home_url).join(href))})
    return calendars
