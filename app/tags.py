"""Day-tags API (/api/tags): symbols family members pin to single days.

Purely local data — tags are never synced to any external calendar.
Reachability: like every other route, these endpoints sit behind HA
ingress plus the client-IP allowlist middleware.

Validation lives in the storage layer (single writer of the day_tags
table); this module translates its ValueError into German HTTP errors.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.models import MAX_TAGS_PER_DAY, TAG_OPTIONS
from app.storage import get_storage

router = APIRouter(prefix="/api/tags")


class DayTagsUpdate(BaseModel):
    emojis: list[str]


@router.get("/options")
async def list_tag_options() -> dict:
    """The fixed symbol catalog for the frontend picker, plus the per-day cap."""
    return {
        "options": [{"id": option.id, "emoji": option.emoji} for option in TAG_OPTIONS],
        "max_per_day": MAX_TAGS_PER_DAY,
    }


@router.get("")
async def list_tags(
    from_date: Annotated[date, Query(alias="from")],
    to_date: Annotated[date, Query(alias="to")],
) -> dict:
    """Tags per ISO date for [from, to] (inclusive); only days with tags."""
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="'from' muss vor 'to' liegen")
    return {"tags": get_storage().get_day_tags(from_date, to_date)}


@router.put("/{day}")
async def set_tags(day: date, update: DayTagsUpdate) -> dict:
    """Replace the tags of one day (an empty list clears it)."""
    deduped = list(dict.fromkeys(update.emojis))
    if len(deduped) > MAX_TAGS_PER_DAY:
        raise HTTPException(
            status_code=400, detail=f"Höchstens {MAX_TAGS_PER_DAY} Symbole pro Tag."
        )
    try:
        stored = get_storage().set_day_tags(day, deduped)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Unbekanntes Symbol.") from error
    return {"date": day.isoformat(), "emojis": stored}
