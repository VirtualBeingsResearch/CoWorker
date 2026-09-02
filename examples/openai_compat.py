#!/usr/bin/env python3
"""Call the running Coworker as an OpenAI-compatible model.

Requires a communication token (primary API__COMMUNICATION_TOKEN or an extra
issued through openai:control). Extra tokens map to openai:{short_name}.

  python examples/openai_compat.py
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = os.getenv("COWORKER_API_BASE", "http://localhost:8000")
TOKEN = os.getenv("COWORKER_API_TOKEN", "")


def _request(path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE.rstrip("/") + path,
        data=data,
        method="GET" if payload is None else "POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        raise SystemExit(f"{error.code} {body}") from error


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set COWORKER_API_TOKEN to a communication token.")
    models = _request("/v1/models")
    print("models:", json.dumps(models, ensure_ascii=False, indent=2))
    completion = _request(
        "/v1/chat/completions",
        {
            "model": "coworker",
            "messages": [
                {"role": "user", "content": "用一句话介绍你自己。"},
            ],
        },
    )
    print("completion:", json.dumps(completion, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
