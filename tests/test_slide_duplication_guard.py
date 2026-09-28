"""Regression test for the "exact clone, edit in place" slide-duplication
bug: a deck's own boot script appends its rendered slides into a container
via `container.insertAdjacentHTML('beforeend', ...)`. Since we persist the
ALREADY-rendered DOM (script tag included), reloading a saved page re-runs
that same append against a container that already holds the previous
render - duplicating every slide, then crashing when the script's own
per-slide bookkeeping array (sized for one render) gets indexed against DOM
nodes from both ("Cannot read properties of undefined (reading 'mod')",
seen live).

make_slide_render_idempotent() (app/claude_artifact.py) guards against this
permanently, applied once at upload time, to genuinely never-rendered
content. This proves it in a real browser (Playwright), not just at the
string level (see the make_slide_render_* tests in
tests/test_claude_import.py):

1. The unguarded fixture really does duplicate and crash on a simulated
   reload (so this test isn't vacuously passing).
2. The real lifecycle: guard applied once to fresh content -> first render
   -> the resulting DOM gets saved (exactly what our own raw-editor
   autosave does, see save_raw_editor in app/routers/editor.py) -> reloading
   that saved page must not duplicate.
3. A static placeholder already inside the container on a fresh, never-
   rendered upload (seen live) must not block that first render either.

Needs `playwright install chromium` (see README) - already a project
dependency for the Claude-artifact importer.

Run: python tests/test_slide_duplication_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")

from playwright.sync_api import sync_playwright  # noqa: E402

from app.claude_artifact import make_slide_render_idempotent  # noqa: E402

# A container that already holds one previous render's worth of slides
# (exactly what a saved, already-edited page looks like) plus the deck's
# own non-idempotent boot script, which appends a fresh render on top and
# indexes the result against its own per-render bookkeeping array.
_FIXTURE = """<!doctype html>
<html><head></head><body>
<div id="scaler">
  <div class="slide" data-mod="a" data-editor-slide="0">A</div>
  <div class="slide" data-mod="b" data-editor-slide="1">B</div>
</div>
<script>
const S = [
  {mod: 'a', html: '<div class="slide" data-mod="a">A</div>'},
  {mod: 'b', html: '<div class="slide" data-mod="b">B</div>'},
];
const scaler = document.getElementById('scaler');
scaler.insertAdjacentHTML('beforeend', S.map(s => s.html).join(''));
const nodes = Array.from(scaler.querySelectorAll('.slide'));
nodes.forEach((n, i) => n.dataset.mod = S[i].mod);
</script>
</body></html>
"""


# A deck that ships a static placeholder already inside the slide
# container - seen live, a `<div id="prog">` progress indicator - on a
# completely fresh, never-rendered upload. children.length is nonzero from
# the very first load here, which a children.length===0 guard (an earlier,
# buggier version of make_slide_render_idempotent) would misread as "already
# rendered" and permanently skip the real render, leaving the deck blank on
# every load including the first.
_NONEMPTY_CONTAINER_FIXTURE = """<!doctype html>
<html><head></head><body>
<div id="scaler"><div id="prog"></div></div>
<script>
const S = [
  {mod: 'a', html: '<div class="slide" data-mod="a">A</div>'},
  {mod: 'b', html: '<div class="slide" data-mod="b">B</div>'},
];
const scaler = document.getElementById('scaler');
scaler.insertAdjacentHTML('beforeend', S.map(s => s.html).join(''));
</script>
</body></html>
"""


# The same deck, but genuinely fresh - empty container, nothing rendered
# yet. This is the real state make_slide_render_idempotent() actually runs
# against (upload time, never-before-rendered), unlike _FIXTURE above which
# simulates an already-corrupted reload purely to sanity-check the danger.
_FRESH_FIXTURE = """<!doctype html>
<html><head></head><body>
<div id="scaler"></div>
<script>
const S = [
  {mod: 'a', html: '<div class="slide" data-mod="a">A</div>'},
  {mod: 'b', html: '<div class="slide" data-mod="b">B</div>'},
];
const scaler = document.getElementById('scaler');
scaler.insertAdjacentHTML('beforeend', S.map(s => s.html).join(''));
const nodes = Array.from(scaler.querySelectorAll('.slide'));
nodes.forEach((n, i) => n.dataset.mod = S[i].mod);
</script>
</body></html>
"""


def _load_and_check(browser, html: str):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.set_content(html, wait_until="load")
    page.wait_for_timeout(200)
    count = page.evaluate("document.getElementById('scaler').querySelectorAll('.slide').length")
    page.close()
    return count, errors


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()

        # Prove the fixture is faithful: unguarded, it really does
        # duplicate (2 pre-existing + 2 freshly appended = 4) and crashes
        # (nodes.length(4) > S.length(2), so nodes[2]/[3] index past S).
        count, errors = _load_and_check(browser, _FIXTURE)
        assert count == 4, f"fixture doesn't reproduce the bug as expected: got {count} slides"
        assert any("undefined" in e for e in errors), f"fixture should crash like the real bug: {errors}"

        # The actual fix, against the real (fresh, never-rendered) state:
        # apply once at upload time, then a genuine render -> save -> reload
        # cycle must not duplicate.
        guarded = make_slide_render_idempotent(_FRESH_FIXTURE)
        count2, errors2 = _load_and_check(browser, guarded)
        assert count2 == 2, f"first render should produce 2 slides, got {count2}"
        assert errors2 == [], f"first render should not crash: {errors2}"

        page = browser.new_page()
        save_errors = []
        page.on("pageerror", lambda exc: save_errors.append(str(exc)))
        page.set_content(guarded, wait_until="load")
        page.wait_for_timeout(200)
        saved_html = page.evaluate("document.documentElement.outerHTML")
        page.close()
        assert save_errors == [], f"the page our own autosave would capture must not have errored: {save_errors}"

        count3, errors3 = _load_and_check(browser, saved_html)
        assert count3 == 2, f"reloading the saved (post-render) page must not duplicate, got {count3}"
        assert errors3 == [], f"reload after save should not crash: {errors3}"

        # Re-applying the guard to already-guarded (but not yet rendered)
        # text must be a no-op, not a second wrapper layer - a nested guard
        # would actually block the real render (the outer layer's
        # setAttribute() runs before the inner layer's own hasAttribute()
        # check, which then always finds the marker already set).
        twice_guarded = make_slide_render_idempotent(guarded)
        assert twice_guarded == guarded, "re-applying the guard to already-guarded text must be a no-op"

        # The other failure mode this guard must not reintroduce: a static
        # placeholder already inside the container must not block the very
        # first, never-rendered render from firing at all.
        guarded_nonempty = make_slide_render_idempotent(_NONEMPTY_CONTAINER_FIXTURE)
        count4, errors4 = _load_and_check(browser, guarded_nonempty)
        assert count4 == 2, f"a pre-existing static child must not block the first render, got {count4}"
        assert errors4 == [], f"first render should not crash: {errors4}"

        browser.close()

    print("OK: make_slide_render_idempotent prevents the real slide-duplication/crash bug, verified in a real browser")


if __name__ == "__main__":
    main()
