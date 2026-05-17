"""
Cloud LLM wrapper for the Second Brain MVP.

Uses the OpenAI Python SDK. OPENAI_BASE_URL is optional, so the same code
works against OpenAI itself or any OpenAI-compatible endpoint
(OpenRouter, Together, Groq, Azure-compatible gateways, etc.).

No Ollama / no local LLM.

Conversation history note:
  Both call functions take an optional `conversation_history` argument
  (list of {role, content} dicts or Pydantic models). When present, a
  short formatted block is prepended to the user-turn message so the
  model can resolve references like "he" / "that decision" using prior
  turns. The history block is INLINED into the single user message
  (not added as separate role=assistant messages in the messages array)
  for two reasons:
    1. Some OpenAI-compatible providers behave oddly when an assistant
       message in the messages array contains [N]-style citations from a
       prior retrieval — the model sometimes tries to honor those against
       the new context.
    2. The inlined preamble explicitly tells the model "use this only to
       resolve references, do not cite from it", which is harder to
       convey via the role structure alone.
"""

import os
from typing import Any, List, Optional

from openai import APITimeoutError, OpenAI

from errors import LLMError, UpstreamTimeoutError
from prompts import (
    INSUFFICIENT_CONTEXT_ANSWER,
    format_conversation_history,
    system_prompt_for_mode,
)


def _build_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        # Startup validation should have caught this, but be defensive.
        raise LLMError(log_context="OPENAI_API_KEY missing at call time")

    # Treat an empty OPENAI_BASE_URL the same as "not set" so we fall back
    # to the SDK default (https://api.openai.com/v1).
    base_url = os.getenv("OPENAI_BASE_URL") or None
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url)
    return OpenAI(api_key=api_key)


def _build_user_message(
    question: str,
    context: str,
    conversation_history: Optional[List[Any]],
) -> str:
    """
    Compose the single user-turn message: optional history preamble +
    current question + numbered context snippets + closing instruction.
    """
    history_block = format_conversation_history(conversation_history or [])
    return (
        f"{history_block}"
        f"Question:\n{question}\n\n"
        f"Context snippets:\n{context}\n\n"
        f"Answer the question using only the context above. "
        f"Cite sources as [1], [2], etc."
    )


def _max_tokens_from_env(default: int = 500) -> int:
    try:
        return int(os.getenv("LLM_MAX_TOKENS", str(default)))
    except ValueError:
        return default


def generate_grounded_answer(
    question: str,
    context: str,
    mode: str = "default",
    model: Optional[str] = None,
    conversation_history: Optional[List[Any]] = None,
) -> str:
    """
    Send the question + numbered context snippets to the cloud LLM and
    return the answer string.

    Args:
        question:              latest user question (the one being answered).
        context:               numbered "[N] (source: ...)" snippets.
        mode:                  retrieval/answer mode; selects system prompt.
        model:                 override OPENAI_MODEL.
        conversation_history:  optional list of recent {role, content}
                               turns. Used to resolve references in the
                               current question. Does NOT affect retrieval.

    Raises:
        UpstreamTimeoutError  on SDK-reported timeout.
        LLMError              on any other SDK / network failure.
    """
    model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    max_tokens = _max_tokens_from_env()

    if not context.strip():
        return INSUFFICIENT_CONTEXT_ANSWER

    user_message = _build_user_message(question, context, conversation_history)
    system_prompt = system_prompt_for_mode(mode)

    try:
        client = _build_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            temperature=0.1,
            max_tokens=max_tokens,
        )
    except APITimeoutError as e:
        raise UpstreamTimeoutError(
            log_context=f"LLM timeout: {type(e).__name__}"
        )
    except Exception as e:  # noqa: BLE001 -- surface any SDK error to the API
        # Log only the exception class name — never the API key, prompt,
        # or context. The user-facing detail is generic by design.
        raise LLMError(log_context=f"LLM call failed: {type(e).__name__}")

    choices = response.choices
    if not choices:
        return INSUFFICIENT_CONTEXT_ANSWER

    text = (choices[0].message.content or "").strip()
    return text or INSUFFICIENT_CONTEXT_ANSWER


def stream_grounded_answer(
    question: str,
    context: str,
    mode: str = "default",
    model: Optional[str] = None,
    conversation_history: Optional[List[Any]] = None,
):
    """
    Yield token chunks from the LLM as they arrive.

    Same prompts / temperature / max_tokens as generate_grounded_answer,
    just with stream=True. Yields strings (deltas). Caller is responsible
    for concatenating them.

    Args:
        See generate_grounded_answer.

    Raises:
        UpstreamTimeoutError on SDK timeout.
        LLMError on any other SDK / network failure.
    """
    model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    max_tokens = _max_tokens_from_env()

    if not context.strip():
        yield INSUFFICIENT_CONTEXT_ANSWER
        return

    user_message = _build_user_message(question, context, conversation_history)
    system_prompt = system_prompt_for_mode(mode)

    try:
        client = _build_client()
        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            temperature=0.1,
            max_tokens=max_tokens,
            stream=True,
        )
    except APITimeoutError as e:
        raise UpstreamTimeoutError(
            log_context=f"LLM stream timeout: {type(e).__name__}"
        )
    except Exception as e:  # noqa: BLE001
        raise LLMError(log_context=f"LLM stream open failed: {type(e).__name__}")

    try:
        for event in stream:
            # OpenAI SDK shape: event.choices[0].delta.content (may be None).
            if not event.choices:
                continue
            delta = event.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece
    except APITimeoutError as e:
        raise UpstreamTimeoutError(
            log_context=f"LLM stream timeout: {type(e).__name__}"
        )
    except Exception as e:  # noqa: BLE001
        raise LLMError(log_context=f"LLM stream iter failed: {type(e).__name__}")