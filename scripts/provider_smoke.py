"""Run one provider request and print only non-sensitive runtime metadata."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _secret(provider: str) -> str | None:
    name = "DEEPSEEK_API_KEY" if provider == "deepseek" else "GLM_API_KEY"
    value = os.environ.get(name)
    path = os.environ.get(f"{name}_FILE")
    if path:
        value = Path(path).read_text(encoding="utf-8").strip()
    return value.strip() if value else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("deepseek", "glm"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    request_id = None
    tokens = {"input": None, "output": None}
    status = "failed"
    try:
        key = _secret(args.provider)
        if not key:
            raise ValueError("provider key is not configured")
        body = json.dumps({
            "model": args.model,
            "messages": [{"role": "user", "content": 'Return JSON: {"ok":true}'}],
            "max_tokens": 16,
        }).encode("utf-8")
        request = urllib.request.Request(
            args.url,
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            request_id = response.headers.get("x-request-id")
            payload = json.loads(response.read())
        usage = payload.get("usage", {})
        tokens = {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens")}
        request_id = request_id or payload.get("id")
        status = "ok"
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.HTTPError):
        pass
    result = {
        "Provider": args.provider,
        "model": args.model,
        "request_id": request_id,
        "tokens": tokens,
        "latency": int((time.perf_counter() - started) * 1000),
        "status": status,
    }
    print(json.dumps(result, separators=(",", ":")))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
