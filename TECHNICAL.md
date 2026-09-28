# Client Deliverables Platform — technical spec

Solo-builder internal tool: collect client input, AI-draft a deliverable
(PPTX, XLSX, or animated HTML), edit it, publish a downloadable file or a
shareable link. Single internal workspace — no multi-tenancy, no client
logins, no RLS (the app always runs as `service_role`).

This file is the source of truth for the platform's invariants. Code
comments across the repo reference it by name (`grep -rn "TECHNICAL.md"`) —
keep it in sync with the code, not the other way around.

## Core invariants

These are load-bearing. Breaking any of them breaks something a test
already checks for (`tests/test_versioning.py` is the acceptance test for
the first two).

**Save-Version-vs-Publish rule.** `artifacts.current_version_id` points at
the version snapshot currently reflected by the live draft; any edit
(`blocks` or `raw_html`) clears it back to `null`. "Save Version"
(`POST /projects/{id}/save-version`) always inserts a *new*
`artifact_versions` row and repoints `current_version_id` at it — it never
overwrites an existing version. "Publish" (`POST /projects/{id}/publish`)
reuses `current_version_id` if one is already set (nothing changed since
the last save), or silently creates a version first if not, then points
`shares.published_version_id` at that version. Once published, further
edits keep mutating the live draft and clearing `current_version_id` as
normal, but `shares.published_version_id` never moves until Publish is
clicked again — editing after publish must never change what's live at the
share link.

**Status state machine** (`projects.status`, enum `draft` /
`in_review` / `published`):
- starts `draft`
- `draft` → `in_review` via "Mark as In Review", and only from `draft`
- any status → `published` via Publish, unconditionally
- `published` is sticky — Mark as In Review is a no-op once published, it
  never demotes back to `in_review`
- plain content edits never change status by themselves

**Artifact Contract.** The AI drafting prompt (`app/ai/draft.py`) is under
a hard instruction: never invent or fabricate an `image_block.image_ref`
value. It may only emit one if the input's `available_images` list
supplied that exact ref — i.e. an image the user actually uploaded to
Supabase Storage (`deliverable-assets` bucket). This applies to every
input path (form, file upload, shared-artifact-link import) since they all
funnel through the same `generate_draft()` call.

**Scope discipline (v1).** No audit/tracking tables, no per-view
time-series, no multi-tenant/RLS plumbing, no queue/worker infrastructure.
`shares.view_count` is a single counter, not a log. Simplifications made to
stay inside this scope are marked `ponytail:` in the code with the
upgrade path named inline — grep for that prefix before assuming a gap is
accidental.

## What's built

See README.md's "What's built" section for the phase-by-phase feature
list and file pointers (Phase 1 CRUD/auth, Phase 2 AI drafting, Phase 3
block editor, Phase 4 publish/PPTX, Phase 5 versioning acceptance test,
Phase 6 XLSX/animated-HTML renderers, Phase 7 file upload/rate-limit/
expiry). This file covers the *rules* those phases have to hold to, plus
the raw-HTML "exact clone" editor below, which is architecturally distinct
enough from the block editor to need its own explanation.

## Animated HTML: block editor vs. "exact clone, edit in place"

`animated_html` projects can be produced two different ways, and they are
edited completely differently:

1. **Drafted from a brief** (form/file/shared-artifact-link input →
   `generate_draft()` → `artifacts.blocks`) — same block list every
   deliverable type uses, rendered by `app/renderers/html.py`
   (GSAP/Chart.js via CDN). Edited in the same block editor as PPTX/XLSX.
2. **Uploaded as a finished HTML file** (`.html`/`.htm` upload, or a
   shared artifact URL for an `animated_html` project) — stored
   *verbatim* in `artifacts.raw_html`, never run through block extraction.
   This is the "exact clone, edit in place" mode: the platform treats the
   upload as a small web application with its own JS/CSS/animation/
   navigation runtime that must keep working, not a static document to
   re-template.

The rest of this section is about mode 2, `app/claude_artifact.py`, and
`/projects/{id}/raw-editor`.

### Architecture: iframe isolation, not DOM takeover

The editor page (`editor.html`) never touches the uploaded document's DOM
directly. It embeds it in a same-origin `<iframe>` at `/raw-editor`, and
the two communicate only through `postMessage`:

```
editor.html (parent)                    /raw-editor (iframe)
  toolbar buttons  --postMessage(cmd)-->  injected script handles it,
  formatState, slideState <--postMessage-- runs execCommand/DOM ops, replies
```

`inject_edit_script()` appends a single `<script data-editor-injected="true">`
tag just before `</body>` of the stored `raw_html` before serving it at
`/raw-editor` (never persisted — `serialize()` strips anything
`[data-editor-injected]` before saving, so re-injecting on every GET is
idempotent). Every editor-owned element carries that same marker, which is
the *only* thing `serialize()` is allowed to remove — see "Don't strip
aggressively" below for why guessing at what else is "chrome" is unsafe.

### Generic slide detection

`detectSlides()` doesn't assume any specific framework. It looks for
elements whose class name contains "slide" (case-insensitive — covers
`.slide`, `.deck-slide`, `.slide-panel`), grouped by shared parent so a
stray `.prev-slide-btn` doesn't get swept in. If nothing matches, it falls
back to whichever parent has the most children sharing an identical
tag+class signature, skipping inline/decorative tags (span, svg, button,
etc.) so those never get mistaken for slide containers.

### Formatting toolbar

Plain `document.execCommand()` (bold/italic/underline/fontName/foreColor/
hiliteColor/insertUnorderedList) — the native browser API for
contenteditable rich text, no library. `fontSize` only understands the
legacy 1–7 `<font size>` scale, so real px sizing wraps the selection in a
throwaway `size="7"` marker and swaps it for an inline `font-size` style.
The toolbar lives in the parent page but the actual commands execute
inside the iframe (a selection only means something in the document it was
made in); button `mousedown` handlers call `preventDefault()` so clicking
a toolbar button doesn't steal focus and collapse the iframe's selection
first. The toolbar also *reflects* the live selection (font/size/color/
bold/italic/underline) via a `selectionchange` listener in the iframe that
reports `getComputedStyle()`/`queryCommandState()` back to the parent —
this listener is registered before slide detection can early-return, since
formatting has to work on any contenteditable page, deck or not.

### Hazards specific to arbitrary uploaded JS (read before touching this code)

These were each found live, not anticipated in advance — the lesson each
one taught is the reason the code looks the way it does now:

- **Don't strip DOM elements a deck's own script still references.**
  `strip_hidden_slides()`/`strip_native_authoring_chrome()` used to delete
  elements outright. One deck's boot script indexed slides by position
  against its own bookkeeping array; deleting one desynced the two and
  threw `Cannot read properties of undefined`, halting the *entire*
  script — not just navigation, every animation on the page. Prefer
  CSS-hiding (`display:none!important`, persisted via a non-stripped
  `<style id="__persist_hidden__">`) over removal unless you've verified
  live (real browser, zero console errors) that a specific deck's script
  doesn't depend on the element's presence.
- **A deck's own render call can be non-idempotent.** "Exact clone, edit
  in place" persists the *already-rendered* DOM, script tag included. If
  that script builds its slides via `container.insertAdjacentHTML(
  'beforeend', ...)` (append, not replace), it duplicates everything on
  every subsequent load, because the container already holds the
  previous render. `make_slide_render_idempotent()` finds that exact
  pattern and wraps it in a `container.children.length===0 && ...` guard,
  applied once at upload time (`_save_raw_html`) so it's permanent.
  `dedupe_slides_if_needed()` is a save-time backstop for the same failure
  mode, and `detectSlides()`'s own `dedupeIfNeeded()` cleans it up live in
  the editor too — three independent layers because this bug was seen
  recurring from different angles before the root cause was found.
- **A deck can carry an embedded CSP.** Exported artifacts (from AI
  page-builder tools that generate standalone HTML) often ship
  `Content-Security-Policy` with `connect-src 'none'`, which silently
  blocks the autosave `fetch()` with no server-visible error.
  `_relax_csp_for_editing()` loosens just `connect-src` to `'self'`,
  leaving the rest of the policy intact.
- **Don't force `opacity`/`transform` on every element to "fix" a stuck
  animation.** An inline `!important` on those properties permanently
  overrides any CSS animation using them — including legitimate looping
  decorative ones (a pulsing glow), which is worse than the invisible
  content it was meant to fix. Only touch elements that are genuinely
  stuck (computed opacity `< 0.95` **and** no `infinite` animation
  iteration count) — see `revealCurrent()`.
- **Prefer driving a deck's own navigation over faking it.** Dispatching
  real `ArrowLeft`/`ArrowRight` `keydown` events (`stepNative()`) lets a
  deck's own reveal-on-enter animation play as it would for a real click,
  instead of us guessing at the end state. This was pulled once on
  suspicion of causing slide duplication, then restored once the actual
  cause (the non-idempotent render call above) was found and fixed
  independently — verified live with a 40-event rapid burst causing zero
  duplication against the fix. `tests/test_slide_duplication_guard.py`
  guards this specific regression permanently, in a real browser.

### Sidebar: client/project tree

`base.html`'s app-wide sidebar (every authenticated page) lists clients
with their projects nested underneath, via `sidebar_clients()` in
`app/db.py`. It's registered as a Jinja global per-router
(`templates.env.globals["sidebar_clients"] = lambda: sidebar_clients(get_supabase())`)
rather than called directly, specifically so tests can monkeypatch each
router's own `get_supabase` name and still have the sidebar query hit the
fake DB instead of the network. Inline rename/delete for both clients and
projects; delete cascades through the FK chain in
`migrations/001_init.sql` (client → projects → inputs/artifacts →
artifact_versions/shares) with no manual cleanup needed.
