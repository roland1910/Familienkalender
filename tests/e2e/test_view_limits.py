"""E2E tests for rendering limits (defense against event floods).

Foreign calendars can deliver arbitrarily many events. The views must cap
what they render: the week view limits all-day lanes and timed events per
day, the day popover limits its list length. The tests inject synthetic
event floods by intercepting the events API in the browser.
"""

from datetime import date, timedelta

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _monday() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def _all_day_event(uid: str, title: str, start: date, days: int) -> dict:
    return {
        "source_id": 1,
        "source_name": "Demo",
        "uid": uid,
        "title": title,
        "start": start.isoformat(),
        "end": (start + timedelta(days=days)).isoformat(),
        "all_day": True,
        "location": None,
    }


def _timed_event(uid: str, title: str, day: date, hour: int, minute: int = 0) -> dict:
    start = f"{day.isoformat()}T{hour:02d}:{minute:02d}:00"
    end_minute = minute + 30
    end = f"{day.isoformat()}T{hour + end_minute // 60:02d}:{end_minute % 60:02d}:00"
    return {
        "source_id": 1,
        "source_name": "Demo",
        "uid": uid,
        "title": title,
        "start": start,
        "end": end,
        "all_day": False,
        "location": None,
    }


def _serve_events(page: Page, events: list[dict]) -> None:
    page.route("**/api/events*", lambda route: route.fulfill(json={"events": events}))


def _open_week_view(page: Page, server_url: str) -> None:
    page.goto(server_url)
    expect(page.locator("#calendar .month-view")).to_be_visible()
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()


def test_week_allday_lanes_are_capped_with_more_button(page: Page, server_url: str) -> None:
    """More than 5 overlapping all-day bars collapse into a '+N weitere' row."""
    monday = _monday()
    events = [
        _all_day_event(f"lane-{index}", f"Lane-Termin {index}", monday, 3)
        for index in range(1, 9)  # 8 overlapping bars -> 8 lanes
    ]
    _serve_events(page, events)
    _open_week_view(page, server_url)

    # 4 lanes of bars plus the "+N weitere" row = 5 rows total.
    expect(page.locator(".allday-bar")).to_have_count(4)
    more = page.locator(".week-allday .more-button")
    expect(more).to_have_count(3)  # Mon, Tue, Wed each carry hidden bars
    expect(more.first).to_have_text("+4 weitere")

    more.first.click()
    popover = page.locator("#day-popover")
    expect(popover).to_be_visible()
    for index in range(1, 9):
        expect(popover).to_contain_text(f"Lane-Termin {index}")


def test_week_allday_shows_all_lanes_when_at_most_five(page: Page, server_url: str) -> None:
    monday = _monday()
    events = [
        _all_day_event(f"lane-{index}", f"Lane-Termin {index}", monday, 3)
        for index in range(1, 6)  # exactly 5 lanes -> no cap
    ]
    _serve_events(page, events)
    _open_week_view(page, server_url)
    expect(page.locator(".allday-bar")).to_have_count(5)
    expect(page.locator(".week-allday .more-button")).to_have_count(0)


def test_week_timed_events_per_day_are_capped_with_hint(page: Page, server_url: str) -> None:
    """A day with more than 30 timed events renders 30 plus a '+N weitere' hint."""
    monday = _monday()
    events = [
        _timed_event(
            f"timed-{index}", f"Zeit-Termin {index}", monday, 6 + index // 4, 15 * (index % 4)
        )
        for index in range(1, 36)  # 35 timed events on one day
    ]
    _serve_events(page, events)
    _open_week_view(page, server_url)

    column = page.locator(f'.week-day-column[data-date="{monday.isoformat()}"]')
    expect(column.locator(".timed-event")).to_have_count(30)
    hint = column.locator(".more-button")
    expect(hint).to_have_count(1)
    expect(hint).to_have_text("+5 weitere")

    hint.click()
    popover = page.locator("#day-popover")
    expect(popover).to_be_visible()
    expect(popover).to_contain_text("Zeit-Termin 35")


def test_day_popover_list_is_capped_at_100_entries(page: Page, server_url: str) -> None:
    """The popover renders at most 100 entries plus an '… und N weitere' line."""
    monday = _monday()
    events = [
        _timed_event(f"flood-{index}", f"Flut-Termin {index}", monday, 6, index % 60)
        for index in range(1, 151)  # 150 events on one day
    ]
    _serve_events(page, events)
    page.goto(server_url)
    expect(page.locator("#calendar .month-view")).to_be_visible()

    cell = page.locator(f'.day-cell[data-date="{monday.isoformat()}"]')
    cell.locator(".more-button").click()
    popover = page.locator("#day-popover")
    expect(popover).to_be_visible()
    expect(popover.locator(".popover-item")).to_have_count(100)
    expect(popover.locator(".popover-more")).to_have_text("… und 50 weitere")
