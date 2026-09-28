"""Self-check for app/ai/draft.py's parse/validate/retry-once logic, mocking
the OpenRouter (OpenAI-compatible) client so no network/API key is needed.
Covers: markdown-fenced JSON extraction, first-try success, and the
retry-once-then-raise path required by TECHNICAL.md Phase 2's invariant that a
failed draft is never persisted.

Run: python tests/test_draft.py
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")

from openai import APIError  # noqa: E402

from app.ai.draft import DraftError, generate_draft  # noqa: E402

GOOD_BLOCKS = '[{"id": "s1", "type": "text_block", "enabled": true, "content": {"heading": "H", "body": "B"}}]'


def _fake_response(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_markdown_fenced_success():
    with patch("app.ai.draft.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = _fake_response(
            "```json\n" + GOOD_BLOCKS + "\n```"
        )
        blocks = generate_draft({"business_name": "Acme"})
        assert len(blocks) == 1 and blocks[0]["type"] == "text_block"


def test_retries_once_then_succeeds():
    with patch("app.ai.draft.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.side_effect = [
            _fake_response("not json at all"),
            _fake_response(GOOD_BLOCKS),
        ]
        blocks = generate_draft({"business_name": "Acme"})
        assert len(blocks) == 1
        assert MockOpenAI.return_value.chat.completions.create.call_count == 2


def test_fails_twice_raises_without_persisting():
    with patch("app.ai.draft.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.side_effect = [
            _fake_response("still not json"),
            _fake_response("nope"),
        ]
        try:
            generate_draft({"business_name": "Acme"})
            raise SystemExit("expected DraftError on second failure")
        except DraftError:
            pass


def test_api_error_surfaces_as_draft_error():
    """A real API failure (billing/rate-limit/network - hit for real during
    manual testing as an OpenRouter 402) must fail closed the same way a bad
    parse does, not crash with an unhandled 500."""
    api_error = APIError("Insufficient credits", request=object(), body=None)
    with patch("app.ai.draft.OpenAI") as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.side_effect = api_error
        try:
            generate_draft({"business_name": "Acme"})
            raise SystemExit("expected DraftError on API failure")
        except DraftError as e:
            assert "Insufficient credits" in str(e)


if __name__ == "__main__":
    test_markdown_fenced_success()
    test_retries_once_then_succeeds()
    test_fails_twice_raises_without_persisting()
    test_api_error_surfaces_as_draft_error()
    print("OK: draft parsing, retry-once, fail-closed-on-second-failure, and API-error fail-closed all hold")
