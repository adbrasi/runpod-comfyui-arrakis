#!/usr/bin/env python3
"""Run a smoke test against a Runpod load-balancing endpoint."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_base_url(endpoint_id: str | None, base_url: str | None) -> str:
    if endpoint_id:
        return f"https://{endpoint_id}.api.runpod.ai"
    if base_url:
        return base_url.rstrip("/")
    raise RuntimeError("provide either --endpoint-id or --base-url")


def build_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def wait_for_ping(base_url: str, headers: dict[str, str], retries: int, delay_s: int) -> None:
    for attempt in range(1, retries + 1):
        response = requests.get(f"{base_url}/ping", headers=headers, timeout=30)
        if response.status_code == 200:
            return
        if response.status_code != 204:
            response.raise_for_status()
        print(f"[smoke-test-lb] ping attempt {attempt}/{retries}: status={response.status_code}")
        if attempt < retries:
            time.sleep(delay_s)
    raise RuntimeError("load-balancing endpoint did not become healthy in time")


def first_inline_image(response_payload: dict[str, Any]) -> tuple[bytes, str] | None:
    output = response_payload.get("output", {})
    images: list[dict[str, Any]] = []

    if isinstance(output, dict):
        if isinstance(output.get("outputs"), list):
            for item in output["outputs"]:
                if item.get("type") == "image" and item.get("data"):
                    images.append(item)
        if isinstance(output.get("images"), list):
            for item in output["images"]:
                if item.get("data"):
                    images.append(item)

    if not images:
        return None

    image = images[0]
    return base64.b64decode(image["data"]), image.get("filename") or "output.bin"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint-id", help="Runpod load-balancing endpoint ID")
    parser.add_argument("--base-url", help="Base URL for the load-balancing endpoint")
    parser.add_argument("--workflow", required=True, help="Path to workflow JSON")
    parser.add_argument("--overrides-json", help="Path to overrides JSON array")
    parser.add_argument("--output-mode", default="inline", choices=["auto", "inline", "base64", "object_store", "s3"])
    parser.add_argument("--ping-retries", type=int, default=30)
    parser.add_argument("--ping-delay-s", type=int, default=5)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--save-response", help="Path to save the raw JSON response")
    parser.add_argument("--save-image", help="Path to save the first inline image")
    args = parser.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY")
    base_url = build_base_url(args.endpoint_id, args.base_url)
    headers = build_headers(api_key)
    workflow = load_json(Path(args.workflow))
    overrides = load_json(Path(args.overrides_json)) if args.overrides_json else []

    wait_for_ping(base_url, headers, args.ping_retries, args.ping_delay_s)

    payload = {
        "input": {
            "workflow": workflow,
            "overrides": overrides,
            "output_mode": args.output_mode,
        }
    }
    response = requests.post(f"{base_url}/generate", headers=headers, json=payload, timeout=args.timeout_s)
    response.raise_for_status()
    response_payload = response.json()
    print(json.dumps(response_payload, indent=2)[:12000])

    if args.save_response:
        response_path = Path(args.save_response)
        response_path.write_text(json.dumps(response_payload, indent=2), encoding="utf-8")
        print(f"[smoke-test-lb] saved response: {response_path}")

    if args.save_image:
        image_payload = first_inline_image(response_payload)
        if not image_payload:
            raise RuntimeError("response did not contain an inline image to save")
        image_bytes, filename = image_payload
        image_path = Path(args.save_image)
        image_path.write_bytes(image_bytes)
        print(f"[smoke-test-lb] saved image: {image_path} ({filename}, {len(image_bytes)} bytes)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[smoke-test-lb] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
