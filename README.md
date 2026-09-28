# Client Deliverables Platform

Solo-builder v1: collect client input, AI-draft a deliverable (PPTX, XLSX, or
animated HTML), edit it block by block, publish a downloadable file or a
shareable (optionally password-protected) link.

## Local setup

1. Create a Supabase project at https://supabase.com.
2. In the SQL editor, run [migrations/001_init.sql](migrations/001_init.sql), then
   [002_claude_artifact_input.sql](migrations/002_claude_artifact_input.sql),
   [003_html_upload_input.sql](migrations/003_html_upload_input.sql),
   [004_raw_html_artifacts.sql](migrations/004_raw_html_artifacts.sql), and
   [005_share_labels.sql](migrations/005_share_labels.sql), in order.
3. In Supabase Storage, create a **public** bucket named `deliverable-assets`.
   This is where already-uploaded images live; `image_block.image_ref` is a
   path into it. The AI never invents these - see the Artifact Contract note
   in TECHNICAL.md.
4. In Supabase Auth, create one user for yourself (internal login only —
   clients never get Supabase accounts).
5. Copy `.env.example` to `.env` and fill in:
   - `SUPABASE_URL`, `SUPABASE_KEY` — from Project Settings > API
     (use the `service_role` key; this app never runs under RLS as a client)
   - `OPENROUTER_API_KEY` — used for AI drafting (Phase 2), routed through
     [OpenRouter](https://openrouter.ai) rather than a provider SDK directly,
     so the model is swappable via `OPENROUTER_MODEL` (default
     `anthropic/claude-opus-5`) without code changes
   - `SESSION_SECRET` — any random string, signs the login and share-password
     session cookies
6. Install dependencies and run:
   ```
   pip install -r requirements.txt
   playwright install chromium   # one-time browser download, needed for the Claude-artifact importer below
   uvicorn app.main:app --reload
   ```
7. Visit http://localhost:8000, log in with the Supabase Auth user you created.

## Self-checks

No live Supabase/Anthropic connection needed - these run against an in-memory
fake and check the parts of the spec that are easy to silently get wrong:

```
python tests/test_versioning.py            # Save-Version-vs-Publish rule + status state machine
python tests/test_renderers.py             # pptx/xlsx/html each render every block type, skip disabled, pptx autofit
python tests/test_editor_route.py          # every page renders through the real ASGI app (auth, publish, password gate)
python tests/test_draft.py                 # AI draft parsing, retry-once, fail-closed-on-second-failure
python tests/test_claude_import.py         # artifact URL validation, fetch decision logic, source-type prompt framing
python tests/test_slide_duplication_guard.py  # real-browser (Playwright) proof that an uploaded deck's own
                                               # non-idempotent slide-render script can't duplicate/crash on reload
```

## What's built

All phases from TECHNICAL.md are implemented end to end:

- **Phase 1** — client/project CRUD, internal login (Supabase Auth + signed
  session cookie), block Pydantic models + discriminated union
  (`app/models/blocks.py`).
- **Phase 2** — input form (+ Phase 7's file upload, see below) and AI
  drafting (`app/ai/draft.py`, via OpenRouter - model configurable through
  `OPENROUTER_MODEL`, default `anthropic/claude-opus-5`): validates the
  response, retries once with the validation error appended, never persists
  on a second failure.
- **Phase 3** — block editor (`app/routers/editor.py`, `app/templates/projects/`):
  structured per-type editing (repeatable rows for lists/KPIs, a real grid
  editor for tables/charts - no pipe-delimited text fields), add/delete
  blocks beyond what the AI drafted, move up/down, enable/disable toggle,
  a live content preview pane, Save Version, Mark as In Review. Every edit
  clears `artifacts.current_version_id`.
- **Phase 4** — Publish implements the exact Save-Version-vs-Publish rule,
  PPTX renderer (`app/renderers/pptx.py`, with autofit so long client text
  shrinks to fit instead of overflowing the slide), password-protected
  public share route at `/s/{slug}` (bcrypt + signed cookie), view counting.
  A project can carry several independent published links at once, one per
  client (`shares.label`, no unique constraint on `artifact_id`) - Publish
  always creates a new link, while Republish/Delete on an existing link
  (`app/routers/publish.py`) act on just that one, leaving every other
  client's link and pinned version untouched.
- **Phase 5** — `tests/test_versioning.py` is the acceptance test: editing
  the draft after publish doesn't move the published pointer, and
  `projects.status` only ever transitions via the exact rules in TECHNICAL.md.
- **Phase 6** — XLSX renderer (`app/renderers/xlsx.py`, openpyxl) and
  animated HTML renderer (`app/renderers/html.py` + GSAP/Chart.js via CDN,
  `app/templates/render_html/page.html`). `/s/{slug}` branches by
  `deliverable_type`; all three renderers share the same block list contract.
- **Phase 7** — file upload input (PDF/DOCX/CSV via `app/input_parsing.py`,
  parsed into the same `raw_data` shape the manual form produces), rate
  limiting on the share password endpoint, share expiry enforcement, a
  simple total-view-count display.
- **Phase 3 addition — canvas view** — a second, visual way to edit the same
  draft (`app/templates/projects/_canvas.html`), toggled alongside the
  original form view and remembered via an `editor_view` cookie. Same
  blocks, same `/blocks/{id}/content|toggle|move` endpoints, same
  `_parse_content_form()` contract - the two views share their per-type
  field markup via a Jinja macro (`_block_fields.html`) rather than
  duplicating it, and every partial-swap endpoint picks its response
  template off the same cookie the page itself reads. Inline edits save
  on blur (htmx), disabled blocks stay visible but dimmed with a
  "Hidden — won't publish" flag, and drag-to-reorder calls the exact same
  `/move` endpoint the up/down buttons use, once per position moved.
- **Claude artifact URL as an input source** — paste a public
  `claude.ai/artifact/...` link on the input page instead of filling the
  form (`app/claude_artifact.py`). Real artifact pages are JS-hydrated (the
  content isn't in the raw server HTML), so this renders the page in a
  headless browser (Playwright) before extracting visible text - a
  login-gated or genuinely empty/broken artifact fails closed with a clear
  error rather than silently drafting from an empty page. A successful
  import feeds `app/ai/draft.py`'s exact same generate/validate/retry
  pipeline (just a different prompt framing for this source type: "restructure
  existing material" instead of "draft from a brief") and lands the user
  straight in the canvas view above to review it.
- **Animated HTML: "exact clone, edit in place"** — uploading a finished
  `.html`/`.htm` file (or importing a Claude-artifact URL) for an
  `animated_html` project stores it verbatim in `artifacts.raw_html`
  instead of extracting it into blocks, and edits it in an isolated iframe
  (`/projects/{id}/raw-editor`) rather than templating it away - the
  uploaded file's own JS/CSS/animations/navigation keep running. Slide
  navigation (prev/next/dots), a platform-level Build panel (bulk show/
  hide, persisted and reversible until publish), and a formatting toolbar
  (font/size/bold/italic/underline/color/highlight/bullets, native
  `execCommand`, reflects the live selection) all talk to the iframe over
  `postMessage`, never the parent DOM directly. See TECHNICAL.md's "Animated
  HTML" section for the architecture and the specific failure modes this
  had to be hardened against (a deck's own non-idempotent render script
  duplicating on reload, embedded CSPs blocking autosave, stripping DOM
  elements a deck's script still depends on, forced animation properties
  permanently overriding a deck's own looping animations).
- **App-wide sidebar + client/project management** — every authenticated
  page (`base.html`) shows a collapsible client → project tree with inline
  rename/delete for both and quick-add for new clients/projects. Delete
  cascades through the existing FK chain (`migrations/001_init.sql`), no
  manual cleanup code.

## Known simplifications (marked `ponytail:` in the code)

- The public-password rate limiter is in-process memory - fine for one
  worker, resets on restart, and won't share state if you ever scale to
  multiple workers. Move to Redis/DB if that happens.
- A broken `image_ref` (deleted asset, bad path) is skipped rather than
  failing the whole render - acceptable until Phase 7's file upload makes
  broken refs a more common failure mode worth surfacing to the editor.
- "Views over time" is the raw `shares.view_count` total, not a real
  time-series - the schema has no per-view log table, and TECHNICAL.md's scope
  discipline explicitly rules out adding audit/tracking tables for v1.
- The Claude artifact importer launches a fresh headless Chromium per import
  rather than a pooled/shared browser - it's an occasional manual action,
  not a hot path. Pool it if that stops being true.
- `strip_hidden_slides()` (removes a deselected slide from the published/
  exported copy entirely, rather than just CSS-hiding it) is only known
  safe for decks whose own render script has already been made idempotent
  by `make_slide_render_idempotent()` - verified live for the one deck
  structure seen so far. A structurally different deck would need the same
  live (real-browser, zero-console-error) verification before trusting it,
  not just trusting the string-level unit tests.
- The formatting toolbar's font-family/font-size dropdowns only reflect a
  match if the selection's computed value matches one of the small preset
  list - a font outside that list just shows blank rather than "detecting"
  an arbitrary value, since the control is a fixed `<select>`, not free text.
