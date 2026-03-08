#!/usr/bin/env python3
"""Run a simple smoke test against a Runpod or local ComfyUI worker endpoint."""

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
        return f"https://api.runpod.ai/v2/{endpoint_id}"
    if base_url:
        return base_url.rstrip("/")
    raise RuntimeError("provide either --endpoint-id or --base-url")


def build_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def wait_for_health(base_url: str, headers: dict[str, str], retries: int, delay_s: int) -> None:
    health_url = f"{base_url}/health"
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(health_url, headers=headers, timeout=30)
            if response.status_code == 404:
                return
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or "workers" not in payload:
                return

            workers = payload.get("workers", {})
            if any(int(workers.get(key, 0)) > 0 for key in ("ready", "idle", "running")):
                return

            print(
                f"[smoke-test] health attempt {attempt}/{retries}: waiting for worker readiness "
                f"(workers={json.dumps(workers)})"
            )
        except Exception as exc:
            print(f"[smoke-test] health attempt {attempt}/{retries}: {exc}")

        if attempt < retries:
            time.sleep(delay_s)


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
    data = image.get("data")
    if not data:
        return None

    filename = image.get("filename") or "output.bin"
    return base64.b64decode(data), filename


def poll_job_status(
    base_url: str,
    headers: dict[str, str],
    job_id: str,
    timeout_s: int,
    poll_interval_s: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    status_url = f"{base_url}/status/{job_id}"

    while time.time() < deadline:
        response = requests.get(status_url, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
        status = payload.get("status")

        if status in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            return payload

        print(f"[smoke-test] job {job_id} status={status}; waiting {poll_interval_s}s")
        time.sleep(poll_interval_s)

    raise RuntimeError(f"timed out waiting for job completion: {job_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint-id", help="Runpod endpoint ID")
    parser.add_argument("--base-url", help="Base URL for a local or proxied worker API")
    parser.add_argument("--workflow", required=True, help="Path to workflow JSON")
    parser.add_argument("--overrides-json", help="Path to overrides JSON array")
    parser.add_argument("--output-mode", default="inline", choices=["auto", "inline", "base64", "object_store", "s3"])
    parser.add_argument("--health-retries", type=int, default=30)
    parser.add_argument("--health-delay-s", type=int, default=10)
    parser.add_argument("--runsync-timeout-s", type=int, default=1800)
    parser.add_argument("--status-poll-interval-s", type=int, default=10)
    parser.add_argument("--save-response", help="Path to save the raw JSON response")
    parser.add_argument("--save-image", help="Path to save the first inline image")
    args = parser.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY")
    base_url = build_base_url(args.endpoint_id, args.base_url)
    headers = build_headers(api_key)

    workflow = load_json(Path(args.workflow))
    overrides = load_json(Path(args.overrides_json)) if args.overrides_json else []

    wait_for_health(base_url, headers, retries=args.health_retries, delay_s=args.health_delay_s)

    payload = {
        "input": {
            "workflow": workflow,
            "overrides": overrides,
            "output_mode": args.output_mode,
        }
    }

    response = requests.post(
        f"{base_url}/runsync",
        headers=headers,
        json=payload,
        timeout=args.runsync_timeout_s,
    )
    response.raise_for_status()

    response_payload = response.json()
    status = response_payload.get("status")
    job_id = response_payload.get("id")

    if status not in {None, "COMPLETED"} and job_id:
        response_payload = poll_job_status(
            base_url,
            headers,
            job_id=job_id,
            timeout_s=args.runsync_timeout_s,
            poll_interval_s=args.status_poll_interval_s,
        )

    print(json.dumps(response_payload, indent=2)[:12000])

    if args.save_response:
        response_path = Path(args.save_response)
        response_path.write_text(json.dumps(response_payload, indent=2), encoding="utf-8")
        print(f"[smoke-test] saved response: {response_path}")

    if args.save_image:
        image_payload = first_inline_image(response_payload)
        if not image_payload:
            raise RuntimeError("response did not contain an inline image to save")
        image_bytes, filename = image_payload
        image_path = Path(args.save_image)
        image_path.write_bytes(image_bytes)
        print(f"[smoke-test] saved image: {image_path} ({filename}, {len(image_bytes)} bytes)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[smoke-test] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
