#!/usr/bin/env python3
"""Submit concurrent jobs to a Runpod/ComfyUI endpoint and summarize queue/execution behavior."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def record_health_sample(base_url: str, headers: dict[str, str]) -> dict[str, Any] | None:
    response = requests.get(f"{base_url}/health", headers=headers, timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return None
    return payload


def health_sampler(
    base_url: str,
    headers: dict[str, str],
    stop_event: threading.Event,
    interval_s: float,
    samples: list[dict[str, Any]],
) -> None:
    while not stop_event.is_set():
        try:
            sample = record_health_sample(base_url, headers)
            if sample:
                sample["_ts"] = time.time()
                samples.append(sample)
        except Exception as exc:
            samples.append({"_ts": time.time(), "error": str(exc)})
        stop_event.wait(interval_s)


def wait_for_terminal_status(
    base_url: str,
    headers: dict[str, str],
    job_id: str,
    timeout_s: int,
    poll_interval_s: float,
    request_index: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    status_url = f"{base_url}/status/{job_id}"
    while time.time() < deadline:
        response = requests.get(status_url, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
        status = payload.get("status")
        if status in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            print(f"[load-test] request={request_index} job={job_id} status={status}")
            return payload
        print(f"[load-test] request={request_index} job={job_id} status={status}")
        time.sleep(poll_interval_s)
    raise RuntimeError(f"timed out waiting for terminal status: {job_id}")


def wait_for_endpoint_drain(
    base_url: str,
    headers: dict[str, str],
    timeout_s: int,
    poll_interval_s: float,
) -> dict[str, Any] | None:
    deadline = time.time() + timeout_s
    last_sample: dict[str, Any] | None = None
    while time.time() < deadline:
        sample = record_health_sample(base_url, headers)
        if not sample:
            return None
        last_sample = sample
        jobs = sample.get("jobs", {})
        workers = sample.get("workers", {})
        if int(jobs.get("inQueue", 0)) == 0 and int(jobs.get("inProgress", 0)) == 0 and int(workers.get("running", 0)) == 0:
            return sample
        print(
            "[load-test] waiting for endpoint drain "
            f"(queue={jobs.get('inQueue', 0)} in_progress={jobs.get('inProgress', 0)} running={workers.get('running', 0)})"
        )
        time.sleep(poll_interval_s)
    raise RuntimeError(f"endpoint did not drain within {timeout_s}s: {json.dumps(last_sample or {}, indent=2)}")


def submit_async_job(
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    submit_timeout_s: int,
) -> tuple[str, dict[str, Any]]:
    response = requests.post(f"{base_url}/run", headers=headers, json=payload, timeout=submit_timeout_s)
    response.raise_for_status()
    job = response.json()
    return job["id"], job


def submit_sync_job(
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    submit_timeout_s: int,
) -> tuple[str | None, dict[str, Any]]:
    response = requests.post(f"{base_url}/runsync", headers=headers, json=payload, timeout=submit_timeout_s)
    response.raise_for_status()
    terminal = response.json()
    return terminal.get("id"), terminal


def run_single_job(
    request_index: int,
    base_url: str,
    headers: dict[str, str],
    workflow: dict[str, Any],
    overrides: list[dict[str, Any]],
    output_mode: str,
    request_mode: str,
    submit_timeout_s: int,
    poll_timeout_s: int,
    poll_interval_s: float,
) -> dict[str, Any]:
    started_at = time.time()
    payload = {
        "input": {
            "workflow": workflow,
            "overrides": overrides,
            "output_mode": output_mode,
        }
    }

    if request_mode == "run":
        job_id, _job = submit_async_job(base_url, headers, payload, submit_timeout_s)
        print(f"[load-test] request={request_index} submitted async job={job_id}")
        terminal = wait_for_terminal_status(
            base_url,
            headers,
            job_id,
            timeout_s=poll_timeout_s,
            poll_interval_s=poll_interval_s,
            request_index=request_index,
        )
    else:
        job_id, terminal = submit_sync_job(base_url, headers, payload, submit_timeout_s)
        status = terminal.get("status")
        print(f"[load-test] request={request_index} runsync status={status} job={job_id}")
        if status not in {None, "COMPLETED"} and job_id:
            terminal = wait_for_terminal_status(
                base_url,
                headers,
                job_id,
                timeout_s=poll_timeout_s,
                poll_interval_s=poll_interval_s,
                request_index=request_index,
            )

    finished_at = time.time()
    output = terminal.get("output", {}) if isinstance(terminal, dict) else {}
    return {
        "request_index": request_index,
        "job_id": job_id,
        "request_mode": request_mode,
        "status": terminal.get("status"),
        "wall_time_s": round(finished_at - started_at, 3),
        "delay_time_ms": terminal.get("delayTime"),
        "execution_time_ms": terminal.get("executionTime"),
        "has_output": bool(output),
        "error": terminal.get("error"),
    }


def failure_result(request_index: int, request_mode: str, error: Exception) -> dict[str, Any]:
    return {
        "request_index": request_index,
        "job_id": None,
        "request_mode": request_mode,
        "status": "CLIENT_ERROR",
        "wall_time_s": None,
        "delay_time_ms": None,
        "execution_time_ms": None,
        "has_output": False,
        "error": str(error),
    }


def summarize_results(
    started_at: float,
    finished_at: float,
    results: list[dict[str, Any]],
    health_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    successful = [item for item in results if item.get("status") == "COMPLETED"]
    failed = [item for item in results if item.get("status") != "COMPLETED"]
    delay_values = [float(item["delay_time_ms"]) for item in successful if item.get("delay_time_ms") is not None]
    execution_values = [float(item["execution_time_ms"]) for item in successful if item.get("execution_time_ms") is not None]
    wall_values = [float(item["wall_time_s"]) for item in successful if item.get("wall_time_s") is not None]

    max_workers: dict[str, int] = {}
    max_jobs: dict[str, int] = {}
    for sample in health_samples:
        for key, value in sample.get("workers", {}).items():
            max_workers[key] = max(max_workers.get(key, 0), int(value))
        for key, value in sample.get("jobs", {}).items():
            max_jobs[key] = max(max_jobs.get(key, 0), int(value))

    return {
        "total_requests": len(results),
        "completed_requests": len(successful),
        "failed_requests": len(failed),
        "total_wall_time_s": round(finished_at - started_at, 3),
        "delay_time_ms": {
            "avg": round(statistics.mean(delay_values), 3) if delay_values else None,
            "p50": round(percentile(delay_values, 0.50), 3) if delay_values else None,
            "p90": round(percentile(delay_values, 0.90), 3) if delay_values else None,
            "max": round(max(delay_values), 3) if delay_values else None,
        },
        "execution_time_ms": {
            "avg": round(statistics.mean(execution_values), 3) if execution_values else None,
            "p50": round(percentile(execution_values, 0.50), 3) if execution_values else None,
            "p90": round(percentile(execution_values, 0.90), 3) if execution_values else None,
            "max": round(max(execution_values), 3) if execution_values else None,
        },
        "request_wall_time_s": {
            "avg": round(statistics.mean(wall_values), 3) if wall_values else None,
            "p50": round(percentile(wall_values, 0.50), 3) if wall_values else None,
            "p90": round(percentile(wall_values, 0.90), 3) if wall_values else None,
            "max": round(max(wall_values), 3) if wall_values else None,
        },
        "health_peaks": {
            "workers": max_workers,
            "jobs": max_jobs,
            "samples": len(health_samples),
        },
        "failures": failed,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint-id", help="Runpod endpoint ID")
    parser.add_argument("--base-url", help="Base URL for a local or proxied worker API")
    parser.add_argument("--workflow", required=True, help="Path to workflow JSON")
    parser.add_argument("--overrides-json", help="Path to overrides JSON array")
    parser.add_argument("--total-requests", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--request-mode", choices=["run", "runsync"], default="runsync")
    parser.add_argument("--output-mode", default="inline", choices=["auto", "inline", "base64", "object_store", "s3"])
    parser.add_argument("--submit-timeout-s", type=int, default=300)
    parser.add_argument("--poll-timeout-s", type=int, default=1800)
    parser.add_argument("--poll-interval-s", type=float, default=2.0)
    parser.add_argument("--health-poll-interval-s", type=float, default=2.0)
    parser.add_argument("--wait-for-drain-timeout-s", type=int, default=0)
    parser.add_argument("--save-json", help="Path to save the full summary JSON")
    args = parser.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY")
    base_url = build_base_url(args.endpoint_id, args.base_url)
    headers = build_headers(api_key)
    workflow = load_json(Path(args.workflow))
    overrides = load_json(Path(args.overrides_json)) if args.overrides_json else []

    if args.wait_for_drain_timeout_s > 0:
        wait_for_endpoint_drain(
            base_url,
            headers,
            timeout_s=args.wait_for_drain_timeout_s,
            poll_interval_s=args.poll_interval_s,
        )

    health_samples: list[dict[str, Any]] = []
    health_stop = threading.Event()
    health_thread = threading.Thread(
        target=health_sampler,
        args=(base_url, headers, health_stop, args.health_poll_interval_s, health_samples),
        daemon=True,
    )
    health_thread.start()

    started_at = time.time()
    results: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            future_to_request_index = {}
            for index in range(args.total_requests):
                future = executor.submit(
                    run_single_job,
                    request_index=index,
                    base_url=base_url,
                    headers=headers,
                    workflow=workflow,
                    overrides=overrides,
                    output_mode=args.output_mode,
                    request_mode=args.request_mode,
                    submit_timeout_s=args.submit_timeout_s,
                    poll_timeout_s=args.poll_timeout_s,
                    poll_interval_s=args.poll_interval_s,
                )
                future_to_request_index[future] = index

            for future in as_completed(future_to_request_index):
                request_index = future_to_request_index[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    print(f"[load-test] request={request_index} failed: {exc}")
                    results.append(failure_result(request_index, args.request_mode, exc))
    finally:
        health_stop.set()
        health_thread.join(timeout=5)

    finished_at = time.time()
    results.sort(key=lambda item: item["request_index"])
    summary = summarize_results(started_at, finished_at, results, health_samples)
    print(json.dumps(summary, indent=2))

    if args.save_json:
        output_path = Path(args.save_json)
        output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"[load-test] saved summary: {output_path}")


if __name__ == "__main__":
    main()
