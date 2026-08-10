from __future__ import annotations

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

    def clean(self, description: str, prompt: str, *, timeout: int = 120) -> str:
        if not description.strip():
            return ""
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
            raise DescriptionCleaningError(
                f"Description could not be cleaned with {self.model}: {exc}"
            ) from exc
