"""
Cloud LLM wrapper for the Second Brain MVP.

Uses the OpenAI Python SDK. OPENAI_BASE_URL is optional, so the same code
works against OpenAI itself or any OpenAI-compatible endpoint
(Azure-compatible gateways, Together, Groq, Anyscale, etc.).

No Ollama / no local LLM.
"""

import os
from typing import Optional

from openai import OpenAI


# The exact fallback string we want the LLM to use when the context can't
# answer the question. Mirrored both in the system prompt (so the model
# emits it) and as a defensive return value (so we emit it even if the
# LLM call short-circuits).
INSUFFICIENT_CONTEXT_ANSWER = "I do not have enough Slack context to answer that."


SYSTEM_PROMPT = f"""You are the Second Brain assistant. You answer questions about
the user's Slack workspace using ONLY the numbered context snippets provided
below the user's question.

Rules:
- Answer ONLY from the provided context. Do not use outside knowledge.
- If the context is insufficient or unrelated, reply EXACTLY with:
  {INSUFFICIENT_CONTEXT_ANSWER}
- Cite sources inline using bracketed numbers like [1], [2] that correspond
  to the numbered snippets you used. Cite every claim that comes from the
  context.
- Do not invent people, user IDs, dates, channels, decisions, or quotes
  that are not in the context.
- Be concise. Prefer short, direct answers over restating the context.
"""


def _build_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set.")

    # Treat an empty OPENAI_BASE_URL the same as "not set" so we fall back
    # to the SDK default (https://api.openai.com/v1).
    base_url = os.getenv("OPENAI_BASE_URL") or None
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url)
    return OpenAI(api_key=api_key)


def generate_grounded_answer(
    question: str,
    context: str,
    model: Optional[str] = None,
) -> str:
    """
    Send the question + numbered context snippets to the cloud LLM and
    return the answer string. The system prompt enforces the grounding
    rules; this function adds a defensive short-circuit for empty context.
    """
    model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not context.strip():
        return INSUFFICIENT_CONTEXT_ANSWER

    user_message = (
        f"Question:\n{question}\n\n"
        f"Context snippets:\n{context}\n\n"
        f"Answer the question using only the context above. "
        f"Cite sources as [1], [2], etc."
    )

    try:
        client = _build_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
        )
    except Exception as e:  # noqa: BLE001 -- surface any SDK error to the API
        print(f"[llm] LLM call failed: {e}")
        return f"LLM error: {e}"

    choices = response.choices
    if not choices:
        return INSUFFICIENT_CONTEXT_ANSWER

    text = (choices[0].message.content or "").strip()
    return text or INSUFFICIENT_CONTEXT_ANSWER