"""
Cloud LLM wrapper for the Second Brain MVP.

Uses the OpenAI Python SDK. OPENAI_BASE_URL is optional, so the same code
works against OpenAI itself or any OpenAI-compatible endpoint
(OpenRouter, Together, Groq, Azure-compatible gateways, etc.).

No Ollama / no local LLM.
"""

import os
from typing import Optional

from openai import APITimeoutError, OpenAI

from errors import LLMError, UpstreamTimeoutError
from prompts import INSUFFICIENT_CONTEXT_ANSWER, system_prompt_for_mode


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


def generate_grounded_answer(
    question: str,
    context: str,
    mode: str = "default",
    model: Optional[str] = None,
) -> str:
    """
    Send the question + numbered context snippets to the cloud LLM and
    return the answer string.

    Raises:
        UpstreamTimeoutError  on SDK-reported timeout.
        LLMError              on any other SDK / network failure.

    The system prompt enforces the grounding rules; this function adds a
    defensive short-circuit for empty context that avoids burning a call.
    """
    model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # LLM_MAX_TOKENS=500 by default. Caps output length so demo responses
    # stay tight and we don't burn unexpected tokens.
    try:
        max_tokens = int(os.getenv("LLM_MAX_TOKENS", "500"))
    except ValueError:
        max_tokens = 500

    if not context.strip():
        return INSUFFICIENT_CONTEXT_ANSWER

    user_message = (
        f"Question:\n{question}\n\n"
        f"Context snippets:\n{context}\n\n"
        f"Answer the question using only the context above. "
        f"Cite sources as [1], [2], etc."
    )

    system_prompt = system_prompt_for_mode(mode)

    try:
        client = _build_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
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