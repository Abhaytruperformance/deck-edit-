"""Import raw content from a public Claude.ai artifact URL as an input source
(alongside the form and file-upload paths in app/input_parsing.py), feeding
the same generate_draft() pipeline.

Real claude.ai artifact pages are JS-hydrated (content is built client-side,
not present in the raw server HTML - see the corporate-deck example that
prompted this), so a plain HTTP GET only sees empty chrome. This renders the
page in a headless browser (Playwright) first, then reads the same visible
text a human viewer would see - across every frame, since Claude serves the
actual artifact content from a sandboxed iframe on a separate origin
(*.claudeusercontent.com), not the main claude.ai document.

A flat text dump of a rich deck (headings, stat callouts, tables all
flattened into one blob) produces a single oversized text_block on draft -
the render then looks broken because the content was never structured to
begin with. So extraction here preserves what structure it can: headings
become block breaks, real <table> elements become pipe-delimited rows the
draft prompt can parse into comparison_table, and any <img> assets actually
present in the rendered page (logos, photos) get uploaded to Storage and
handed to the drafting model as legitimate already-uploaded image refs -
never fabricated, per the Artifact Contract's image_block rule.

claude.ai also fronts these pages with Cloudflare bot protection that
blocks Playwright's default (automation-flagged) user agent outright with a
403 "security verification" page, regardless of the artifact's own sharing
setting - a plain desktop user agent is enough to get past it.

This still isn't an authenticated Claude.ai client - it only works on
artifacts the owner made public. A login-gated artifact is caught by
Claude's own redirect to /login; a genuinely empty/broken artifact is caught
by the near-empty-text threshold below.
"""
import base64
import hashlib
import json
import re
import uuid
from html.parser import HTMLParser
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

_ARTIFACT_PATH_RE = re.compile(r"^/(artifact|code/artifact)/")
_SKIP_TAGS = {"script", "style", "noscript", "template"}
MIN_TEXT_CHARS = 200  # below this, treat as a broken/empty artifact rather than draft from it
NAV_TIMEOUT_MS = 20_000
SETTLE_MS = 3_000  # let client-side render (and Cloudflare's JS check) finish after "load"
MAX_IMAGES = 15  # cap uploads for decks that reuse the same logo dozens of times

# Playwright's default UA advertises itself as a headless/automated browser,
# which Cloudflare's bot protection in front of claude.ai blocks outright
# (403, "Performing security verification") before the artifact ever loads -
# regardless of whether the artifact itself is shared. A plain desktop UA
# gets past it.
_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Runs inside the page: walks the DOM (not innerText, which flattens
# structure) so headings and tables survive as recognizable text markers
# instead of dissolving into one undifferentiated blob.
_TEXT_WALK_JS = """
() => {
  const SKIP = new Set(['script','style','noscript','template']);
  const out = [];
  function walk(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      const t = node.textContent.trim();
      if (t) out.push(t);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const tag = node.tagName.toLowerCase();
    if (SKIP.has(tag)) return;
    if (/^h[1-6]$/.test(tag)) {
      const t = node.innerText.trim();
      if (t) out.push('\\n\\n## ' + t + '\\n');
      return;
    }
    if (tag === 'table') {
      const rows = Array.from(node.querySelectorAll('tr')).map(tr =>
        Array.from(tr.querySelectorAll('th,td')).map(c => c.innerText.trim()).join(' | ')
      ).filter(r => r.trim());
      if (rows.length) out.push('\\n\\n' + rows.join('\\n') + '\\n');
      return;
    }
    for (const child of node.childNodes) walk(child);
  }
  walk(document.body);
  return out.join('\\n');
}
"""

# Runs inside the page: only real <img> assets, since a deck's decorative
# icons are almost always inline <svg> in this kind of artifact - this
# naturally filters to the photos/logos worth uploading.
_IMAGES_JS = """
() => Array.from(document.querySelectorAll('img')).map(img => ({
  src: img.currentSrc || img.src,
  alt: img.alt || '',
}))
"""


# Injected into a stored raw_html document each time it's served for
# editing (never persisted - serialize() strips it back out before saving,
# so re-injecting on every GET is idempotent). Makes the whole body directly
# editable in place (native contenteditable, exact original CSS/JS intact),
# autosaves on pause-in-typing or losing focus, and - when the document looks
# like a multi-slide deck (a `.slide` convention, or any repeated-sibling
# block under a common parent) - adds slide-level structural controls
# (focus one slide at a time, hide/show, delete, reorder) driven by
# postMessage from the outer editor page, since a contenteditable region on
# its own has no notion of "one slide" to jump to, disable or reorder.
_EDIT_SCRIPT = """
<script data-editor-injected="true">
(function() {
  document.body.setAttribute('contenteditable', 'true');
  document.body.style.outline = 'none';

  var indicator = document.createElement('div');
  indicator.id = '__editor_indicator__';
  indicator.setAttribute('data-editor-injected', 'true');
  indicator.style.cssText = 'position:fixed;top:8px;right:8px;background:#1a1a1a;color:#fff;' +
    'font:12px sans-serif;padding:4px 10px;border-radius:4px;z-index:2147483647;opacity:0;' +
    'transition:opacity .2s;pointer-events:none;';
  document.body.appendChild(indicator);

  function show(text) {
    indicator.textContent = text;
    indicator.style.opacity = '1';
  }

  function serialize() {
    var clone = document.documentElement.cloneNode(true);
    var body = clone.querySelector('body');
    if (body) body.removeAttribute('contenteditable');
    clone.querySelectorAll('[data-editor-injected]').forEach(function(s) { s.remove(); });
    clone.querySelectorAll('.editor-current').forEach(function(s) { s.classList.remove('editor-current'); });
    clone.querySelectorAll('[data-editor-slide]').forEach(function(s) {
      s.style.removeProperty('display');
      s.style.removeProperty('opacity');
      s.style.removeProperty('visibility');
      s.style.removeProperty('pointer-events');
    });
    clone.querySelectorAll('[data-editor-revealed]').forEach(function(el) {
      el.style.removeProperty('opacity');
      el.style.removeProperty('transform');
      el.style.removeProperty('transition');
      el.removeAttribute('data-editor-revealed');
    });
    // The selection outline (see selectImage()) is edit-time-only, unlike
    // the width/height a resize drag sets - only strip the former.
    clone.querySelectorAll('img').forEach(function(img) { img.style.removeProperty('outline'); });
    return '<!doctype html>\\n' + clone.outerHTML;
  }

  var saveTimer = null;
  var dirty = false;
  function save() {
    if (!dirty) return Promise.resolve();
    dirty = false;
    show('Saving...');
    return fetch(window.location.pathname + '/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({html: serialize()}),
    }).then(function(r) {
      show(r.ok ? 'Saved' : 'Save failed');
      setTimeout(function() { indicator.style.opacity = '0'; }, 1200);
      return r;
    }).catch(function(e) { show('Save failed'); throw e; });
  }

  document.body.addEventListener('input', function() {
    dirty = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 1500);
  });
  // Capturing blur listener: fires not just when a user clicks away after
  // typing, but also when our own slide navigation hides the slide the
  // caret happened to be in (display:none forces a blur) - so this must
  // stay a no-op unless an actual edit (the `dirty` flag above) happened,
  // or every nav click would look like a save.
  document.body.addEventListener('blur', save, true);

  // Formatting toolbar (bold/italic/underline/font/size/color/bullets),
  // driven from the parent page - a text selection only means something in
  // the document it was made in, so the actual execCommand() calls have to
  // run in here, not in the parent. Registered before the slide-detection
  // gate below (which can exit this whole IIFE early) since formatting is
  // meaningful on any contenteditable page, slide deck or not.
  //
  // execCommand('fontSize', ...) only understands the legacy 1-7 <font
  // size> scale, not real px/pt values - the standard workaround is to let
  // it wrap the selection in a throwaway `size="7"` marker, then swap that
  // marker for a real inline font-size in px.
  window.addEventListener('message', function(e) {
    var m = e.data;
    if (!m || m.source !== 'rawEditorHost' || m.cmd !== 'format') return;
    if (m.action === 'fontSizePx') {
      document.execCommand('fontSize', false, '7');
      document.querySelectorAll('font[size="7"]').forEach(function(el) {
        el.removeAttribute('size');
        el.style.fontSize = m.value + 'px';
      });
    } else if (m.action === 'foreColor' || m.action === 'hiliteColor' || m.action === 'fontName') {
      document.execCommand(m.action, false, m.value);
    } else {
      document.execCommand(m.action, false, null);
    }
    dirty = true;
    save();
  });

  // Reports the current selection's actual font/size/color/bold/italic/
  // underline back to the parent toolbar, so it reflects what's really
  // selected (like any real editor) instead of always showing blank
  // placeholders regardless of what the cursor is sitting in.
  function rgbToHex(rgb) {
    var m = rgb.match(/\\d+(\\.\\d+)?/g);
    if (!m) return null;
    return '#' + m.slice(0, 3).map(function(n) {
      return ('0' + Math.round(parseFloat(n)).toString(16)).slice(-2);
    }).join('');
  }
  function reportFormatState() {
    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    var node = sel.anchorNode;
    var el = node && node.nodeType === 3 ? node.parentElement : node;
    if (!el || !document.body.contains(el)) return;
    var cs = getComputedStyle(el);
    parent.postMessage({
      source: 'rawEditor', type: 'formatState',
      fontFamily: cs.fontFamily,
      fontSize: Math.round(parseFloat(cs.fontSize)),
      color: rgbToHex(cs.color),
      backgroundColor: /rgba?\\(0, ?0, ?0, ?0\\)|transparent/.test(cs.backgroundColor) ? null : rgbToHex(cs.backgroundColor),
      bold: document.queryCommandState('bold'),
      italic: document.queryCommandState('italic'),
      underline: document.queryCommandState('underline'),
    }, '*');
  }
  document.addEventListener('selectionchange', reportFormatState);

  // Image controls: single-click selects an <img> and shows a drag handle to
  // resize it; double-click replaces it. Two different gestures on purpose -
  // a single click can't also open a file picker, or there'd be no way to
  // just select-and-resize without a dialog popping up every time.
  // ponytail: a deck whose own nav icons happen to be <img> tags (rather
  // than svg/button, the common case) would have those hijacked into image
  // controls too - acceptable for now, revisit if that deck shows up.
  var selectedImg = null;
  var handle = document.createElement('div');
  handle.id = '__editor_resize_handle__';
  handle.setAttribute('data-editor-injected', 'true');
  handle.style.cssText = 'position:fixed;width:12px;height:12px;background:#ff5a24;' +
    'border:2px solid #fff;border-radius:50%;box-shadow:0 0 2px rgba(0,0,0,.5);' +
    'cursor:nwse-resize;z-index:2147483647;display:none;';
  document.body.appendChild(handle);

  function positionHandle() {
    if (!selectedImg) return;
    var r = selectedImg.getBoundingClientRect();
    handle.style.left = (r.right - 6) + 'px';
    handle.style.top = (r.bottom - 6) + 'px';
  }

  function selectImage(img) {
    if (selectedImg) selectedImg.style.outline = '';
    selectedImg = img;
    img.style.outline = '2px solid #ff5a24';
    handle.style.display = 'block';
    positionHandle();
  }

  function deselectImage() {
    if (selectedImg) selectedImg.style.outline = '';
    selectedImg = null;
    handle.style.display = 'none';
  }

  document.body.addEventListener('click', function(e) {
    var img = e.target.closest && e.target.closest('img');
    if (img) { e.preventDefault(); selectImage(img); return; }
    if (e.target !== handle) deselectImage();
  });
  window.addEventListener('scroll', positionHandle, true);
  window.addEventListener('resize', positionHandle);

  handle.addEventListener('mousedown', function(e) {
    if (!selectedImg) return;
    e.preventDefault();
    var img = selectedImg;
    var startX = e.clientX, startY = e.clientY;
    var startW = img.offsetWidth, startH = img.offsetHeight;
    var ratio = startW / (startH || 1);
    function onMove(me) {
      var w = Math.max(20, startW + (me.clientX - startX));
      img.style.width = w + 'px';
      img.style.height = (w / ratio) + 'px';
      positionHandle();
    }
    function onUp() {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      dirty = true;
      save();
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  // Double-click an <img> to replace it - uploads through the same-origin
  // upload-image endpoint (this iframe is served by our own app, so a plain
  // fetch carries the session cookie just like the /save call above) and
  // writes the returned public URL straight into src.
  document.body.addEventListener('dblclick', function(e) {
    var img = e.target.closest && e.target.closest('img');
    if (!img) return;
    e.preventDefault();
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.style.display = 'none';
    input.addEventListener('change', function() {
      var file = input.files[0];
      input.remove();
      if (!file) return;
      show('Uploading...');
      var form = new FormData();
      form.append('file', file);
      fetch(window.location.pathname + '/upload-image', {method: 'POST', body: form})
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (!data.url) { show('Upload failed'); return; }
          img.src = data.url;
          dirty = true;
          save();
          positionHandle();
        })
        .catch(function() { show('Upload failed'); });
    });
    document.body.appendChild(input);
    input.click();
  });
  // A stylesheet (not img.style/img.title, which would get baked into the
  // saved/published/exported HTML) so the pointer cursor is edit-time only -
  // serialize() below already strips anything [data-editor-injected].
  var imgCursorStyle = document.createElement('style');
  imgCursorStyle.setAttribute('data-editor-injected', 'true');
  imgCursorStyle.textContent = 'img{cursor:pointer}';
  document.head.appendChild(imgCursorStyle);

  /* Slide-level controls: only activates when the upload looks like a deck.
     Prefers any element whose class name contains "slide" (case-insensitive
     - covers ".slide", ".deck-slide", ".slide-panel", etc.), grouped by
     shared parent so a stray "prev-slide-btn" doesn't get pulled in
     alongside real slide containers. Otherwise falls back to whichever
     parent has the most children sharing an identical tag+class signature -
     a generic "repeated card" heuristic, skipping typical inline/decorative
     tags so those don't get mistaken for slides. */
  // A deck that builds its own slides from a JS template array at boot
  // (seen live: `S.push({mod, html})` then rendered into a container) can
  // be non-idempotent - if that boot script runs again against a page
  // that was saved AFTER it already rendered once (which is exactly what
  // "exact clone, edit in place" produces after the first save: the saved
  // HTML already contains the rendered slides, plus the same boot script,
  // which then runs again on the next load), it re-inserts a second full
  // copy on top of the first with no idempotency guard of its own. This
  // shows up as an exact front-half/back-half repeat and needs cleaning up
  // every time the page loads, not just once - it isn't caused by
  // anything we do, it's inherent to how that script behaves whenever it's
  // given an already-rendered starting point.
  function dedupeIfNeeded(list) {
    var n = list.length;
    if (n < 2 || n % 2 !== 0) return list;
    var half = n / 2;
    // Compare rendered text, not outerHTML - the deck's own boot script was
    // seen assigning some per-render attribute (an instance id/key) that
    // differs between the two passes even though the visible content is
    // byte-identical, which made an exact outerHTML match too strict to
    // ever fire. A majority match (not unanimous) still counts as the same
    // duplication bug - seen live, one slide had already been edited
    // (through the "first" copy, unknowingly, before the duplicate was
    // noticed) by the time this ran, which would otherwise make one
    // mismatched pair block cleanup of the other 31 genuine duplicates.
    var matches = 0;
    for (var i = 0; i < half; i++) {
      if (list[i].textContent === list[i + half].textContent) matches++;
    }
    if (matches < half * 0.9) return list;
    for (var j = half; j < n; j++) list[j].remove();
    return list.slice(0, half);
  }

  function detectSlides() {
    var bySlideClass = null, bestSlideCount = 1;
    var groups = new Map();
    // [data-editor-injected] elements (our own UI: save indicator, resize
    // handle, ...) are plain siblings appended straight to <body> - on a
    // page with no real slide markup at all, the generic "repeated card"
    // heuristic below can otherwise mistake two of THEM for a pair of
    // duplicate slides and .remove() one via dedupeIfNeeded. Both branches
    // must skip our own chrome, not just real page content.
    document.querySelectorAll('body *').forEach(function(el) {
      if (el.hasAttribute('data-editor-injected')) return;
      if (typeof el.className !== 'string' || !/slide/i.test(el.className)) return;
      var parent = el.parentElement;
      if (!parent) return;
      var byTag = groups.get(parent) || new Map();
      groups.set(parent, byTag);
      var arr = byTag.get(el.tagName) || [];
      arr.push(el);
      byTag.set(el.tagName, arr);
    });
    groups.forEach(function(byTag) {
      byTag.forEach(function(arr) {
        if (arr.length > bestSlideCount) { bestSlideCount = arr.length; bySlideClass = arr; }
      });
    });
    if (bySlideClass) return dedupeIfNeeded(bySlideClass);

    var INLINE = {SPAN: 1, I: 1, B: 1, EM: 1, SMALL: 1, A: 1, SVG: 1, PATH: 1, BUTTON: 1, LABEL: 1};
    var best = null, bestCount = 1;
    document.querySelectorAll('body, body *').forEach(function(el) {
      var counts = {};
      Array.prototype.forEach.call(el.children, function(c) {
        if (!c.tagName || INLINE[c.tagName] || c.hasAttribute('data-editor-injected')) return;
        var key = c.tagName + '.' + c.className;
        counts[key] = (counts[key] || 0) + 1;
      });
      Object.keys(counts).forEach(function(key) {
        if (counts[key] > bestCount) { bestCount = counts[key]; best = {parent: el, key: key}; }
      });
    });
    if (!best) return [];
    return dedupeIfNeeded(Array.prototype.filter.call(best.parent.children, function(c) {
      return (c.tagName + '.' + c.className) === best.key;
    }));
  }

  var slides = detectSlides();
  if (slides.length < 2) return;
  slides.forEach(function(s, i) { s.setAttribute('data-editor-slide', i); });

  // Editing-time only: keeps a slide the user turned off in the Build panel
  // hidden in this same view too. The published/exported copy goes further
  // (the element is removed outright server-side, see strip_hidden_slides
  // in claude_artifact.py) - this is just so it doesn't reappear here.
  var persistStyle = document.getElementById('__persist_hidden__');
  if (!persistStyle) {
    persistStyle = document.createElement('style');
    persistStyle.id = '__persist_hidden__';
    document.head.appendChild(persistStyle);
  }
  persistStyle.textContent = '[data-editor-hidden]{display:none!important}';

  // First heading text on the slide, so a "Build" overview panel can show
  // something more useful than "Slide 7" - best-effort, falls back to null.
  function labelFor(s) {
    var h = s.querySelector('h1, h2, h3');
    var t = h ? h.textContent.replace(/\\s+/g, ' ').trim() : '';
    return t ? t.slice(0, 60) : null;
  }

  // Many decks in this "animated_html" line of work reveal a slide's own
  // content (word-by-word fades, staggered entrances) only in response to
  // their own next/prev handler actually running - jumping straight there
  // via display/opacity on the slide container alone leaves that content
  // stuck at its pre-animation (typically invisible) state, and skips
  // whatever transition the deck itself would have played.
  //
  // A first attempt drove this with dispatched keydown events, but got
  // reverted after appearing to cause slide duplication - later traced to
  // a completely different bug (a deck's own non-idempotent
  // insertAdjacentHTML re-running against an already-rendered page on
  // reload; see make_slide_render_idempotent, now applied permanently at
  // upload time). Re-verified live with 40 rapid synthetic keydowns
  // against a since-guarded page: zero duplication. The keydown dispatch
  // was never the cause. Restored below (stepNative), now the primary
  // mechanism - it lets the deck's OWN transition play naturally, exactly
  // like a real click, instead of us faking the end state.
  //
  // revealCurrent() stays as a delayed fallback for decks with no keydown
  // listener at all: running it immediately would force-complete the
  // opacity/transform before the deck's own (now correctly triggered)
  // transition has had time to visibly play, defeating the point of
  // dispatching the keydown in the first place. It only touches elements
  // that are ACTUALLY still hidden (computed opacity below ~1) - a first
  // version forced every element in
  // the slide regardless, which also permanently froze any decorative
  // element with its own continuous/looping animation (a pulsing glow, a
  // moving gradient): an inline !important on opacity/transform overrides
  // a CSS animation on those same properties for good, per spec, with no
  // way for the animation to ever win it back.
  //
  // Gating on low opacity alone still wasn't enough - a looping animation
  // (animation-iteration-count: infinite) legitimately dips through low
  // opacity as part of its normal cycle, and sampling it at the wrong
  // instant looked identical to a one-shot reveal that got stuck. Only an
  // element with NO infinite animation is treated as "stuck hidden";
  // anything actually mid-loop is left completely alone.
  function revealCurrent(slide) {
    document.querySelectorAll('[data-editor-revealed]').forEach(function(el) {
      el.style.removeProperty('opacity');
      el.style.removeProperty('transform');
      el.style.removeProperty('transition');
      el.removeAttribute('data-editor-revealed');
    });
    var hidden = [];
    slide.querySelectorAll('*').forEach(function(el) {
      var cs = getComputedStyle(el);
      var op = parseFloat(cs.opacity);
      if (isNaN(op) || op >= 0.95) return;
      if (cs.animationIterationCount.split(',').some(function(v) { return v.trim() === 'infinite'; })) return;
      hidden.push(el);
    });
    hidden.forEach(function(el) {
      el.style.setProperty('transition', 'none', 'important');
      el.setAttribute('data-editor-revealed', '');
    });
    // Force the pre-reveal (hidden) state to actually apply before turning
    // transitions back on, or the browser has nothing to animate from and
    // just snaps straight to the end state - same reason a plain CSS class
    // toggle needs a reflow in between to visibly transition.
    void slide.offsetHeight;
    hidden.forEach(function(el) {
      el.style.setProperty('transition', 'opacity .35s ease, transform .35s ease', 'important');
      el.style.setProperty('opacity', '1', 'important');
      el.style.setProperty('transform', 'none', 'important');
    });
  }

  // Steps the deck's OWN arrow-key navigation, if it has any (near-universal
  // in this family of generated decks) - dispatching real key events lets
  // any reveal-on-enter animation the deck plays run exactly as it would
  // for a real user, rather than us faking the end state. Safe against the
  // slide-duplication bug seen earlier: that was a completely separate
  // issue (see the comment above revealCurrent), independently and
  // permanently fixed regardless of how navigation is driven - re-verified
  // live with a rapid 40-event burst against a since-guarded page.
  function stepNative(times, key) {
    for (var n = 0; n < times; n++) {
      document.dispatchEvent(new KeyboardEvent('keydown', {key: key, bubbles: true, cancelable: true}));
    }
  }

  var cur = 0;
  function focus(i) {
    var target = Math.max(0, Math.min(slides.length - 1, i));
    var delta = target - cur;
    stepNative(Math.abs(delta), delta > 0 ? 'ArrowRight' : 'ArrowLeft');
    cur = target;
    // Inline !important beats any external stylesheet rule regardless of
    // its selector specificity - decks like this one ship their own
    // "current slide" CSS (an attribute like [data-off]) that would
    // otherwise fight a same-specificity <style> tag and leave the slide
    // looking blank even though its container is showing.
    slides.forEach(function(s, idx) {
      var on = idx === cur;
      s.classList.toggle('editor-current', on);
      s.style.setProperty('display', on ? 'block' : 'none', 'important');
      s.style.setProperty('opacity', on ? '1' : '0', 'important');
      s.style.setProperty('visibility', on ? 'visible' : 'hidden', 'important');
      s.style.setProperty('pointer-events', on ? 'auto' : 'none', 'important');
    });
    // Gives the deck's own (now correctly triggered) transition time to
    // actually play before force-completing anything still stuck - firing
    // this immediately would snap straight past the animation stepNative()
    // above was just asked to start.
    setTimeout(function() { revealCurrent(slides[cur]); }, 500);
    // Many generated decks run their own slide engine keyed off
    // "#<1-based index>" in the URL hash (for deep-linking) and show their
    // own on-page "n / total" readout driven by that same state. Nudging
    // it keeps that native readout in sync with our toolbar instead of the
    // two silently disagreeing; harmless no-op on a deck that ignores hash
    // changes, since our own inline overrides above already did the work.
    try { window.location.hash = '#' + (cur + 1); } catch (e) {}
    postState();
  }
  function postState() {
    parent.postMessage({source: 'rawEditor', type: 'slideState',
      index: cur, total: slides.length, hidden: slides[cur].hasAttribute('data-editor-hidden'),
      list: slides.map(function(s, i) {
        return {index: i, hidden: s.hasAttribute('data-editor-hidden'), label: labelFor(s)};
      })}, '*');
  }

  window.addEventListener('message', function(e) {
    var m = e.data;
    if (!m || m.source !== 'rawEditorHost') return;
    if (m.cmd === 'goto') { focus(m.i); return; }
    if (m.cmd === 'toggle') {
      // No index given (main toolbar) -> the focused slide; an index
      // (the Build panel) -> that row, without moving focus.
      var s = slides[typeof m.i === 'number' ? m.i : cur];
      if (!s) return;
      if (s.hasAttribute('data-editor-hidden')) s.removeAttribute('data-editor-hidden');
      else s.setAttribute('data-editor-hidden', '');
      dirty = true;
      postState();
      save();
    }
  });

  // A deck can default to a slide other than its first (saved state, a
  // hash from a previous visit, etc.) - step its own nav firmly back to 0
  // first so `cur` above starts as a value we know is actually correct.
  stepNative(slides.length + 5, 'ArrowLeft');
  var initHash = /^#(\\d+)$/.exec(window.location.hash);
  var initIndex = initHash ? Number(initHash[1]) - 1 : 0;
  focus(initIndex);
})();
</script>
"""


class _ElementSpanFinder(HTMLParser):
    """Locates the exact [start, end) character spans of top-level elements
    matching `predicate(tag, attrs_dict)`, so callers can cut them straight
    out of the original html string. Deliberately not re-serializing the
    parsed tree back out - that would risk subtly reformatting attribute
    quoting/whitespace in the parts we keep, breaking the "exact clone"
    guarantee for content that was never meant to change. Tracks
    matching-tag depth so an unrelated same-tag descendant (e.g. a nested
    <section> or <div>) doesn't end the span early."""

    def __init__(self, html: str, predicate):
        super().__init__(convert_charrefs=False)
        self.html = html
        self.predicate = predicate
        offsets = [0]
        for line in html.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        self._line_offsets = offsets
        self.spans: list[tuple[int, int]] = []
        self._open_tag = None
        self._depth = 0
        self._start = None

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_offsets[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if self._open_tag is not None:
            if tag == self._open_tag:
                self._depth += 1
            return
        if self.predicate(tag, dict(attrs)):
            self._open_tag = tag
            self._depth = 1
            self._start = self._offset()

    def handle_endtag(self, tag):
        if self._open_tag is None or tag != self._open_tag:
            return
        self._depth -= 1
        if self._depth == 0:
            close_at = self.html.find(">", self._offset())
            end = close_at + 1 if close_at != -1 else len(self.html)
            self.spans.append((self._start, end))
            self._open_tag = None
            self._start = None


def _remove_spans(html: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return html
    out = []
    pos = 0
    for start, end in spans:
        out.append(html[pos:start])
        pos = end
    out.append(html[pos:])
    return "".join(out)


_TAG_RE = re.compile(r"<[^>]+>")


def dedupe_slides_if_needed(html: str) -> str:
    """Safety net at save time against a slide-duplication bug seen live: a
    deck's own slide-building script isn't idempotent, and re-running it
    against an already-rendered page (exactly what "exact clone, edit in
    place" produces after the first save) inserts a second full copy of
    every slide. Runs unconditionally on every save as a backstop against
    this whole class of bug, not just while any one trigger for it exists.
    Detects a front-half/back-half repeat of [data-editor-slide] elements
    and drops the second half. Compares rendered text rather than raw
    markup - the duplicating script was seen assigning some per-render
    attribute (an instance id/key) that differs between the two passes even
    though the visible content is byte-identical, which made a raw-HTML
    comparison too strict to ever fire. Requires a majority match, not a
    unanimous one - seen live, one slide had already been edited (through
    one of the two copies, unknowingly, before the duplicate was noticed)
    by the time this ran, which would otherwise let one mismatched pair
    block cleanup of the other 31 genuine duplicates."""
    def is_slide(tag, attrs):
        return "data-editor-slide" in attrs

    finder = _ElementSpanFinder(html, is_slide)
    finder.feed(html)
    spans = finder.spans
    n = len(spans)
    if n < 2 or n % 2 != 0:
        return html
    half = n // 2

    def text_of(start, end):
        return _TAG_RE.sub("", html[start:end])

    first_half = [text_of(s, e) for s, e in spans[:half]]
    second_half = [text_of(s, e) for s, e in spans[half:]]
    matches = sum(1 for a, b in zip(first_half, second_half) if a == b)
    if matches < half * 0.9:
        return html
    return _remove_spans(html, spans[half:])


_INSERT_ADJACENT_OPEN_RE = re.compile(
    r"[A-Za-z_$][\w$.]*\.insertAdjacentHTML\(\s*['\"](?:beforeend|afterbegin)['\"]\s*,"
)


def make_slide_render_idempotent(html: str) -> str:
    """A deck that builds its own slides at boot by appending markup into an
    existing container (`container.insertAdjacentHTML('beforeend', ...)`) is
    only safe to run once. "Exact clone, edit in place" means every save
    stores the ALREADY-rendered DOM, script tag included - so the next time
    that page loads, the same script runs again against a container that
    already holds the previous render. Seen live: this duplicates every
    slide (32 -> 64), and then crashes ("Cannot read properties of
    undefined (reading 'mod')") because the script's own per-slide
    bookkeeping array is sized for one render's worth of slides but gets
    indexed against DOM nodes from both. That crash halts the rest of the
    script too - not just navigation, every animation on the page stops.

    Wraps each such append in a marker-attribute guard (only fires if the
    target doesn't already carry `data-editor-rendered`, then sets it) so it
    runs exactly once, same as the original, never-edited file - without
    touching anything else in the script, including the user's
    already-rendered edits. Applied once, at upload time (see
    _save_raw_html in app/routers/projects.py), so it's baked into the
    stored html permanently rather than needing to run on every read; the
    marker attribute itself gets serialized along with everything else on
    the first real save, so later loads see it and correctly skip re-running.

    Deliberately NOT a `target.children.length===0` check (an earlier
    version used that): a deck can ship a static placeholder already inside
    the container by design (seen live - a `<div id="prog">` progress node
    sitting inside the slide container from the very first, never-rendered
    load), which made children.length nonzero from the start and permanently
    blocked the real render from ever firing, on every load including the
    first - the deck looked entirely blank/black. A marker we control isn't
    fooled by unrelated static markup already in the container."""

    # Process matches back-to-front so replacing one match doesn't shift the
    # character offsets the next (earlier) match's own paren-scan relies on.
    matches = list(_INSERT_ADJACENT_OPEN_RE.finditer(html))
    out = html
    for m in reversed(matches):
        call_paren = out.index("(", out.index("insertAdjacentHTML", m.start()))
        depth = 1
        i = call_paren + 1
        while i < len(out) and depth > 0:
            if out[i] == "(":
                depth += 1
            elif out[i] == ")":
                depth -= 1
            i += 1
        target = out[m.start() : m.end()].split(".insertAdjacentHTML", 1)[0]
        # A call site already wrapped by an earlier pass would otherwise get
        # wrapped a second time (the guard's own comma expression still
        # contains the literal "target.insertAdjacentHTML(" substring this
        # regex matches on) - and a *nested* guard actually breaks the first
        # real render, since the outer layer's setAttribute() runs before
        # the inner layer's own hasAttribute() check, which then always sees
        # the marker already set and never fires the real call. Skip
        # call sites that are already guarded instead.
        already_guarded = f"hasAttribute('data-editor-rendered')&&(({target}).setAttribute('data-editor-rendered','1'),"
        lookback = out[max(0, m.start() - len(already_guarded) - 10) : m.start()]
        if already_guarded in lookback:
            continue
        original_call = out[m.start() : i]
        guarded = (
            f"(!({target}).hasAttribute('data-editor-rendered')&&"
            f"(({target}).setAttribute('data-editor-rendered','1'),{original_call}))"
        )
        out = out[: m.start()] + guarded + out[i:]
    return out


def strip_hidden_slides(html: str) -> str:
    """Removes slides the user turned off in the Build panel entirely,
    rather than leaving them CSS-hidden - a CSS-only hide left them
    reachable as blank "ghost slides" by a deck's own next/prev navigation
    (its own bookkeeping array still counts them; only their box goes
    invisible, revealing the deck's background underneath).

    This was disabled for a while: a deck's own boot script can track
    slides by index/count against its own internal `{mod, html}` template
    array, and physically deleting DOM elements used to desync that array
    from the live DOM (`Cannot read properties of undefined (reading
    'mod')`, halting the whole script). Investigation found the actual
    cause wasn't removal itself - it was that removal was being combined
    with a *duplication* bug (a non-idempotent `container.insertAdjacentHTML`
    re-running on reload, see make_slide_render_idempotent), which made the
    live DOM have MORE elements than the script's own array expected.
    Removal alone only ever produces FEWER DOM elements than that array,
    which every observed access pattern in these decks indexes safely.
    Confirmed live (Playwright, zero console errors across repeated
    navigation) once make_slide_render_idempotent() runs first - which it
    always does now, applied once at upload time in
    app/routers/projects.py's _save_raw_html, so by the time this function
    ever sees a document the guard is already permanently baked in."""
    finder = _ElementSpanFinder(html, lambda tag, attrs: "data-editor-hidden" in attrs)
    finder.feed(html)
    return _remove_spans(html, finder.spans)


_NATIVE_CHROME_GATES = {
    "bb": re.compile(r">\s*Build\s*<", re.IGNORECASE),
    "pn": re.compile(r"Build the deck", re.IGNORECASE),
    "hint": re.compile(r"navigate", re.IGNORECASE),
}


def strip_native_authoring_chrome(html: str) -> str:
    """Some uploaded decks bundle their own author-only controls right into
    the file: a "Build"/slide-toggle button and the panel it opens, plus a
    small on-page hint naming its keyboard shortcut - meant for whoever
    assembled the deck to trim before sharing, not for the client opening
    the published link. Their ids alone (id="bb"/"pn"/"hint") are generic
    enough that some other deck could reuse them for something unrelated,
    so each candidate is only hidden once its own markup also carries a
    matching, deliberate signal ("Build", "Build the deck", "navigate").

    Hides via CSS rather than removing the elements from the DOM - an
    earlier version deleted them outright, which crashed a real deck's own
    boot script ("Cannot read properties of null (reading 'appendChild')")
    because it still looks up one of these elements (a module-toggle list
    living inside the panel) by id and populates it unconditionally,
    without checking it exists first. Same lesson as strip_hidden_slides:
    a deck's own script can depend on markup being present that has no
    visible purpose once hidden, so removing it outright is unsafe in a
    way CSS-hiding isn't. Deliberately leaves everything else alone - the
    deck's real Prev/Next/dot navigation (which happened to live in the
    same container as the Build button, and got wrongly removed along with
    it in an earlier version of this fix) and its reveal-on-enter
    animations (which an even earlier version force-skipped to their end
    state by promoting the deck's own @media print stylesheet - "flatten
    to a static page" is the wrong target for a deliverable whose whole
    point is to be interactive and animated)."""
    def is_candidate(tag, attrs):
        return attrs.get("id") in _NATIVE_CHROME_GATES

    finder = _ElementSpanFinder(html, is_candidate)
    finder.feed(html)
    hide_ids = []
    for start, end in finder.spans:
        snippet = html[start:end]
        tag_close = snippet.find(">")
        opening_tag = snippet[: tag_close + 1] if tag_close != -1 else snippet
        id_match = re.search(r'id="([^"]*)"', opening_tag)
        gate = _NATIVE_CHROME_GATES.get(id_match.group(1)) if id_match else None
        if gate and gate.search(snippet):
            hide_ids.append(id_match.group(1))
    if not hide_ids:
        return html
    rule = ",".join(f"#{i}" for i in hide_ids)
    style_tag = f"<style>{rule}{{display:none!important}}</style>"
    if "</head>" in html:
        return html.replace("</head>", style_tag + "</head>", 1)
    return style_tag + html


_CSP_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_CSP_CONTENT_ATTR_RE = re.compile(r'content=(["\'])(.*?)\1', re.IGNORECASE | re.DOTALL)


def _relax_csp_for_editing(html: str) -> str:
    """A saved Claude-artifact export often carries its own sandbox CSP
    (connect-src 'none', from Claude's own artifact isolation) - harmless for
    a static file, but it silently blocks the autosave fetch() our injected
    script needs, client-side, with no server-visible error. Loosens just the
    connect-src directive to same-origin; the rest of the policy (no
    third-party scripts/frames/etc.) is left as-is."""
    def patch_tag(m):
        tag = m.group(0)
        low = tag.lower()
        if "http-equiv" not in low or "content-security-policy" not in low:
            return tag
        def patch_content(cm):
            quote, policy = cm.group(1), cm.group(2)
            if "connect-src" in policy:
                policy = re.sub(r"connect-src[^;]*", "connect-src 'self'", policy)
            else:
                policy = policy.rstrip("; ") + "; connect-src 'self'"
            return "content=" + quote + policy + quote
        return _CSP_CONTENT_ATTR_RE.sub(patch_content, tag, count=1)
    return _CSP_META_RE.sub(patch_tag, html)


def inject_edit_script(html: str) -> str:
    """Adds the contenteditable/autosave script just before </body> (or at
    the end, if the stored document is fragmentary)."""
    html = _relax_csp_for_editing(html)
    if "</body>" in html:
        return html.replace("</body>", _EDIT_SCRIPT + "</body>", 1)
    return html + _EDIT_SCRIPT


class ImportError_(Exception):
    """User-facing import failure - message is safe to show as-is."""


def is_claude_artifact_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc.endswith("claude.ai")
        and bool(_ARTIFACT_PATH_RE.match(parsed.path))
    )


class _ScriptJsonScanner(HTMLParser):
    """Pulls out any <script type="application/json"> blocks from the
    post-render DOM - best-effort structured-data capture, on top of the
    plain-text extraction below."""

    def __init__(self):
        super().__init__()
        self._capture = False
        self._buf: list[str] = []
        self.blobs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/json":
            self._capture = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._capture:
            self.blobs.append("".join(self._buf))
            self._capture = False

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)


def _extract_embedded_json(rendered_html: str) -> dict | None:
    scanner = _ScriptJsonScanner()
    scanner.feed(rendered_html)
    for blob in scanner.blobs:
        try:
            return json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


class _StructuredHTMLExtractor(HTMLParser):
    """Static-file counterpart to _TEXT_WALK_JS - a saved HTML deck has no
    live DOM to query, so this walks the raw markup with stdlib HTMLParser
    instead, but preserves the same structure markers (headings, pipe-delimited
    tables) so it feeds the draft prompt identically to the live-page path."""

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._heading_depth = 0
        self._heading_buf: list[str] = []
        self._table_depth = 0
        self._row: list[str] | None = None
        self._cell_buf: list[str] | None = None
        self._rows: list[str] = []
        self.text_parts: list[str] = []
        self.images: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "img":
            attrs_d = dict(attrs)
            self.images.append({"src": attrs_d.get("src", ""), "alt": attrs_d.get("alt", "")})
            return
        if re.match(r"^h[1-6]$", tag):
            self._heading_depth += 1
            self._heading_buf = []
            return
        if tag == "table":
            self._table_depth += 1
            self._rows = []
            return
        if self._table_depth and tag == "tr":
            self._row = []
            return
        if self._table_depth and tag in ("td", "th"):
            self._cell_buf = []

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if re.match(r"^h[1-6]$", tag) and self._heading_depth:
            self._heading_depth -= 1
            text = "".join(self._heading_buf).strip()
            if text:
                self.text_parts.append(f"\n\n## {text}\n")
            return
        if self._table_depth and tag in ("td", "th") and self._cell_buf is not None:
            if self._row is not None:
                self._row.append("".join(self._cell_buf).strip())
            self._cell_buf = None
            return
        if self._table_depth and tag == "tr" and self._row is not None:
            row_text = " | ".join(self._row)
            if row_text.strip(" |"):
                self._rows.append(row_text)
            self._row = None
            return
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._rows:
                self.text_parts.append("\n\n" + "\n".join(self._rows) + "\n")
                self._rows = []

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._heading_depth:
            self._heading_buf.append(data)
            return
        if self._cell_buf is not None:
            self._cell_buf.append(data)
            return
        if self._table_depth:
            return  # whitespace between table tags, outside any cell
        text = data.strip()
        if text:
            self.text_parts.append(text)


def parse_html_upload(data: bytes) -> tuple[dict, list[dict]]:
    """Parse a locally-uploaded .html/.htm file - same structured extraction
    (headings/tables/images) as fetch_artifact() below, minus the browser:
    a saved file's markup is already the final rendered HTML, so no
    JS-hydration step is needed. Raises ValueError (caught the same way as
    the other app/input_parsing.py formats) if there's nothing usable."""
    html_str = data.decode("utf-8", errors="replace")
    extractor = _StructuredHTMLExtractor()
    extractor.feed(html_str)

    text = "\n".join(extractor.text_parts).strip()
    if len(text) < MIN_TEXT_CHARS:
        raise ValueError("Couldn't extract usable content from that HTML file - it may be empty.")

    raw_data = {"business_name": "", "goals": "", "key_data_points": "", "notes": text}
    embedded = _extract_embedded_json(html_str)
    if embedded is not None:
        raw_data["embedded_data"] = embedded

    images = []
    for img in extractor.images:
        parsed = parse_data_uri(img.get("src") or "")
        if parsed:
            img_bytes, content_type = parsed
            images.append({"data": img_bytes, "content_type": content_type, "alt": img.get("alt", "")})
    return raw_data, images


def parse_data_uri(src: str) -> tuple[bytes, str] | None:
    """"data:image/png;base64,AAAA..." -> (raw_bytes, "image/png"). None for
    anything that isn't a base64 image data URI (a normal http(s) <img> src,
    for instance - those aren't ours to re-host)."""
    if not src.startswith("data:image/"):
        return None
    header, _, payload = src.partition(",")
    if ";base64" not in header:
        return None
    content_type = header[len("data:"):].split(";")[0]
    try:
        return base64.b64decode(payload), content_type
    except (ValueError, base64.binascii.Error):
        return None


def _build_raw_data(url: str, status: int, final_url: str, visible_text: str, rendered_html: str) -> dict:
    """Pure decision logic (no browser/network calls) so this is testable on
    its own: turns a rendered page's outcome into either raw_data or a
    user-facing ImportError_."""
    if status in (401, 403):
        raise ImportError_(
            "This artifact isn't publicly shared - ask the owner to enable sharing, "
            "or paste the content directly instead."
        )
    if status != 200:
        raise ImportError_(f"Couldn't fetch that URL (HTTP {status}).")
    if urlparse(final_url).path.startswith("/login"):
        raise ImportError_(
            "This artifact isn't publicly shared - ask the owner to enable sharing, "
            "or paste the content directly instead."
        )

    text = visible_text.strip()
    if len(text) < MIN_TEXT_CHARS:
        raise ImportError_(
            "Couldn't extract usable content from that artifact - it may be empty or broken. "
            "Try the form or file upload instead."
        )

    raw_data = {"business_name": "", "goals": "", "key_data_points": "", "notes": text, "source_url": url}
    embedded = _extract_embedded_json(rendered_html)
    if embedded is not None:
        raw_data["embedded_data"] = embedded
    return raw_data


def _extract_all_frames(page) -> tuple[str, str, list[dict], str]:
    """Claude renders the actual artifact content inside a sandboxed iframe
    on a separate origin (*.claudeusercontent.com), isolated from the
    claude.ai chrome around it - reading only the main frame gets you the
    title bar and a "Sign in" prompt, never the artifact itself. Concatenate
    every frame instead of guessing which one is "the" content frame, so
    this doesn't depend on Claude's exact iframe/domain naming.

    Also returns best_frame_html: the single frame's own document (not
    concatenated - concatenating multiple full <html> documents isn't valid
    markup) with the most extracted text, as the exact-clone source for the
    raw-HTML edit-in-place mode - the content frame reliably has far more
    text than the claude.ai chrome frame around it.
    """
    text_parts, html_parts, images = [], [], []
    best_text_len = -1
    best_frame_html = ""
    for frame in page.frames:
        frame_text = ""
        try:
            frame_text = frame.evaluate(_TEXT_WALK_JS).strip()
            if frame_text:
                text_parts.append(frame_text)
        except Exception:
            pass
        frame_html = ""
        try:
            frame_html = frame.content()
            html_parts.append(frame_html)
        except Exception:
            pass
        if len(frame_text) > best_text_len:
            best_text_len = len(frame_text)
            best_frame_html = frame_html
        try:
            for img in frame.evaluate(_IMAGES_JS):
                parsed = parse_data_uri(img.get("src") or "")
                if parsed:
                    data, content_type = parsed
                    images.append({"data": data, "content_type": content_type, "alt": img.get("alt", "")})
        except Exception:
            pass
    return "\n\n".join(text_parts), "\n".join(html_parts), images, best_frame_html


def upload_images(db, images: list[dict]) -> list[dict]:
    """Uploads real, already-extracted image bytes to the deliverable-assets
    Storage bucket and returns [{"image_ref": path, "context": alt}, ...] -
    the same already-uploaded-path shape the manual editor's image_block
    field expects (see TECHNICAL.md: never fabricate an image_ref). Dedupes
    identical bytes (a deck logo repeated on every slide) and caps at
    MAX_IMAGES. Best-effort: a failed upload just means one fewer image
    option, not a failed import.
    """
    if not images:
        return []
    bucket = db.storage.from_("deliverable-assets")
    prefix = f"imports/{uuid.uuid4().hex[:10]}"
    seen: set[str] = set()
    uploaded = []
    for i, img in enumerate(images):
        if len(uploaded) >= MAX_IMAGES:
            break
        digest = hashlib.sha1(img["data"]).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        ext = (img["content_type"].split("/")[-1].split("+")[0] or "png")[:5]
        path = f"{prefix}/img_{i}.{ext}"
        try:
            bucket.upload(path, img["data"], {"content-type": img["content_type"]})
        except Exception:
            continue
        uploaded.append({"image_ref": path, "context": (img.get("alt") or "")[:200]})
    return uploaded


def fetch_artifact(url: str) -> tuple[dict, list[dict], str]:
    """Render a public Claude artifact URL in a headless browser and extract
    it into the same raw_data shape the manual input form produces, plus any
    image assets actually found in the rendered page (raw bytes, not yet
    uploaded - the caller uploads them via upload_images() once it has a db
    handle, since raw_data itself has to stay JSON-serializable for the
    inputs table), plus the content frame's own HTML for the exact-clone
    edit-in-place mode. Raises ImportError_ (safe to show to the user) on
    anything that shouldn't be persisted as an input.
    """
    if not is_claude_artifact_url(url):
        raise ImportError_("That doesn't look like a claude.ai artifact link.")

    try:
        with sync_playwright() as pw:
            # ponytail: launches a fresh Chromium per import rather than a
            # pooled/shared browser - this is an occasional manual action,
            # not a hot path. Pool it if that stops being true.
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(user_agent=_DESKTOP_USER_AGENT)
                # "networkidle" waits for 500ms of total network silence, which
                # a page holding a websocket/analytics/polling connection open
                # (claude.ai does) never reaches - it just hangs to the
                # timeout. "load" fires once and reliably; the fixed settle
                # wait after it covers client-side render (and the sandboxed
                # content iframe loading) finishing.
                # ponytail: fixed settle delay rather than watching the DOM
                # for mutations to actually stop - good enough for a manual
                # one-off import; swap for a mutation-quiet wait if
                # slower-hydrating artifacts start coming back empty.
                response = page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
                page.wait_for_timeout(SETTLE_MS)
                status = response.status if response else 0
                final_url = page.url
                visible_text, rendered_html, images, best_frame_html = _extract_all_frames(page)
            finally:
                browser.close()
    except PlaywrightError as e:
        raise ImportError_(f"Couldn't reach that URL: {e}") from e

    raw_data = _build_raw_data(url, status, final_url, visible_text, rendered_html)
    return raw_data, images, best_frame_html
