"""Turn a Google Calendar description into readable plain text.

Google Calendar delivers ``description`` as HTML more often than not (a
meeting body pasted from a browser, the note a tool like Loom or Zoom adds).
Copied verbatim into a CalDAV resource, that arrives in Nextcloud — and in
every other calendar client — as source code:

    <div id="loom-description">
    <b>This meeting will be recorded by Loom.</b><br>Set up <a href="...

That was the live finding after the Etappe-45 deploy. This module holds the
one pure function that fixes it. It is deliberately small and free of any
dependency: ``html.parser`` and ``html.unescape`` are standard library, and
the requirements file is hash-pinned — no new package for this.

Rules, in the order they matter:

- Line structure is preserved. Block boundaries (``</p>``, ``</div>``,
  ``<br>``, ``</li>`` …) become line breaks; consecutive boundaries collapse
  to at most one blank line, because Google's HTML produces plenty of them.
- LINKS survive. ``<a href="URL">Text</a>`` becomes ``Text (URL)`` — the
  meeting link is usually the single most important thing in such a
  description, so it must never be dropped with the tag. When the text IS the
  URL (or the mail address of a ``mailto:`` link), it appears once.
- Everything else is unwrapped, and HTML entities are decoded (``&amp;`` →
  ``&``, ``&nbsp;`` → an ordinary space, numeric ones as well).
- A description that is NOT HTML is left exactly as it is — same lines, same
  indentation. Prose like "a < b" must not be swallowed, so the parser only
  runs when the text actually contains something tag-shaped (a ``<name …>``
  closed by ``>``); otherwise only entities are decoded. The rare prose that
  looks like a tag ("Angebot <b 500 EUR>") is the accepted cost of cleaning up
  the HTML that Google really sends.
- Nothing raises. Foreign, half-broken markup falls back to a plain tag strip.

The LENGTH CAP is applied elsewhere and deliberately AFTERWARDS: the sync
clamps the event's text fields (``app.sources.limits.clamp_event_text`` in
``app.sync``) once this conversion has already run, so the 4000 characters
hold readable text instead of markup — markup would spend most of the budget
on tags. Only the parser input is bounded here, see MAX_HTML_LENGTH.
"""

import html
import logging
import re
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# Upper bound on the markup handed to the parser. Ten times the 4000-character
# cap the converted text is clamped to later: even the most tag-heavy HTML
# carries far more than 4000 characters of readable text in 40000 characters of
# markup, so nothing of substance is lost — while a pathological description
# cannot make the parser chew through megabytes.
MAX_HTML_LENGTH = 40_000

# What makes a string "HTML enough" to parse: a tag name behind "<", closed by
# a ">" with no further "<" in between. Requiring the closing bracket keeps
# ordinary prose ("wenn a < b", "3<4") on the plain-text path.
_TAG_LIKE = re.compile(r"<\s*/?[A-Za-z][^<>]{0,300}>")

# Runs of any whitespace inside a text node — HTML collapses them to one space.
_WHITESPACE = re.compile(r"\s+")
_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# Tags that end a line. "p" and friends open a paragraph, i.e. a blank line;
# the rest simply break.
_PARAGRAPH_TAGS = frozenset(
    {"p", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "pre"}
)
_LINE_TAGS = frozenset(
    {"div", "li", "tr", "br", "hr", "dt", "dd", "section", "article", "header", "footer"}
)

# Content of these is markup machinery, never text the reader wants.
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})

# HTML void elements never carry an end tag; without this the skip counter
# would never come back down for a stray "<br/>".
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "source"})


def _breaks_for(tag: str) -> int:
    if tag in _PARAGRAPH_TAGS:
        return 2
    if tag in _LINE_TAGS:
        return 1
    return 0


class _TextExtractor(HTMLParser):
    """Collects the readable text of an HTML fragment.

    Line breaks are requested rather than written: ``_pending`` remembers how
    many are owed and is flushed before the next piece of text. A block
    boundary only RAISES the count (so ``</div><div>`` yields one break, not
    two), while every ``<br>`` ADDS one — two of them really are a blank line.
    The count is capped at two on flush, which is what keeps Google's
    ``<br><br><br><br>`` from becoming four empty lines.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._pending = 0
        self._skip_depth = 0
        self._href: str | None = None
        self._link_parts: list[str] = []

    # -- output ---------------------------------------------------------
    def _emit(self, text: str) -> None:
        if not text:
            return
        if self._parts:
            self._parts.append("\n" * min(self._pending, 2))
        self._pending = 0
        self._parts.append(text)

    def _request_break(self, count: int, *, additive: bool) -> None:
        if not count:
            return
        self._pending = self._pending + count if additive else max(self._pending, count)

    # -- link handling --------------------------------------------------
    def _close_link(self) -> None:
        href, self._href = self._href, None
        text = _WHITESPACE.sub(" ", "".join(self._link_parts)).strip()
        self._link_parts = []
        if href is None:
            self._emit(text)
            return
        href = href.strip()
        if not href:
            self._emit(text)
        elif not text:
            self._emit(href)
        elif text == href or f"mailto:{text}" == href:
            # The text already IS the address — repeating it helps nobody.
            self._emit(text)
        else:
            self._emit(f"{text} ({href})")

    # -- parser callbacks -----------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "a":
            if self._href is not None:  # nested/unclosed link: flush the old one
                self._close_link()
            self._href = next((value for name, value in attrs if name == "href"), None)
            return
        if tag == "li":
            self._request_break(1, additive=False)
            self._emit("- ")
            return
        self._request_break(_breaks_for(tag), additive=tag == "br")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a":
            if self._href is not None or self._link_parts:
                self._close_link()
            return
        self._request_break(_breaks_for(tag), additive=False)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._href is not None:
            self._link_parts.append(data)
            return
        collapsed = _WHITESPACE.sub(" ", data)
        if not collapsed.strip():
            # Whitespace between tags is only ever an inline separator; it must
            # not cancel a pending line break (source indentation would then
            # turn every block boundary into a blank line).
            if self._pending == 0 and self._parts:
                self._parts.append(" ")
            return
        self._emit(collapsed)

    def result(self) -> str:
        if self._href is not None or self._link_parts:
            self._close_link()  # unclosed <a>: keep the link rather than lose it
        return "".join(self._parts)


def _tidy(text: str) -> str:
    """Trim every line, squeeze spaces and cap runs of blank lines."""
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _strip_tags(markup: str) -> str:
    """Fallback for markup the parser choked on: drop tags, decode entities."""
    return _tidy(html.unescape(re.sub(r"<[^<>]*>", " ", markup)).replace("\xa0", " "))


def html_to_text(value: str | None) -> str | None:
    """Readable plain text for a (possibly HTML) calendar description.

    ``None`` stays ``None`` and an empty/blank string ends up empty, so the
    caller keeps deciding what "no description" means.
    """
    if not value:
        return value
    markup = value[:MAX_HTML_LENGTH]
    if not _TAG_LIKE.search(markup):
        # Plain text: decode entities, leave lines and indentation alone.
        return html.unescape(markup).replace("\xa0", " ").strip()
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
        text = parser.result()
    except Exception as exc:
        # Foreign markup must never abort a sync. Only the exception TYPE is
        # logged: parser messages quote the (untrusted) description.
        logger.warning(
            "HTML description fell back to tag stripping (%s)", type(exc).__name__
        )
        return _strip_tags(markup)
    return _tidy(text.replace("\xa0", " "))
