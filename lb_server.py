#!/usr/bin/env python3
"""FastAPI server for Runpod load-balancing endpoints backed by local ComfyUI."""

from __future__ import annotations

import argparse
import logging
import threading
import uuid
from typing import Any

import handler
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("arrakis-comfyui-lb")

WORKER_LOCK = threading.Lock()
app = FastAPI(title="Arrakis ComfyUI Load Balancer", version="1.0.0")


def comfy_ready() -> bool:
    return handler.check_server(f"http://{handler.COMFY_HOST}/system_stats", retries=1, delay_ms=0)


def extract_job_input(payload: Any) -> Any:
    if isinstance(payload, dict) and "input" in payload and isinstance(payload["input"], (dict, str)):
        return payload["input"]
    return payload


def execute_payload(payload: Any, request_id: str) -> dict[str, Any]:
    job_input = extract_job_input(payload)
    try:
        return handler.execute_job_input(job_input, job_id=request_id, progress_cb=lambda message: LOGGER.info("%s: %s", request_id, message))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - exercised in integration
        LOGGER.exception("request %s failed", request_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "mode": "load_balancer"}


@app.get("/ping")
async def ping() -> Response:
    return Response(status_code=200 if comfy_ready() else 204)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "mode": "load_balancer",
        "comfy_api_ready": comfy_ready(),
        "worker_busy": WORKER_LOCK.locked(),
    }


@app.post("/generate")
@app.post("/run")
@app.post("/runsync")
async def generate(request: Request) -> dict[str, Any]:
    if not comfy_ready():
        raise HTTPException(status_code=503, detail="comfyui is not ready yet")

    acquired = WORKER_LOCK.acquire(blocking=False)
    if not acquired:
        raise HTTPException(status_code=503, detail="worker is busy")

    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    try:
        payload = await request.json()
        result = execute_payload(payload, request_id)
        return {
            "id": request_id,
            "status": "COMPLETED",
            "output": result,
        }
    finally:
        WORKER_LOCK.release()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(__import__("os").environ.get("PORT", "8000")))
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
