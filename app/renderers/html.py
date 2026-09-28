"""Animated HTML renderer: same Artifact Contract blocks -> one self-contained
page. GSAP/ScrollTrigger + Chart.js load from CDN; everything else is inline
since this is served publicly with no build step.
"""
import json

from jinja2 import Environment, FileSystemLoader

from app.db import get_supabase

_env = Environment(loader=FileSystemLoader("app/templates/render_html"), autoescape=True)
_ASSET_BUCKET = "deliverable-assets"


def render(blocks: list[dict]) -> str:
    render_blocks = []
    for block in blocks:
        if not block.get("enabled", True):
            continue
        entry = dict(block)
        if entry["type"] == "chart":
            entry["_chart_json"] = json.dumps(entry["content"])
        elif entry["type"] == "image_block":
            try:
                entry["_image_url"] = get_supabase().storage.from_(_ASSET_BUCKET).get_public_url(
                    entry["content"]["image_ref"]
                )
            except Exception:
                entry["_image_url"] = None
        render_blocks.append(entry)
    template = _env.get_template("page.html")
    return template.render(blocks=render_blocks)
