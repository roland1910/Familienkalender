"""Tests for turning Google's HTML descriptions into readable plain text.

Live finding after the Etappe-45 deploy: the mirrored copies in Roland's
Nextcloud carried Google's raw HTML ("<div id=\"loom-description\"><b>This
meeting will be recorded by Loom.</b><br>Set up <a href=\"...\">Loom</a>"),
which every calendar client shows as source code. The conversion has to keep
the two things that matter — the line structure and the meeting LINKS — while
never damaging a description that is plain text to begin with.
"""

from app.sources.html_text import MAX_HTML_LENGTH, html_to_text

LOOM = (
    '<div id="loom-description">\n'
    "<b>This meeting will be recorded by Loom.</b><br>"
    'Set up <a href="https://www.loom.com/notes?workspace=1&amp;x=2">Loom</a>'
    " for your team.\n</div>"
)


class TestEmptyInput:
    def test_none_stays_none(self) -> None:
        assert html_to_text(None) is None

    def test_empty_stays_empty(self) -> None:
        assert html_to_text("") == ""

    def test_whitespace_only_becomes_empty(self) -> None:
        assert html_to_text("   \n\t ") == ""

    def test_markup_without_text_becomes_empty(self) -> None:
        assert html_to_text("<div><br></div>") == ""


class TestPlainTextIsLeftAlone:
    """A description that is not HTML must survive untouched."""

    def test_plain_lines_keep_their_breaks(self) -> None:
        text = "Agenda:\n- Rückblick\n- Planung"
        assert html_to_text(text) == text

    def test_a_less_than_sign_in_prose_is_not_swallowed(self) -> None:
        assert html_to_text("Wenn a < b, dann 3<4 rechnen") == (
            "Wenn a < b, dann 3<4 rechnen"
        )

    def test_indentation_and_blank_lines_survive(self) -> None:
        text = "Ablauf\n\n    1. Begrüßung\n    2. Bericht"
        assert html_to_text(text) == text

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert html_to_text("\n  Kurzinfo  \n\n") == "Kurzinfo"

    def test_entities_are_decoded_in_plain_text_too(self) -> None:
        assert html_to_text("Meier &amp; Sohn") == "Meier & Sohn"


class TestTagsBecomeLineStructure:
    def test_br_becomes_a_line_break(self) -> None:
        assert html_to_text("Zeile 1<br>Zeile 2<br/>Zeile 3") == (
            "Zeile 1\nZeile 2\nZeile 3"
        )

    def test_paragraphs_and_divs_separate_lines(self) -> None:
        assert html_to_text("<p>Erstens</p><p>Zweitens</p>") == "Erstens\n\nZweitens"
        assert html_to_text("<div>Oben</div><div>Unten</div>") == "Oben\nUnten"

    def test_list_items_become_bullets(self) -> None:
        html = "<ul><li>Rückblick</li><li>Planung</li></ul>"
        assert html_to_text(html) == "- Rückblick\n- Planung"

    def test_inline_tags_are_removed_without_a_break(self) -> None:
        assert html_to_text("<b>Wichtig</b>: <i>heute</i> um <span>9</span>") == (
            "Wichtig: heute um 9"
        )

    def test_no_raw_tag_survives(self) -> None:
        text = html_to_text(LOOM)
        assert "<" not in text and ">" not in text

    def test_excess_blank_lines_are_collapsed(self) -> None:
        html = "Oben<br><br><br><br>Unten"
        assert html_to_text(html) == "Oben\n\nUnten"

    def test_source_indentation_does_not_leak_into_the_text(self) -> None:
        html = "<div>\n    <b>Titel</b>\n    <br>\n    Text\n</div>"
        assert html_to_text(html) == "Titel\nText"


class TestEntities:
    def test_named_and_numeric_entities_are_decoded(self) -> None:
        html = "<p>a &lt; b &amp; c &gt; d &quot;e&quot; &#8364; &#x27;f&#x27;</p>"
        assert html_to_text(html) == "a < b & c > d \"e\" € 'f'"

    def test_nbsp_becomes_an_ordinary_space(self) -> None:
        text = html_to_text("<p>Raum&nbsp;2</p>")
        assert text == "Raum 2"
        assert "\xa0" not in text


class TestLinks:
    """Meeting links are the most important content of such a description."""

    def test_link_keeps_text_and_url(self) -> None:
        html = '<a href="https://meet.example.com/abc">Beitreten</a>'
        assert html_to_text(html) == "Beitreten (https://meet.example.com/abc)"

    def test_link_whose_text_is_the_url_appears_once(self) -> None:
        url = "https://meet.example.com/abc"
        assert html_to_text(f'<a href="{url}">{url}</a>') == url

    def test_link_without_text_shows_the_url(self) -> None:
        html = '<a href="https://meet.example.com/abc"></a>'
        assert html_to_text(html) == "https://meet.example.com/abc"

    def test_link_without_href_keeps_its_text(self) -> None:
        assert html_to_text("<a>Nur Text</a>") == "Nur Text"

    def test_mailto_link_with_the_address_as_text_appears_once(self) -> None:
        html = '<a href="mailto:chef@example.com">chef@example.com</a>'
        assert html_to_text(html) == "chef@example.com"

    def test_url_query_entities_are_decoded_in_the_link(self) -> None:
        assert "?workspace=1&x=2" in html_to_text(LOOM)

    def test_unclosed_link_is_still_emitted(self) -> None:
        html = '<p>Start <a href="https://example.com/x">Hier'
        assert html_to_text(html) == "Start Hier (https://example.com/x)"


class TestRealGoogleDescription:
    def test_loom_block_reads_like_a_note(self) -> None:
        assert html_to_text(LOOM) == (
            "This meeting will be recorded by Loom.\n"
            "Set up Loom (https://www.loom.com/notes?workspace=1&x=2)"
            " for your team."
        )


class TestRobustness:
    """Foreign, possibly broken markup must never raise."""

    def test_unclosed_and_malformed_tags_do_not_raise(self) -> None:
        for html in (
            "<div <weird>>Text</div",
            "<p>Absatz",
            "<<<>>>Text",
            '<a href="x" <b>Text</a>',
            "<script>evil()</script>Text",
            "Text <!-- Kommentar --> weiter",
        ):
            assert isinstance(html_to_text(html), str)

    def test_script_and_style_content_is_dropped(self) -> None:
        html = "<style>.a{color:red}</style><p>Sichtbar</p><script>x=1</script>"
        assert html_to_text(html) == "Sichtbar"

    def test_comments_are_dropped(self) -> None:
        assert html_to_text("<p>Text <!-- intern --> weiter</p>") == "Text weiter"

    def test_absurdly_long_input_is_bounded(self) -> None:
        html = "<p>" + ("x" * (MAX_HTML_LENGTH * 3)) + "</p>"
        assert len(html_to_text(html)) <= MAX_HTML_LENGTH
