"""Shared helpers for the browser E2E tests."""

from datetime import date, timedelta

from playwright.sync_api import Page, expect

MONTH_NAMES_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def month_title(day: date) -> str:
    return f"{MONTH_NAMES_DE[day.month - 1]} {day.year}"


def month_grid_range(anchor: date) -> tuple[date, date]:
    """First and last day of the 42-cell month grid (weeks start Monday)."""
    first_of_month = anchor.replace(day=1)
    grid_start = first_of_month - timedelta(days=first_of_month.weekday())
    return grid_start, grid_start + timedelta(days=41)


def goto_calendar(page: Page, server_url: str) -> None:
    """Open the calendar and wait until the initial data has rendered."""
    page.goto(server_url)
    expect(page.locator("#calendar .month-view, #calendar .week-view")).to_be_visible()


def goto_month_containing(page: Page, server_url: str, target: date) -> None:
    """Open the calendar and page forward/backward to the target month."""
    goto_calendar(page, server_url)
    today = date.today()
    months_ahead = (target.year - today.year) * 12 + (target.month - today.month)
    button = page.locator("#btn-next" if months_ahead >= 0 else "#btn-prev")
    for _ in range(abs(months_ahead)):
        button.click()
    expect(page.locator("#period-title")).to_have_text(month_title(target))


def monday_of_week(day: date) -> date:
    return day - timedelta(days=day.weekday())


def goto_week_containing(page: Page, server_url: str, target: date) -> None:
    """Open the calendar in week view and page to the week of the target day."""
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    weeks_ahead = (monday_of_week(target) - monday_of_week(date.today())).days // 7
    button = page.locator("#btn-next" if weeks_ahead >= 0 else "#btn-prev")
    for _ in range(abs(weeks_ahead)):
        button.click()
    expect(
        page.locator(f'.week-day-column[data-date="{target.isoformat()}"]')
    ).to_be_attached()


def swipe_horizontally(page: Page, delta_x: int) -> None:
    """Dispatch a synthetic horizontal touch swipe on the calendar area."""
    page.evaluate(
        """(deltaX) => {
            const element = document.getElementById("calendar");
            const rect = element.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;
            const touchAt = (x, y) =>
                new Touch({ identifier: 1, target: element, clientX: x, clientY: y });
            element.dispatchEvent(new TouchEvent("touchstart", {
                touches: [touchAt(centerX, centerY)],
                changedTouches: [touchAt(centerX, centerY)],
                bubbles: true,
            }));
            element.dispatchEvent(new TouchEvent("touchend", {
                touches: [],
                changedTouches: [touchAt(centerX + deltaX, centerY)],
                bubbles: true,
            }));
        }""",
        delta_x,
    )
