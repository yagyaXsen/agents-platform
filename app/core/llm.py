"""Minimal client for any OpenAI-compatible chat API (OpenAI, Groq, Gemini, local).

Uses only the standard library. Configure with LLM_BASE_URL, LLM_API_KEY and
LLM_MODEL. Without a key the agent still works: answers are extracted from
the knowledge base instead of generated.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

NO_ANSWER = "NO_ANSWER"

SYSTEM_PROMPT = """You are the customer assistant for {business}.
Answer ONLY using the context below. Keep it to 2-3 short sentences, friendly and plain.
If the context does not contain the answer, reply with exactly: NO_ANSWER
Never invent prices, dates, medical, legal or financial advice. Do not mention "the context".

Context:
{context}"""


class LLMError(Exception):
    pass


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float = 20.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


def chat(cfg: LLMConfig, messages: list[dict], max_tokens: int = 300, temperature: float = 0.2) -> tuple[str, int]:
    """Returns (text, total_tokens). Raises LLMError on any failure."""
    body = json.dumps(
        {"model": cfg.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    ).encode()
    req = urllib.request.Request(
        f"{cfg.base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
            # some providers sit behind bot protection that rejects Python's default agent
            "User-Agent": "agentgrid-poc/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read(300).decode(errors="replace").replace("\n", " ")
        raise LLMError(f"HTTP {e.code} from the LLM provider: {detail}") from e
    except (OSError, ValueError) as e:  # network errors, timeouts, resets, bad JSON
        raise LLMError(f"{type(e).__name__}: {e}") from e
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError, TypeError) as e:
        raise LLMError("unexpected response shape") from e
    tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
    return text, tokens


def grounded_answer(cfg: LLMConfig, business: str, question: str, passages: list[tuple[str, str]]) -> tuple[str | None, int]:
    """Answer from the given (title, text) passages. Returns (None, tokens) when the
    model says the answer isn't there."""
    context = "\n\n".join(f"[{title}]\n{text}" for title, text in passages)
    text, used = chat(
        cfg,
        [
            {"role": "system", "content": SYSTEM_PROMPT.format(business=business, context=context)},
            {"role": "user", "content": question},
        ],
    )
    if not text or NO_ANSWER in text:
        return None, used
    return text, used
