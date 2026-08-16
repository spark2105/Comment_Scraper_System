from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


OLLAMA_MODEL = "llama3.2"

class DescriptionCleaningError(RuntimeError):
    """Raised when Ollama cannot clean a description."""


class DescriptionCleaner:
    def __init__(self, *, host: str | None = None, model: str | None = None) -> None:
        self.host = host or "http://127.0.0.1:11434"
        self.model = model or OLLAMA_MODEL
        try:
            import ollama
        except ImportError as exc:  # pragma: no cover - handled by the local environment
            raise DescriptionCleaningError(
                "The Ollama Python library is not installed."
            ) from exc
        self.client = ollama.Client(host=self.host)

    def clean(
        self,
        description: str,
        prompt: str,
        *,
        timeout: int = 120,
        retry_delay: int = 15,
        on_cuda_oom: Callable[[int, int], None] | None = None,
        should_continue: Callable[[], None] | None = None,
    ) -> str:
        if not description.strip():
            return ""
        attempt = 0
        while True:
            try:
                response: Any = self.client.chat(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": description},
                    ],
                    options={"temperature": 0.2},
                )
                message = response.get("message") if isinstance(response, dict) else getattr(response, "message", None)
                if isinstance(message, dict):
                    content = message.get("content")
                else:
                    content = getattr(message, "content", None)
                if not isinstance(content, str):
                    raise ValueError("Ollama returned no text content")
                cleaned = content.strip()
                if not cleaned:
                    raise ValueError("Ollama returned an empty cleaned description")
                return cleaned
            except Exception as exc:
                if not _is_cuda_out_of_memory(exc):
                    raise DescriptionCleaningError(
                        f"Description could not be cleaned with {self.model}: {exc}"
                    ) from exc

                attempt += 1
                if on_cuda_oom is not None:
                    on_cuda_oom(attempt, retry_delay)
                remaining = max(0, retry_delay)
                while remaining > 0:
                    if should_continue is not None:
                        should_continue()
                    time.sleep(min(1, remaining))
                    remaining -= 1


def _is_cuda_out_of_memory(error: BaseException) -> bool:
    message = str(error).lower()
    return "cuda" in message and "out of memory" in message
