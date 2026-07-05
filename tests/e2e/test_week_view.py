"""E2E tests for the week view (time grid, positioned events, all-day bars,
auto-zoom hour height)."""

from datetime import date, timedelta

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.helpers import goto_calendar, goto_week_containing

pytestmark = pytest.mark.e2e

MIN_HOUR_HEIGHT_PX = 24


def hour_height(page: Page) -> float:
    """The auto-zoom hour height (--hour-height) of the rendered week view.

    Resolved inside a single evaluate (not via a locator handle): the
    refresh after a view switch re-renders the week view, and a stale
    handle to the replaced node would yield empty computed styles.
    """
    return page.evaluate(
        """() => {
            const view = document.querySelector(".week-view");
            return parseFloat(getComputedStyle(view).getPropertyValue("--hour-height"));
        }"""
    )


def event_metrics(page: Page, column_date: str, title: str) -> dict:
    """Geometry of a timed event: offset/height in its column plus the
    hour height, read in one atomic evaluate (see hour_height)."""
    return page.evaluate(
        """([columnDate, title]) => {
            const view = document.querySelector(".week-view");
            const column = view.querySelector(
                `.week-day-column[data-date="${columnDate}"]`
            );
            const event = [...column.querySelectorAll(".timed-event")].find(
                (node) => node.textContent.includes(title)
            );
            const rect = event.getBoundingClientRect();
            return {
                top: rect.top - column.getBoundingClientRect().top,
                height: rect.height,
                hour: parseFloat(getComputedStyle(view).getPropertyValue("--hour-height")),
            };
        }""",
        [column_date, title],
    )


def test_week_view_shows_seven_columns_and_time_grid(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    expect(page.locator(".week-day-column")).to_have_count(7)
    labels = page.locator(".week-day-label")
    expect(labels).to_have_count(7)
    expect(labels.first).to_contain_text("Mo")
    expect(page.locator(".hour-label", has_text="08:00")).to_be_attached()
    expect(page.locator("#period-title")).to_contain_text("KW")


def test_night_hours_are_collapsed_without_early_events(
    page: Page, server_url: str
) -> None:
    """No demo event in the current week starts before 08:00, so the grid
    hides 00:00-08:00 entirely and starts at 08:00 (replaces the old,
    flaky initial-scroll-to-morning mechanism)."""
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    labels = page.locator(".hour-label")
    expect(labels.first).to_have_text("08:00")
    expect(page.locator(".hour-label", has_text="07:00")).to_have_count(0)
    expect(labels).to_have_count(24 - 8)  # 08:00 .. 23:00, evening kept
    fit = page.evaluate(
        """() => {
            const view = document.querySelector(".week-view");
            return {
                gridHeight: view.querySelector(".week-grid").getBoundingClientRect().height,
                hour: parseFloat(getComputedStyle(view).getPropertyValue("--hour-height")),
            };
        }"""
    )
    assert abs(fit["gridHeight"] - (24 - 8) * fit["hour"]) <= 1


def test_early_event_expands_the_grid_to_its_full_hour(
    page: Page, server_url: str
) -> None:
    """The demo week three weeks ahead has a 06:30 event: the grid starts
    at 06:00 (full hour of the earliest event) and positions it there."""
    target = date.today() + timedelta(days=21)
    goto_week_containing(page, server_url, target)
    labels = page.locator(".hour-label")
    expect(labels.first).to_have_text("06:00")
    expect(page.locator(".hour-label", has_text="05:00")).to_have_count(0)
    column = page.locator(f'.week-day-column[data-date="{target.isoformat()}"]')
    event = column.locator(".timed-event", has_text="Frühdienst")
    expect(event).to_be_visible()
    # 06:30 in a grid starting at 06:00 -> half an hour row from the top.
    metrics = event_metrics(page, target.isoformat(), "Frühdienst")
    assert abs(metrics["top"] - 0.5 * metrics["hour"]) <= 1


def test_timed_event_is_positioned_by_time(page: Page, server_url: str) -> None:
    goto_week_containing(page, server_url, date.today())
    column = page.locator(f'.week-day-column[data-date="{date.today().isoformat()}"]')
    event = column.locator(".timed-event", has_text="Zahnarzt Emil")
    event.scroll_into_view_if_needed()
    expect(event).to_be_visible()
    metrics = event_metrics(page, date.today().isoformat(), "Zahnarzt Emil")
    # Starts 15:00 in a grid starting at 08:00 and lasts one hour.
    assert abs(metrics["top"] - (15 - 8) * metrics["hour"]) <= 1
    assert abs(metrics["height"] - metrics["hour"]) <= 1


def test_multi_day_event_appears_as_all_day_bar(page: Page, server_url: str) -> None:
    visit_start = date.today() + timedelta(days=5)
    goto_week_containing(page, server_url, visit_start)
    bar = page.locator(".allday-bar", has_text="Oma & Opa zu Besuch")
    expect(bar).to_be_visible()


def test_filtered_daytime_meeting_is_hidden(page: Page, server_url: str) -> None:
    """A plain daytime meeting from a filtered source never reaches the UI,
    while the evening event of the same source is shown."""
    target = date.today() + timedelta(days=1)
    goto_week_containing(page, server_url, target)
    expect(page.locator(".timed-event", has_text="Kundentermin München")).to_be_visible()
    expect(page.get_by_text("Weekly Sync")).to_have_count(0)


def test_sources_get_distinct_deterministic_colors(page: Page, server_url: str) -> None:
    target = date.today() + timedelta(days=1)
    goto_week_containing(page, server_url, target)
    marina_chip = page.locator(".timed-event", has_text="Böser Termin")
    kunde_chip = page.locator(".timed-event", has_text="Kundentermin München")
    marina_color = marina_chip.evaluate("node => getComputedStyle(node).backgroundColor")
    kunde_color = kunde_chip.evaluate("node => getComputedStyle(node).backgroundColor")
    assert marina_color != kunde_color


# -- auto-zoom: the visible hours fill the available height -----------------


def _scroll_metrics(page: Page) -> tuple[float, float]:
    # Atomic read on the currently attached node (see hour_height).
    return page.evaluate(
        """() => {
            const scroll = document.querySelector(".week-scroll");
            return [scroll.scrollHeight, scroll.clientHeight];
        }"""
    )


def test_auto_zoom_fits_grid_without_scrolling_on_kiosk(page: Page, server_url: str) -> None:
    """On the 1920x1080 kiosk viewport (default context) the whole visible
    hour range must fit without a vertical scrollbar."""
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    scroll_height, client_height = _scroll_metrics(page)
    assert scroll_height <= client_height, "week grid must not scroll on the kiosk"
    # The hour height is derived from the space, not the old fixed 60px:
    # 16 visible hours (08:00-24:00) exactly fill the grid area.
    hour = hour_height(page)
    assert hour == client_height // 16
    assert hour >= MIN_HOUR_HEIGHT_PX


def test_auto_zoom_reacts_to_window_resize(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    hour_before = hour_height(page)
    page.set_viewport_size({"width": 1920, "height": 700})
    # The resize handler is debounced; poll until it re-fitted the grid.
    page.wait_for_function(
        """expected => {
            const view = document.querySelector(".week-view");
            const hour = parseFloat(getComputedStyle(view).getPropertyValue("--hour-height"));
            return hour < expected;
        }""",
        arg=hour_before,
    )
    scroll_height, client_height = _scroll_metrics(page)
    assert scroll_height <= client_height


def test_auto_zoom_clamps_to_min_height_and_scrolls(
    browser: Browser, server_url: str
) -> None:
    """On a very short viewport the hour height stops at the readability
    minimum and the grid scrolls vertically as a fallback."""
    context = browser.new_context(viewport={"width": 1280, "height": 420}, has_touch=True)
    page = context.new_page()
    try:
        goto_calendar(page, server_url)
        page.locator("#btn-week").click()
        expect(page.locator(".week-view")).to_be_visible()
        assert hour_height(page) == MIN_HOUR_HEIGHT_PX
        scroll_height, client_height = _scroll_metrics(page)
        assert scroll_height > client_height, "clamped grid must fall back to scrolling"
    finally:
        context.close()
