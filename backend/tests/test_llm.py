"""Tests for llm.py — OpenAI wrapper for grounded answers."""

import os
from unittest.mock import MagicMock, patch

import pytest


# ── Helper to build a fake OpenAI response ────────────────────────────────
def _fake_completion(content: str):
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _fake_stream_events(parts):
    events = []
    for part in parts:
        delta = MagicMock()
        delta.content = part
        choice = MagicMock()
        choice.delta = delta
        event = MagicMock()
        event.choices = [choice]
        events.append(event)
    return events


# ── generate_grounded_answer ───────────────────────────────────────────────
class TestGenerateGroundedAnswer:
    def _call(self, question="What's the plan?", context="[1] The plan is X.", mode="default", history=None):
        from llm import generate_grounded_answer

        return generate_grounded_answer(question, context, mode=mode, conversation_history=history)

    def test_empty_context_returns_fallback(self):
        from llm import generate_grounded_answer
        from prompts import INSUFFICIENT_CONTEXT_ANSWER

        result = generate_grounded_answer("q?", "", mode="default")
        assert result == INSUFFICIENT_CONTEXT_ANSWER

    def test_whitespace_context_returns_fallback(self):
        from llm import generate_grounded_answer
        from prompts import INSUFFICIENT_CONTEXT_ANSWER

        result = generate_grounded_answer("q?", "   ", mode="default")
        assert result == INSUFFICIENT_CONTEXT_ANSWER

    def test_normal_call_returns_answer(self):
        fake_resp = _fake_completion("The plan is X [1].")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = fake_resp

        with patch("llm._build_client", return_value=mock_client):
            result = self._call()
        assert result == "The plan is X [1]."

    def test_empty_choices_returns_fallback(self):
        from prompts import INSUFFICIENT_CONTEXT_ANSWER

        fake_resp = MagicMock()
        fake_resp.choices = []
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = fake_resp

        with patch("llm._build_client", return_value=mock_client):
            result = self._call()
        assert result == INSUFFICIENT_CONTEXT_ANSWER

    def test_none_content_returns_fallback(self):
        from prompts import INSUFFICIENT_CONTEXT_ANSWER

        choice = MagicMock()
        choice.message.content = None
        fake_resp = MagicMock()
        fake_resp.choices = [choice]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = fake_resp

        with patch("llm._build_client", return_value=mock_client):
            result = self._call()
        assert result == INSUFFICIENT_CONTEXT_ANSWER

    def test_timeout_raises_upstream_timeout(self):
        from openai import APITimeoutError

        from errors import UpstreamTimeoutError

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = APITimeoutError(request=MagicMock())
        with patch("llm._build_client", return_value=mock_client):
            with pytest.raises(UpstreamTimeoutError):
                self._call()

    def test_generic_exception_raises_llm_error(self):
        from errors import LLMError

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("connection refused")
        with patch("llm._build_client", return_value=mock_client):
            with pytest.raises(LLMError):
                self._call()

    def test_model_env_var_used(self):
        fake_resp = _fake_completion("answer")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = fake_resp
        with patch("llm._build_client", return_value=mock_client):
            with patch.dict(os.environ, {"OPENAI_MODEL": "gpt-4o"}):
                self._call()
        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs[1]["model"] == "gpt-4o"

    def test_conversation_history_inlined(self):
        fake_resp = _fake_completion("answer")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = fake_resp
        history = [{"role": "user", "content": "previous question"}]
        with patch("llm._build_client", return_value=mock_client):
            self._call(history=history)
        call_kwargs = mock_client.chat.completions.create.call_args
        user_msg = call_kwargs[1]["messages"][1]["content"]
        assert "previous question" in user_msg


# ── stream_grounded_answer ────────────────────────────────────────────────
class TestStreamGroundedAnswer:
    def _stream(self, question="What's the plan?", context="[1] content", mode="default"):
        from llm import stream_grounded_answer

        return list(stream_grounded_answer(question, context, mode=mode))

    def test_empty_context_yields_fallback(self):
        from llm import stream_grounded_answer
        from prompts import INSUFFICIENT_CONTEXT_ANSWER

        parts = list(stream_grounded_answer("q?", ""))
        assert parts == [INSUFFICIENT_CONTEXT_ANSWER]

    def test_yields_tokens(self):
        events = _fake_stream_events(["Hello", " world", "!"])
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(events)
        with patch("llm._build_client", return_value=mock_client):
            parts = self._stream()
        assert parts == ["Hello", " world", "!"]

    def test_none_delta_skipped(self):
        events = _fake_stream_events(["part1", None, "part2"])
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = iter(events)
        with patch("llm._build_client", return_value=mock_client):
            parts = self._stream()
        assert "part1" in parts
        assert "part2" in parts
        assert None not in parts

    def test_timeout_during_stream_raises(self):
        from openai import APITimeoutError

        from errors import UpstreamTimeoutError

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = APITimeoutError(request=MagicMock())
        from llm import stream_grounded_answer

        with patch("llm._build_client", return_value=mock_client):
            with pytest.raises(UpstreamTimeoutError):
                list(stream_grounded_answer("q?", "[1] context"))

    def test_stream_iter_error_raises_llm_error(self):
        from errors import LLMError

        def _bad_iter():
            yield "first"
            raise RuntimeError("network drop")

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _bad_iter()
        from llm import stream_grounded_answer

        with patch("llm._build_client", return_value=mock_client):
            with pytest.raises(LLMError):
                list(stream_grounded_answer("q?", "[1] context"))

    def test_missing_api_key_raises_llm_error(self):
        from errors import LLMError
        from llm import stream_grounded_answer

        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with pytest.raises(LLMError):
                list(stream_grounded_answer("q?", "[1] context"))


# ── _build_user_message ────────────────────────────────────────────────────
class TestBuildUserMessage:
    def _build(self, question, context, history=None):
        from llm import _build_user_message

        return _build_user_message(question, context, history)

    def test_contains_question(self):
        msg = self._build("My question?", "[1] some context")
        assert "My question?" in msg

    def test_contains_context(self):
        msg = self._build("q?", "[1] the answer")
        assert "[1] the answer" in msg

    def test_no_history_no_preamble(self):
        msg = self._build("q?", "ctx", history=[])
        assert "Recent conversation" not in msg

    def test_history_included(self):
        msg = self._build("q?", "ctx", history=[{"role": "user", "content": "prior question"}])
        assert "prior question" in msg
