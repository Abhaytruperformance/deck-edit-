"""Self-check for app/claude_artifact.py's URL validation and the
_build_raw_data() decision logic (auth-wall / non-200 / empty-page / success),
plus app/ai/draft.py's prompt framing for source_type="claude_artifact_url".

_build_raw_data() takes plain values (status, final_url, visible_text,
rendered_html) rather than a live page - it never touches Playwright or the
network, so none of this needs to mock a browser.

Run: python tests/test_claude_import.py
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")

from app.ai.draft import generate_draft  # noqa: E402
from app.claude_artifact import (  # noqa: E402
    ImportError_,
    _build_raw_data,
    _relax_csp_for_editing,
    dedupe_slides_if_needed,
    inject_edit_script,
    is_claude_artifact_url,
    make_slide_render_idempotent,
    parse_data_uri,
    parse_html_upload,
    strip_hidden_slides,
    strip_native_authoring_chrome,
    upload_images,
)

LONG_TEXT = "Q3 revenue grew 40% year over year. " * 10  # well over MIN_TEXT_CHARS
SHORT_TEXT = "Loading..."


def test_url_validation():
    assert is_claude_artifact_url("https://claude.ai/artifact/abc123")
    assert is_claude_artifact_url("https://claude.ai/code/artifact/xyz")
    assert not is_claude_artifact_url("https://example.com/artifact/abc")
    assert not is_claude_artifact_url("https://claude.ai/chat/abc123")
    assert not is_claude_artifact_url("not a url")


def _raises(fn, *args):
    try:
        fn(*args)
        return None
    except ImportError_ as e:
        return str(e)


def test_auth_wall_status_rejected():
    msg = _raises(_build_raw_data, "https://claude.ai/artifact/a", 403, "https://claude.ai/artifact/a", LONG_TEXT, "")
    assert msg and "publicly shared" in msg


def test_login_redirect_rejected():
    msg = _raises(_build_raw_data, "https://claude.ai/artifact/a", 200, "https://claude.ai/login", LONG_TEXT, "")
    assert msg and "publicly shared" in msg


def test_non_200_rejected():
    msg = _raises(_build_raw_data, "https://claude.ai/artifact/a", 500, "https://claude.ai/artifact/a", LONG_TEXT, "")
    assert msg and "HTTP 500" in msg


def test_near_empty_text_rejected():
    msg = _raises(_build_raw_data, "https://claude.ai/artifact/a", 200, "https://claude.ai/artifact/a", SHORT_TEXT, "")
    assert msg and "extract usable content" in msg


def test_success_captures_text_and_url():
    raw = _build_raw_data("https://claude.ai/artifact/a", 200, "https://claude.ai/artifact/a", LONG_TEXT, "<html></html>")
    assert raw["notes"] == LONG_TEXT.strip()
    assert raw["source_url"] == "https://claude.ai/artifact/a"
    assert "embedded_data" not in raw


def test_embedded_json_best_effort():
    html = '<script type="application/json">{"kpi": 40}</script>'
    raw = _build_raw_data("https://claude.ai/artifact/a", 200, "https://claude.ai/artifact/a", LONG_TEXT, html)
    assert raw["embedded_data"] == {"kpi": 40}


def test_parse_data_uri():
    assert parse_data_uri("data:image/png;base64,aGVsbG8=") == (b"hello", "image/png")
    assert parse_data_uri("https://example.com/logo.png") is None  # not ours to re-host
    assert parse_data_uri("data:image/svg+xml,<svg/>") is None  # not base64-encoded
    assert parse_data_uri("data:text/plain;base64,aGVsbG8=") is None  # not an image


class _FakeBucket:
    def __init__(self):
        self.uploaded: list[tuple[str, bytes]] = []

    def upload(self, path, data, options):
        self.uploaded.append((path, data))


class _FakeDB:
    def __init__(self):
        self.bucket = _FakeBucket()
        self.storage = SimpleNamespace(from_=lambda name: self.bucket)


def test_upload_images_dedupes_and_caps():
    db = _FakeDB()
    images = [{"data": b"logo", "content_type": "image/png", "alt": "Logo"}] * 20  # same bytes repeated
    result = upload_images(db, images)
    assert len(result) == 1  # deduped by content hash
    assert result[0]["image_ref"] == db.bucket.uploaded[0][0]
    assert result[0]["context"] == "Logo"

    db2 = _FakeDB()
    distinct = [{"data": bytes([i]), "content_type": "image/png", "alt": ""} for i in range(30)]
    result2 = upload_images(db2, distinct)
    assert len(result2) == 15  # capped at MAX_IMAGES


def test_upload_images_empty_list():
    assert upload_images(_FakeDB(), []) == []


def test_parse_html_upload_extracts_structure():
    padding = "Q3 revenue grew 40% year over year. " * 10
    html = f"""
    <html><body>
      <h1>Executive Summary</h1>
      <p>{padding}</p>
      <table><tr><th>Metric</th><th>Value</th></tr><tr><td>Revenue</td><td>$4.2M</td></tr></table>
      <img src="data:image/png;base64,aGVsbG8=" alt="Logo">
      <script>var x = 1;</script>
    </body></html>
    """
    raw_data, images = parse_html_upload(html.encode())
    assert "## Executive Summary" in raw_data["notes"]
    assert "Metric | Value" in raw_data["notes"]
    assert "Revenue | $4.2M" in raw_data["notes"]
    assert "var x = 1" not in raw_data["notes"]  # script content excluded
    assert images == [{"data": b"hello", "content_type": "image/png", "alt": "Logo"}]


def test_parse_html_upload_rejects_near_empty():
    try:
        parse_html_upload(b"<html><body><p>hi</p></body></html>")
        raise SystemExit("expected ValueError on near-empty HTML")
    except ValueError:
        pass


def test_inject_edit_script_before_closing_body():
    html = "<html><body><h1>Hi</h1></body></html>"
    injected = inject_edit_script(html)
    assert injected.count("</body>") == 1
    assert injected.index('data-editor-injected') < injected.index("</body>")
    assert injected.index("<h1>Hi</h1>") < injected.index('data-editor-injected')


def test_inject_edit_script_appends_when_no_body_tag():
    html = "<div>fragment</div>"
    injected = inject_edit_script(html)
    assert injected.startswith(html)
    assert "data-editor-injected" in injected


def test_inject_edit_script_indicator_is_self_stripping():
    """The floating Saving/Saved status badge must carry data-editor-injected
    like the rest of our injected UI - it was created without it, so every
    save serialize()'d it into the STORED html mid-"Saving..." state,
    permanently baking a stuck status badge into the published page."""
    injected = inject_edit_script("<html><body><h1>Hi</h1></body></html>")
    before_style = injected[: injected.index("indicator.style.cssText")]
    assert "__editor_indicator__" in before_style
    assert "data-editor-injected" in before_style


def test_inject_edit_script_includes_slide_controls():
    """The slide-level detect/goto/toggle logic is new, non-trivial JS baked
    into the injected string - assert its load-bearing pieces are actually
    present rather than silently dropped by a future edit. Move/Delete were
    deliberately removed (kept desyncing a deck's own bundled navigation
    script), so this also locks in that they stay gone.

    stepNative/KeyboardEvent (synthetic keydown dispatch, driving a deck's
    own reveal animation) were removed once, on suspicion of causing slide
    duplication, then restored after the real cause was found and fixed
    elsewhere (a deck's own non-idempotent insertAdjacentHTML re-running on
    reload - see make_slide_render_idempotent) and rapid dispatch was
    re-verified live to cause no duplication against the fix. They stay."""
    injected = inject_edit_script("<html><body><h1>Hi</h1></body></html>")
    for marker in (
        "data-editor-slide",
        "data-editor-hidden",
        "__persist_hidden__",
        "rawEditorHost",
        "labelFor",
        "'goto'", "'toggle'",
        "dirty", "revealCurrent", "data-editor-revealed", "dedupeIfNeeded", "getComputedStyle",
        "animationIterationCount", "stepNative", "KeyboardEvent",
        "'format'", "execCommand", "fontSizePx",
        "reportFormatState", "selectionchange", "queryCommandState", "rgbToHex",
    ):
        assert marker in injected, f"missing {marker!r} in injected slide-control script"
    for marker in ("'delete'", "'move'", "saveAndReload"):
        assert marker not in injected, f"{marker!r} should have been removed from the injected script"


def test_inject_edit_script_format_handler_works_without_slides():
    """The formatting toolbar (bold/italic/font/size/color) must work on any
    contenteditable raw_html page, not just multi-slide decks - so its
    message listener has to be registered before the `slides.length < 2`
    early-return, which exits the whole injected IIFE for single-page
    uploads. A previous version had it after that gate, silently disabling
    formatting for anything that isn't a detected slide deck."""
    injected = inject_edit_script("<html><body><h1>Hi</h1></body></html>")
    assert injected.index("cmd !== 'format'") < injected.index("slides.length < 2")


def test_relax_csp_for_editing_unblocks_connect_src():
    html = (
        '<html><head><meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; script-src \'unsafe-inline\'; connect-src \'none\'; '
        'object-src \'none\'"></head><body></body></html>'
    )
    relaxed = _relax_csp_for_editing(html)
    assert "connect-src 'self'" in relaxed
    assert "connect-src 'none'" not in relaxed
    assert "script-src 'unsafe-inline'" in relaxed  # rest of the policy untouched


def test_relax_csp_for_editing_adds_directive_if_absent():
    html = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
    relaxed = _relax_csp_for_editing(html)
    assert "connect-src 'self'" in relaxed


def test_relax_csp_for_editing_noop_without_csp_tag():
    html = "<html><head><meta charset='utf-8'></head><body>hi</body></html>"
    assert _relax_csp_for_editing(html) == html


def test_inject_edit_script_relaxes_embedded_csp():
    html = (
        '<html><head><meta http-equiv="Content-Security-Policy" '
        'content="connect-src \'none\'"></head><body></body></html>'
    )
    injected = inject_edit_script(html)
    assert "connect-src 'self'" in injected


def test_strip_hidden_slides_removes_marked_element():
    html = (
        '<body><section class="deck-slide">A</section>'
        '<section class="deck-slide" data-editor-hidden>B</section>'
        '<section class="deck-slide">C</section></body>'
    )
    stripped = strip_hidden_slides(html)
    assert "B" not in stripped
    assert "A" in stripped and "C" in stripped
    assert stripped.count("deck-slide") == 2


def test_strip_hidden_slides_keeps_nested_same_tag_intact():
    # A same-tag descendant inside the hidden slide must not close the span early.
    html = (
        '<section data-editor-hidden><section>inner</section>tail</section>'
        '<section class="keep">D</section>'
    )
    stripped = strip_hidden_slides(html)
    assert "inner" not in stripped
    assert "tail" not in stripped
    assert '<section class="keep">D</section>' in stripped


def test_strip_hidden_slides_noop_without_hidden_marker():
    html = "<body><section class=\"deck-slide\">A</section></body>"
    assert strip_hidden_slides(html) == html


def test_strip_native_authoring_chrome_hides_build_button_panel_and_hint():
    html = (
        '<head></head><body><section>real content</section>'
        '<div id="ch"><button id="pv">&larr;</button><div id="cnt">1 / 3</div>'
        '<button id="nx">&rarr;</button><div id="trk"><i></i><i></i><i></i></div>'
        '<button id="bb">Build</button></div>'
        '<div id="pn"><h4>Build the deck</h4><p>toggle modules</p></div>'
        '<div id="hint">&larr; &rarr; navigate &middot; B build</div>'
        '</body>'
    )
    stripped = strip_native_authoring_chrome(html)
    assert "real content" in stripped
    # the deck's real, client-facing navigation must survive untouched
    assert 'id="pv"' in stripped and 'id="nx"' in stripped and 'id="trk"' in stripped
    assert 'id="ch"' in stripped
    # the authoring-only elements stay in the DOM (a deck's own script may
    # still reference them, e.g. populating a module list inside #pn
    # unconditionally) - only hidden via CSS, never removed
    assert 'id="bb"' in stripped
    assert 'id="pn"' in stripped
    assert 'id="hint"' in stripped
    assert "Build the deck" in stripped
    assert "#bb,#pn,#hint{display:none!important}" in stripped


def test_strip_native_authoring_chrome_leaves_unrelated_ids_alone():
    # id="bb"/"pn"/"hint" are generic enough that some other deck could use
    # them for something unrelated - only remove when the matching signal
    # text is actually present, never on the id alone.
    html = '<body><div id="bb">bulletin board</div><div id="hint">a friendly hint</div></body>'
    assert strip_native_authoring_chrome(html) == html


def test_dedupe_slides_if_needed_collapses_exact_repeat():
    html = (
        '<div id="stage">'
        '<div data-editor-slide="0">A</div><div data-editor-slide="1">B</div>'
        '<div data-editor-slide="2">A</div><div data-editor-slide="3">B</div>'
        '</div>'
    )
    deduped = dedupe_slides_if_needed(html)
    assert deduped.count("data-editor-slide=") == 2
    assert deduped.count(">A<") == 1 and deduped.count(">B<") == 1


def test_dedupe_slides_if_needed_leaves_genuinely_distinct_slides_alone():
    html = (
        '<div data-editor-slide="0">A</div><div data-editor-slide="1">B</div>'
        '<div data-editor-slide="2">C</div><div data-editor-slide="3">D</div>'
    )
    assert dedupe_slides_if_needed(html) == html


def test_dedupe_slides_if_needed_tolerates_one_edited_duplicate():
    # A majority match (not unanimous) still counts as the same duplication
    # bug - seen live, one slide had already been edited through one of the
    # two copies before the duplicate was noticed, which would otherwise
    # let that one mismatched pair block cleanup of the other 9.
    first_half = "".join(f'<div data-editor-slide="{i}">S{i}</div>' for i in range(10))
    second_half_parts = [f'<div data-editor-slide="{i + 10}">S{i}</div>' for i in range(10)]
    second_half_parts[3] = '<div data-editor-slide="13">EDITED</div>'
    html = first_half + "".join(second_half_parts)
    deduped = dedupe_slides_if_needed(html)
    assert deduped.count("data-editor-slide=") == 10
    assert "EDITED" not in deduped
    assert ">S3<" in deduped


def test_dedupe_slides_if_needed_noop_on_odd_count():
    html = '<div data-editor-slide="0">A</div><div data-editor-slide="1">B</div><div data-editor-slide="2">A</div>'
    assert dedupe_slides_if_needed(html) == html


def test_make_slide_render_idempotent_guards_the_real_pattern():
    # Matches the exact pattern found live in a real deck's boot script -
    # scaler.insertAdjacentHTML('beforeend', S.map(s=>s.html).join(''));
    js = "const scaler=document.getElementById('scaler');\nscaler.insertAdjacentHTML('beforeend',S.map(s=>s.html).join(''));\nconst nodes=Array.from(scaler.querySelectorAll('.slide'));"
    guarded = make_slide_render_idempotent(js)
    # A marker attribute, not children.length===0 - a deck can ship a static
    # placeholder already inside the container (seen live: a #prog node),
    # which makes children.length nonzero even on the very first, never-
    # rendered load and would permanently block the real render from firing.
    assert "!(scaler).hasAttribute('data-editor-rendered')" in guarded
    assert "(scaler).setAttribute('data-editor-rendered','1')" in guarded
    assert "scaler.insertAdjacentHTML('beforeend',S.map(s=>s.html).join(''))" in guarded
    # the call itself, and everything around it, must survive verbatim
    assert "const nodes=Array.from(scaler.querySelectorAll('.slide'));" in guarded
    assert "const scaler=document.getElementById('scaler');" in guarded


def test_make_slide_render_idempotent_noop_without_the_pattern():
    js = "console.log('hello'); document.body.innerHTML = '<p>hi</p>';"
    assert make_slide_render_idempotent(js) == js


def test_make_slide_render_idempotent_handles_multiple_calls():
    js = "a.insertAdjacentHTML('beforeend', x()); b.insertAdjacentHTML('afterbegin', y());"
    guarded = make_slide_render_idempotent(js)
    assert "!(a).hasAttribute('data-editor-rendered')" in guarded
    assert "!(b).hasAttribute('data-editor-rendered')" in guarded
    assert guarded.count("insertAdjacentHTML") == 2


def _fake_response(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_draft_prompt_frames_artifact_import_differently():
    good_blocks = '[{"id": "s1", "type": "text_block", "enabled": true, "content": {"heading": "H", "body": "B"}}]'
    with patch("app.ai.draft.OpenAI") as MockOpenAI:
        create = MockOpenAI.return_value.chat.completions.create
        create.return_value = _fake_response(good_blocks)
        generate_draft({"notes": "some text"}, source_type="claude_artifact_url")
        prompt = create.call_args.kwargs["messages"][1]["content"]
        assert "Existing client material" in prompt

        create.reset_mock()
        create.return_value = _fake_response(good_blocks)
        generate_draft({"business_name": "Acme"}, source_type="form")
        prompt = create.call_args.kwargs["messages"][1]["content"]
        assert "Client input (JSON)" in prompt


if __name__ == "__main__":
    test_url_validation()
    test_auth_wall_status_rejected()
    test_login_redirect_rejected()
    test_non_200_rejected()
    test_near_empty_text_rejected()
    test_success_captures_text_and_url()
    test_embedded_json_best_effort()
    test_parse_data_uri()
    test_upload_images_dedupes_and_caps()
    test_upload_images_empty_list()
    test_parse_html_upload_extracts_structure()
    test_parse_html_upload_rejects_near_empty()
    test_inject_edit_script_before_closing_body()
    test_inject_edit_script_appends_when_no_body_tag()
    test_inject_edit_script_indicator_is_self_stripping()
    test_inject_edit_script_includes_slide_controls()
    test_inject_edit_script_format_handler_works_without_slides()
    test_relax_csp_for_editing_unblocks_connect_src()
    test_relax_csp_for_editing_adds_directive_if_absent()
    test_relax_csp_for_editing_noop_without_csp_tag()
    test_inject_edit_script_relaxes_embedded_csp()
    test_strip_hidden_slides_removes_marked_element()
    test_strip_hidden_slides_keeps_nested_same_tag_intact()
    test_strip_hidden_slides_noop_without_hidden_marker()
    test_strip_native_authoring_chrome_hides_build_button_panel_and_hint()
    test_strip_native_authoring_chrome_leaves_unrelated_ids_alone()
    test_dedupe_slides_if_needed_collapses_exact_repeat()
    test_dedupe_slides_if_needed_leaves_genuinely_distinct_slides_alone()
    test_dedupe_slides_if_needed_tolerates_one_edited_duplicate()
    test_dedupe_slides_if_needed_noop_on_odd_count()
    test_make_slide_render_idempotent_guards_the_real_pattern()
    test_make_slide_render_idempotent_noop_without_the_pattern()
    test_make_slide_render_idempotent_handles_multiple_calls()
    test_draft_prompt_frames_artifact_import_differently()
    print("OK: claude artifact URL validation, fetch decision logic, and source-type prompt framing all hold")
