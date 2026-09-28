"""PPTX renderer: consumes the Artifact Contract block list, one handler per
block `type`. Reads only what the caller passes in - it's the caller's job
(app/routers/publish.py) to have already filtered to enabled blocks from the
published version, never the live draft.
"""
import io

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.util import Inches, Pt

from app.db import get_supabase

_BLANK_LAYOUT = 6
_TITLE_CONTENT_LAYOUT = 1
_CHART_TYPES = {
    "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "line": XL_CHART_TYPE.LINE,
    "pie": XL_CHART_TYPE.PIE,
}
_ASSET_BUCKET = "deliverable-assets"


def _autofit(text_frame) -> None:
    """Shrink text to fit its box instead of overflowing the slide - client
    input length is unpredictable in a way curated demo content never is."""
    text_frame.word_wrap = True
    text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE


def _handle_title_slide(prs: Presentation, content: dict) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = content["headline"]
    _autofit(slide.shapes.title.text_frame)
    if len(slide.placeholders) > 1:
        slide.placeholders[1].text = content["subhead"]
        _autofit(slide.placeholders[1].text_frame)


def _handle_text_block(prs: Presentation, content: dict) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[_TITLE_CONTENT_LAYOUT])
    slide.shapes.title.text = content["heading"]
    _autofit(slide.shapes.title.text_frame)
    body_tf = slide.placeholders[1].text_frame
    body_tf.text = content["body"]
    _autofit(body_tf)


def _handle_bullet_list(prs: Presentation, content: dict) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[_TITLE_CONTENT_LAYOUT])
    slide.shapes.title.text = content["heading"]
    _autofit(slide.shapes.title.text_frame)
    tf = slide.placeholders[1].text_frame
    tf.clear()
    for i, item in enumerate(content["items"]):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = item
    _autofit(tf)


def _handle_kpi_grid(prs: Presentation, content: dict) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])
    title_tf = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(9), Inches(0.6)).text_frame
    title_tf.text = content["title"]
    _autofit(title_tf)

    items = content["items"]
    box_width = Inches(9) / max(len(items), 1)
    for i, item in enumerate(items):
        box = slide.shapes.add_textbox(Inches(0.5) + i * box_width, Inches(1.2), box_width, Inches(2))
        tf = box.text_frame
        tf.text = item["label"]
        p = tf.add_paragraph()
        p.text = f"{item['value']} {item['unit']}"
        p.font.size = Pt(24)
        if item.get("change_pct") is not None:
            p2 = tf.add_paragraph()
            p2.text = f"{item['change_pct']:+.1f}%"
        _autofit(tf)


def _handle_comparison_table(prs: Presentation, content: dict) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])
    headers, rows = content["headers"], content["rows"]
    n_rows, n_cols = len(rows) + 1, len(headers)
    table = slide.shapes.add_table(n_rows, n_cols, Inches(0.5), Inches(0.5), Inches(9), Inches(0.4 * n_rows)).table
    # Tables have no shrink-to-fit; word-wrapping each cell keeps long client
    # text inside its cell instead of spilling over neighbors.
    for c, header in enumerate(headers):
        cell = table.cell(0, c)
        cell.text = header
        cell.text_frame.word_wrap = True
    for r, row in enumerate(rows, start=1):
        for c, cell_text in enumerate(row):
            cell = table.cell(r, c)
            cell.text = cell_text
            cell.text_frame.word_wrap = True


def _handle_chart(prs: Presentation, content: dict) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])
    title_tf = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(9), Inches(0.5)).text_frame
    title_tf.text = content["title"]
    _autofit(title_tf)

    chart_data = CategoryChartData()
    chart_data.categories = content["labels"]
    for series in content["series"]:
        chart_data.add_series(series["name"], series["values"])

    chart_type = _CHART_TYPES[content["chart_type"]]
    slide.shapes.add_chart(chart_type, Inches(0.5), Inches(1), Inches(9), Inches(5), chart_data)


def _handle_image_block(prs: Presentation, content: dict) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])
    try:
        image_bytes = get_supabase().storage.from_(_ASSET_BUCKET).download(content["image_ref"])
        slide.shapes.add_picture(io.BytesIO(image_bytes), Inches(1), Inches(0.5), height=Inches(5))
    except Exception:
        # ponytail: no retry/asset-repair path - if storage lookup fails we still
        # want the slide to render with the caption. Revisit once image uploads
        # (Phase 7) make broken refs a real, more frequent failure mode.
        pass
    caption_tf = slide.shapes.add_textbox(Inches(1), Inches(5.7), Inches(8), Inches(0.5)).text_frame
    caption_tf.text = content["caption"]
    _autofit(caption_tf)


_HANDLERS = {
    "title_slide": _handle_title_slide,
    "text_block": _handle_text_block,
    "bullet_list": _handle_bullet_list,
    "kpi_grid": _handle_kpi_grid,
    "comparison_table": _handle_comparison_table,
    "chart": _handle_chart,
    "image_block": _handle_image_block,
}


def render(blocks: list[dict]) -> bytes:
    prs = Presentation()
    for block in blocks:
        if not block.get("enabled", True):
            continue
        _HANDLERS[block["type"]](prs, block["content"])

    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue()
