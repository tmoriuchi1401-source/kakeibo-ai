"""Bounded, synthetic-only availability probe; never reads receipt stores."""
from __future__ import annotations

import base64
import io
import json
import os
import time

from google import genai
from PIL import Image, ImageDraw, ImageFont

from .gemini_errors import gemini_api_status


MODELS = ("gemini-3.6-flash", "gemini-3.5-flash-lite")
SCHEMA = {
    "type": "object",
    "properties": {"total": {"type": "integer"}},
    "required": ["total"],
    "additionalProperties": False,
}


def create_probe_client(api_key, *, httpx_client=None):
    options = {
        "api_version": "v1", "timeout": 90000,
        "retry_options": {"attempts": 0},
    }
    if httpx_client is not None:
        options["httpx_client"] = httpx_client
    client = genai.Client(api_key=api_key, http_options=options)
    # Version-bound workaround, covered through the real HTTP transport:
    # google-genai 2.24's legacy retry_args mutates attempts=0 to 1 before
    # Interactions translates it as a *retry* count (two wire requests).
    # Set the generated resource's retry count after that normalization.
    # The diagnostic workflow pins 2.24.0; this does not alter production.
    client.interactions.sdk_configuration.retry_config.max_retries = 0
    return client


def synthetic_image() -> bytes:
    image = Image.new("RGB", (720, 400), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)
    draw.multiline_text(
        (30, 30),
        "SYNTHETIC TEST RECEIPT\nItem A  100\nItem B  200\nTOTAL   300",
        fill="black", font=font, spacing=18,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def run_probe(client, *, sleep=time.sleep, emit=print, clock=time.monotonic):
    """At most four requests, no SDK replay, and stop on auth/quota errors.

    Only fixed labels, status integers, and timing/validation metadata escape.
    Neither API error messages nor model responses are logged.
    """
    encoded = base64.b64encode(synthetic_image()).decode("ascii")
    results = []
    for model in MODELS:
        for kind in ("text", "image"):
            if results:
                sleep(15)
            content = [{"type": "text", "text": (
                "Extract the printed TOTAL from this synthetic receipt."
                if kind == "image" else
                "Synthetic receipt: Item A 100, Item B 200, TOTAL 300. Extract TOTAL."
            )}]
            if kind == "image":
                content.append({"type": "image", "mime_type": "image/png", "data": encoded})
            result = {"model": model, "input": kind}
            started = clock()
            try:
                response = client.interactions.create(
                    model=model, input=content,
                    response_format={"type": "text", "mime_type": "application/json", "schema": SCHEMA},
                )
                value = json.loads(response.output_text)
                valid = (isinstance(value, dict) and set(value) == {"total"}
                         and type(value["total"]) is int and value["total"] == 300)
                result.update(status=200, outcome="valid" if valid else "invalid_response")
            except (ValueError, TypeError, AttributeError):
                result.update(status=None, outcome="invalid_response")
            except Exception as error:
                status = gemini_api_status(error)
                result.update(status=status, outcome="api_error" if status else "transport_or_sdk_error")
            result["elapsed_seconds"] = round(max(0, clock() - started), 2)
            results.append(result)
            emit(json.dumps(result, sort_keys=True))
            if result["status"] in (401, 403, 429):
                return results
    return results


def main():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print('{"outcome":"missing_key"}')
        return 1
    # A single call is bounded to 90 seconds; the workflow bounds the whole job.
    try:
        client = create_probe_client(key)
        try:
            results = run_probe(client)
        finally:
            client.close()
    except Exception:
        print('{"outcome":"probe_setup_failed"}')
        return 1
    return 0 if len(results) == 4 and all(r["outcome"] == "valid" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
