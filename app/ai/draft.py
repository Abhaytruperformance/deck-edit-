"""Turns a project's raw input into a validated Artifact block list via an LLM,
called through OpenRouter (OpenAI-compatible API) rather than a provider SDK
directly - lets the model be swapped per-deployment via OPENROUTER_MODEL
without touching this file (e.g. moving from Claude to gpt-4o-mini later).

Invariant (see TECHNICAL.md Phase 2): never return blocks that fail validation.
Caller is responsible for not persisting anything until this succeeds.
"""
import json

from openai import APIError, OpenAI
from pydantic import ValidationError

from app.config import settings
from app.models.blocks import ArtifactBlocks

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM_PROMPT = """You are drafting slide/report content for a client deliverable.

Return ONLY a JSON array of "blocks" - no prose, no markdown code fences, no explanation.
Each block has this exact shape:
  { "id": "<short stable slug, e.g. sec_01>", "type": "<block type>", "enabled": true, "content": { ... } }

Supported block types and their content shape:
- title_slide: { "headline": str, "subhead": str }
- text_block: { "heading": str, "body": str }
- kpi_grid: { "title": str, "items": [{ "label": str, "value": number, "unit": str, "change_pct": number|null }] }
- bullet_list: { "heading": str, "items": [str] }
- image_block: { "caption": str, "image_ref": str }
- comparison_table: { "headers": [str], "rows": [[str]] }
- chart: { "chart_type": "bar"|"line"|"pie", "title": str, "labels": [str], "series": [{ "name": str, "values": [number] }] }

CRITICAL: Never invent or fabricate an "image_ref" value. Only emit an image_block if the
user's input explicitly supplied an already-uploaded image reference (a Supabase Storage path).
If the input JSON includes an "available_images" list, each entry's "image_ref" is a real,
already-uploaded path you may use directly in an image_block - match it to content using that
entry's "context" field. If no such reference was supplied, omit image_block entirely - do not
guess a path or filename.

When the input is source material to restructure rather than a brief to draft from (long-form
text, possibly with "## Heading" markers or pipe-delimited "|" table rows preserved from the
original layout), don't flatten it into one long text_block. Instead:
- Turn any standalone numeric callouts ("70% reduction", "95% of pilots fail") into a kpi_grid,
  one item per stat, with the surrounding sentence as its label.
- Turn pipe-delimited or grid-like rows into a comparison_table with real headers and rows.
- Use "## Heading" markers as natural boundaries between title_slide/text_block/bullet_list
  blocks - don't merge multiple sections into one block just because they were adjacent.

Order the blocks the way they should appear in the final deliverable. Output the raw JSON array
and nothing else."""


class DraftError(Exception):
    pass


def _extract_json_array(text: str) -> list:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())


def _call_model(client: OpenAI, user_prompt: str) -> str:
    try:
        response = client.chat.completions.create(
            model=settings.openrouter_model,
            max_tokens=16000,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
    except APIError as e:
        # Billing/rate-limit/network failures, not a bad-output retry case -
        # same fail-closed invariant (never persist), but a clean error
        # instead of an unhandled 500.
        raise DraftError(f"AI drafting service call failed: {e}") from e
    return response.choices[0].message.content or ""


def generate_draft(raw_input: dict, source_type: str = "form") -> list[dict]:
    """Call the drafting model, validate, retry once on failure. Raises DraftError on second failure.

    source_type only changes the framing line of the prompt: form/file_upload
    input is a brief to draft from; a Claude artifact import or an uploaded
    HTML file is existing client material to restructure - same schema, same
    validate/retry below.
    """
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=settings.openrouter_api_key)
    intro = (
        "Existing client material to restructure into the block schema below - reorganize and "
        "summarize it, don't invent new content, but don't flatten it into one block either "
        "(see the KPI/table/heading guidance above):"
        if source_type in ("claude_artifact_url", "html_upload")
        else "Client input (JSON):"
    )
    user_prompt = f"{intro}\n{json.dumps(raw_input, indent=2)}\n\nGenerate the blocks array now."

    text = _call_model(client, user_prompt)
    try:
        parsed = _extract_json_array(text)
        validated = ArtifactBlocks(blocks=parsed)
        return [b.model_dump() for b in validated.blocks]
    except (json.JSONDecodeError, ValidationError) as first_error:
        retry_prompt = (
            f"{user_prompt}\n\nYour previous response failed validation with this error:\n"
            f"{first_error}\n\nFix it and return ONLY the corrected JSON array."
        )
        text = _call_model(client, retry_prompt)
        try:
            parsed = _extract_json_array(text)
            validated = ArtifactBlocks(blocks=parsed)
            return [b.model_dump() for b in validated.blocks]
        except (json.JSONDecodeError, ValidationError) as second_error:
            raise DraftError(f"AI draft failed validation twice: {second_error}") from second_error
