"""Shared data-loss guard for the add-on's outgoing (writing) syncs.

The mirror sync and the birthday sync both DELETE in Roland's real
calendars, driven by what the calendar sources delivered in the run that
just finished. So a run that would delete must be able to trust that
delivery. Same lesson as the slideshow photo index (CLAUDE.md, Etappe 31):
**availability/freshness, not emptiness, is the criterion.**

Keeping the rule in one place means the two syncs can never drift apart on
the question "may this run delete anything at all?" — and a third write
direction inherits it for free.

The reasons are returned as CODES, not German sentences: the admin UI turns
them into text (same split as slideshow_scan_status / scanWarningText).
"""

import logging

logger = logging.getLogger(__name__)

SKIP_SOURCE_ERROR = "source_error"
SKIP_EMPTY_RESULT = "empty_result"
SKIP_NO_SOURCES = "no_sources"


def sources_verified_fresh(
    source_ids: set[int], source_results: dict[int, str | None] | None
) -> bool:
    """Whether EVERY selected source was read without error in THIS run.

    Only then is an empty desired set trustworthy. ``source_results`` is what
    app.sync reports for the run that just finished; ``None`` means "no
    information" (a manual/standalone call), which counts as not verified.
    A selected source missing from the mapping — disabled, deleted, never
    fetched — is not verified either, on purpose.
    """
    if source_results is None or not source_ids:
        return False
    return all(
        sid in source_results and source_results[sid] is None for sid in source_ids
    )


def skip_reason(
    source_ids: set[int],
    source_results: dict[int, str | None] | None,
    *,
    desired_count: int,
    mapped_count: int,
    remote_count: int = 0,
    label: str = "Outgoing sync",
) -> str | None:
    """Why this run must not reconcile, or None when it may proceed.

    Four cases, deliberately:

    (a) A selected source reported an error in this run → skip. Its events in
        the DB are whatever an earlier run left behind and may already have
        been thinned out; nothing is deleted on that basis.
    (b) No source selected at all while objects are still mapped → skip. A
        missing or unparseable source-id setting reads as the empty list and
        is indistinguishable from "deliberately deselected", so it must never
        mean "delete everything". Switching the sync OFF is the deliberate way
        to stop; the admin UI therefore switches it off when the last source
        is deselected.
    (c) Nothing desired while objects exist (mapped, or discovered in the
        target calendar) and the sources are not verified fresh → skip. This
        is the "successful but empty" case: one unlucky fetch would otherwise
        wipe every written entry out of Roland's real calendar.
    (d) Everything else → run. Notably the legitimate cleanups still work: all
        appointments really gone (or the source deselected) plus an error-free
        sync of the selected sources means the empty set is the truth, and a
        fresh install with neither mapping nor remote objects has nothing to
        lose.

    ``remote_count`` is 0 for the pre-network check (nothing listed yet) and
    the real listing size afterwards, so a LOST mapping is protected too.
    """
    # A missing entry reads as None here, i.e. "did not fail" — the absent
    # case is handled by the verified-fresh test further down instead.
    failed = [sid for sid in source_ids if (source_results or {}).get(sid) is not None]
    if failed:
        logger.warning(
            "%s skipped: %d selected source(s) failed this run — existing "
            "entries are left untouched rather than deleted on stale data.",
            label,
            len(failed),
        )
        return SKIP_SOURCE_ERROR
    if not source_ids and mapped_count:
        logger.warning(
            "%s skipped: no source selected while %d entries are mapped — "
            "switch the sync off to stop it deliberately.",
            label,
            mapped_count,
        )
        return SKIP_NO_SOURCES
    if desired_count or sources_verified_fresh(source_ids, source_results):
        return None
    if mapped_count or remote_count:
        logger.warning(
            "%s skipped: nothing to write while %d entries exist and the "
            "sources were not verified in this run.",
            label,
            mapped_count or remote_count,
        )
        return SKIP_EMPTY_RESULT
    return None
