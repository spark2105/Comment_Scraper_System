from __future__ import annotations

import sys

import ollama


OLLAMA_MODEL = "llama3.2"


def main() -> int:
    host = "http://127.0.0.1:11434"
    model = OLLAMA_MODEL
    client = ollama.Client(host=host)

    try:
        client.show(model)
        print(f"{model} is already available in the local Ollama installation.", flush=True)
        return 0
    except Exception:
        print(f"{model} is missing - installing it into the local Ollama installation.", flush=True)

    try:
        client.pull(model)
    except Exception as exc:
        print(f"Installation of {model} failed: {exc}", file=sys.stderr, flush=True)
        return 1

    print(f"{model} was installed successfully.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
