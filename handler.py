"""Universal Runpod ComfyUI handler with workflow overrides and generic outputs."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
import mimetypes
import os
import shutil
import socket
import tempfile
import time
import traceback
import urllib.parse
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    import runpod
except ImportError:  # pragma: no cover - local unit tests without runpod installed
    runpod = None

try:
    from network_volume import is_network_volume_debug_enabled, run_network_volume_diagnostics
except ImportError:  # pragma: no cover - local unit tests without base image internals
    def is_network_volume_debug_enabled() -> bool:
        return False

    def run_network_volume_diagnostics() -> None:
        return None


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("arrakis-comfyui")

COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1:8188")
COMFY_INPUT_ROOT = Path(os.environ.get("COMFY_INPUT_ROOT", "/comfyui/input"))
COMFY_OUTPUT_ROOT = Path(os.environ.get("COMFY_OUTPUT_ROOT", "/comfyui/output"))

COMFY_API_AVAILABLE_INTERVAL_MS = int(os.environ.get("COMFY_API_AVAILABLE_INTERVAL_MS", "100"))
COMFY_API_AVAILABLE_MAX_RETRIES = int(os.environ.get("COMFY_API_AVAILABLE_MAX_RETRIES", "300"))
COMFY_HISTORY_INTERVAL_MS = int(os.environ.get("COMFY_HISTORY_INTERVAL_MS", "250"))
COMFY_HISTORY_MAX_RETRIES = int(os.environ.get("COMFY_HISTORY_MAX_RETRIES", "120"))
WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", "5"))
WEBSOCKET_RECONNECT_DELAY_S = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", "3"))
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"
DEFAULT_OUTPUT_MODE = os.environ.get("DEFAULT_OUTPUT_MODE", "auto").lower()
S3_PRESIGN_TTL_SECONDS = int(os.environ.get("S3_PRESIGN_TTL_SECONDS", "86400"))

KNOWN_FILE_OUTPUT_TYPES = {
    "images": "image",
    "videos": "video",
    "gifs": "gif",
    "audio": "audio",
}


def _requests():
    import requests

    return requests


def _websocket():
    import websocket

    if os.environ.get("WEBSOCKET_TRACE", "false").lower() == "true":
        websocket.enableTrace(True)
    return websocket


def _boto3():
    import boto3

    return boto3


def _progress(job: dict[str, Any], message: str) -> None:
    LOGGER.info(message)
    if runpod is None:
        return
    try:
        runpod.serverless.progress_update(job, message)
    except Exception:
        LOGGER.debug("progress_update failed", exc_info=True)


def _storage_config() -> dict[str, Any] | None:
    endpoint = (
        os.environ.get("S3_ENDPOINT_URL")
        or os.environ.get("OUTPUT_S3_ENDPOINT_URL")
        or os.environ.get("BUCKET_ENDPOINT_URL")
    )
    access_key = (
        os.environ.get("S3_ACCESS_KEY_ID")
        or os.environ.get("OUTPUT_S3_ACCESS_KEY_ID")
        or os.environ.get("BUCKET_ACCESS_KEY_ID")
    )
    secret_key = (
        os.environ.get("S3_SECRET_ACCESS_KEY")
        or os.environ.get("OUTPUT_S3_SECRET_ACCESS_KEY")
        or os.environ.get("BUCKET_SECRET_ACCESS_KEY")
    )
    bucket_name = (
        os.environ.get("S3_BUCKET_NAME")
        or os.environ.get("OUTPUT_S3_BUCKET_NAME")
        or os.environ.get("BUCKET_NAME")
    )
    region = (
        os.environ.get("S3_REGION")
        or os.environ.get("OUTPUT_S3_REGION")
        or os.environ.get("BUCKET_REGION")
        or "auto"
    )
    public_base_url = (
        os.environ.get("S3_PUBLIC_BASE_URL")
        or os.environ.get("OUTPUT_S3_PUBLIC_BASE_URL")
        or os.environ.get("BUCKET_PUBLIC_BASE_URL")
    )

    if not endpoint or not access_key or not secret_key or not bucket_name:
        return None

    return {
        "endpoint": endpoint,
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket_name": bucket_name,
        "region": region,
        "public_base_url": public_base_url,
    }


def _s3_client():
    cfg = _storage_config()
    if not cfg:
        raise RuntimeError("object storage is not configured")
    return _boto3().client(
        "s3",
        endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
        region_name=cfg["region"],
    )


def _validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must use http or https")

    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        raise ValueError("URL points to a restricted host")

    try:
        ip = ipaddress.ip_address(host)
        _validate_ip_address(ip)
    except ValueError as exc:
        if "private" in str(exc) or "restricted" in str(exc) or "loopback" in str(exc):
            raise
        for resolved_ip in _resolve_host_ips(host):
            _validate_ip_address(resolved_ip)


def _validate_ip_address(ip: ipaddress._BaseAddress) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise ValueError("URL points to a restricted IP")


def _resolve_host_ips(host: str) -> set[ipaddress._BaseAddress]:
    try:
        info = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return set()

    return {ipaddress.ip_address(item[4][0]) for item in info}


def _strip_data_uri(value: str) -> str:
    return value.split(",", 1)[1] if "," in value else value


def _normalize_asset_list(job_input: dict[str, Any]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []

    for image in job_input.get("images", []) or []:
        assets.append(
            {
                "name": image["name"],
                "data": image.get("image") or image.get("data"),
                "url": image.get("url"),
            }
        )

    for field_name in ("assets", "files", "media"):
        for item in job_input.get(field_name, []) or []:
            assets.append(
                {
                    "name": item["name"],
                    "data": item.get("data") or item.get("image") or item.get("base64"),
                    "url": item.get("url"),
                }
            )

    names = set()
    for asset in assets:
        if not asset.get("name"):
            raise ValueError("every asset must include a name")
        if not asset.get("data") and not asset.get("url"):
            raise ValueError(f"asset '{asset['name']}' must include either data or url")
        if asset["name"] in names:
            raise ValueError(f"duplicate asset name: {asset['name']}")
        names.add(asset["name"])

    return assets


def validate_input(job_input: Any) -> dict[str, Any]:
    if job_input is None:
        raise ValueError("missing input")

    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError as exc:
            raise ValueError("input is not valid JSON") from exc

    if not isinstance(job_input, dict):
        raise ValueError("input must be a JSON object")

    workflow = job_input.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        raise ValueError("input.workflow must be a non-empty object")

    overrides = job_input.get("overrides")
    if overrides is None:
        overrides = job_input.get("inputs", [])
    if not isinstance(overrides, list):
        raise ValueError("input.overrides must be a list")

    output_mode = str(job_input.get("output_mode", DEFAULT_OUTPUT_MODE)).lower()
    if output_mode not in {"auto", "inline", "base64", "object_store", "s3"}:
        raise ValueError("input.output_mode must be one of auto, inline, base64, object_store or s3")

    return {
        "workflow": deepcopy(workflow),
        "overrides": overrides,
        "assets": _normalize_asset_list(job_input),
        "output_mode": output_mode,
        "comfy_org_api_key": job_input.get("comfy_org_api_key"),
    }


def check_server(url: str, retries: int = COMFY_API_AVAILABLE_MAX_RETRIES, delay_ms: int = COMFY_API_AVAILABLE_INTERVAL_MS) -> bool:
    requests = _requests()
    for _ in range(retries):
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(delay_ms / 1000)
    return False


def prepare_assets(job_id: str, assets: list[dict[str, Any]]) -> dict[str, str]:
    requests = _requests()

    for asset in assets:
        if asset.get("url"):
            _validate_url(asset["url"])

    COMFY_INPUT_ROOT.mkdir(parents=True, exist_ok=True)
    job_dir = COMFY_INPUT_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    mapping: dict[str, str] = {}
    for asset in assets:
        original_name = asset["name"]
        safe_name = Path(original_name).name
        relative_name = f"{job_id}/{safe_name}"
        destination = job_dir / safe_name

        if asset.get("data"):
            try:
                destination.write_bytes(base64.b64decode(_strip_data_uri(asset["data"])))
            except binascii.Error as exc:
                raise ValueError(f"asset '{original_name}' is not valid base64") from exc
        else:
            with requests.get(asset["url"], stream=True, timeout=120) as response:
                response.raise_for_status()
                with destination.open("wb") as fh:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            fh.write(chunk)

        mapping[original_name] = relative_name
        LOGGER.info("prepared asset %s -> %s", original_name, relative_name)

    return mapping


def remap_workflow_paths(obj: Any, mapping: dict[str, str]) -> Any:
    if isinstance(obj, dict):
        return {key: remap_workflow_paths(value, mapping) for key, value in obj.items()}
    if isinstance(obj, list):
        return [remap_workflow_paths(value, mapping) for value in obj]
    if isinstance(obj, str) and obj in mapping:
        return mapping[obj]
    return obj


def apply_overrides(workflow: dict[str, Any], overrides: list[dict[str, Any]]) -> None:
    for override in overrides:
        node_id = str(override["node"])
        field = override["field"]
        if node_id not in workflow:
            raise ValueError(f"override references unknown node: {node_id}")
        workflow.setdefault(node_id, {}).setdefault("inputs", {})
        workflow[node_id]["inputs"][field] = override["value"]


def queue_workflow(workflow: dict[str, Any], client_id: str, comfy_org_api_key: str | None = None) -> str:
    requests = _requests()

    payload: dict[str, Any] = {
        "prompt": workflow,
        "client_id": client_id,
    }
    effective_key = comfy_org_api_key or os.environ.get("COMFY_ORG_API_KEY")
    if effective_key:
        payload["extra_data"] = {"api_key_comfy_org": effective_key}

    response = requests.post(f"http://{COMFY_HOST}/prompt", json=payload, timeout=30)
    if response.status_code == 400:
        raise ValueError(f"workflow validation failed: {response.text}")
    response.raise_for_status()
    data = response.json()
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise ValueError(f"missing prompt_id in queue response: {data}")
    return prompt_id


def _attempt_websocket_reconnect(ws_url: str, initial_error: Exception):
    websocket = _websocket()
    last_error = initial_error
    for attempt in range(WEBSOCKET_RECONNECT_ATTEMPTS):
        try:
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)
            return new_ws
        except (websocket.WebSocketException, ConnectionRefusedError, socket.timeout, OSError) as exc:
            last_error = exc
            LOGGER.warning("websocket reconnect %s/%s failed: %s", attempt + 1, WEBSOCKET_RECONNECT_ATTEMPTS, exc)
            time.sleep(WEBSOCKET_RECONNECT_DELAY_S)
    raise websocket.WebSocketConnectionClosedException(str(last_error))


def get_history(prompt_id: str) -> dict[str, Any]:
    requests = _requests()
    response = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def wait_for_prompt_history(
    prompt_id: str,
    retries: int = COMFY_HISTORY_MAX_RETRIES,
    delay_ms: int = COMFY_HISTORY_INTERVAL_MS,
) -> dict[str, Any]:
    last_history: dict[str, Any] = {}
    for _ in range(retries):
        last_history = get_history(prompt_id)
        prompt_history = last_history.get(prompt_id)
        if isinstance(prompt_history, dict):
            return prompt_history
        time.sleep(delay_ms / 1000)
    raise RuntimeError(f"prompt history did not become available for {prompt_id}")


def wait_for_completion(client_id: str, prompt_id: str) -> None:
    websocket = _websocket()
    requests = _requests()
    ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
    ws = websocket.WebSocket()
    ws.connect(ws_url, timeout=10)
    ws.settimeout(15)

    try:
        while True:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                history = get_history(prompt_id)
                prompt_history = history.get(prompt_id, {})
                status = prompt_history.get("status", {})
                status_value = status.get("status") if isinstance(status, dict) else status
                if str(status_value).lower() in {"success", "completed"}:
                    return
                continue
            except websocket.WebSocketConnectionClosedException as exc:
                ws = _attempt_websocket_reconnect(ws_url, exc)
                continue

            if not isinstance(raw, str):
                continue

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            message_type = message.get("type")
            data = message.get("data", {})
            if data.get("prompt_id") and data["prompt_id"] != prompt_id:
                continue

            if message_type == "execution_error":
                raise RuntimeError(data.get("exception_message") or str(data))

            if message_type == "executing" and data.get("node") is None and data.get("prompt_id") == prompt_id:
                return

            if message_type == "status":
                queue_remaining = data.get("status", {}).get("exec_info", {}).get("queue_remaining")
                LOGGER.info("queue remaining: %s", queue_remaining)

    finally:
        try:
            ws.close()
        except Exception:
            pass


def fetch_output_bytes(file_info: dict[str, Any]) -> bytes:
    requests = _requests()
    response = requests.get(
        f"http://{COMFY_HOST}/view",
        params={
            "filename": file_info["filename"],
            "subfolder": file_info.get("subfolder", ""),
            "type": file_info.get("type", "output"),
        },
        timeout=120,
        stream=True,
    )
    response.raise_for_status()
    payload = b"".join(response.iter_content(1024 * 1024))
    response.close()
    return payload


def output_path_for_file_info(file_info: dict[str, Any]) -> Path | None:
    if file_info.get("type") == "temp":
        return None

    filename = Path(file_info["filename"]).name
    subfolder = Path(file_info.get("subfolder", ""))
    candidate = (COMFY_OUTPUT_ROOT / subfolder / filename).resolve()
    output_root = COMFY_OUTPUT_ROOT.resolve()

    if not candidate.is_relative_to(output_root):
        raise ValueError(f"unsafe output path returned by ComfyUI: {candidate}")
    return candidate


def iter_output_file_infos(prompt_history: dict[str, Any]) -> list[dict[str, Any]]:
    file_infos: list[dict[str, Any]] = []
    for node_output in (prompt_history.get("outputs") or {}).values():
        for value in node_output.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("filename"):
                        file_infos.append(item)
            elif isinstance(value, dict) and value.get("filename"):
                file_infos.append(value)
    return file_infos


def cleanup_prompt_outputs(prompt_history: dict[str, Any]) -> None:
    for file_info in iter_output_file_infos(prompt_history):
        try:
            output_path = output_path_for_file_info(file_info)
        except ValueError:
            LOGGER.warning("skipping unsafe output path cleanup for %s", file_info, exc_info=True)
            continue

        if output_path is None:
            continue
        try:
            output_path.unlink(missing_ok=True)
            LOGGER.info("removed output file %s", output_path)
        except Exception:
            LOGGER.warning("failed to remove output file %s", output_path, exc_info=True)
            continue

        parent = output_path.parent
        output_root = COMFY_OUTPUT_ROOT.resolve()
        while parent != output_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def store_output_payload(job_id: str, filename: str, payload: bytes, output_mode: str) -> dict[str, Any]:
    resolved_mode = output_mode
    storage_cfg = _storage_config()
    if output_mode == "auto":
        resolved_mode = "object_store" if storage_cfg else "inline"
    elif output_mode == "s3":
        resolved_mode = "object_store"

    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    size_bytes = len(payload)

    if resolved_mode == "inline":
        return {
            "mode": "inline",
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "data": base64.b64encode(payload).decode("utf-8"),
        }

    if not storage_cfg:
        raise RuntimeError("output_mode requires object storage, but S3/R2 environment variables are missing")

    key = f"outputs/{job_id}/{filename}"
    client = _s3_client()
    client.put_object(Bucket=storage_cfg["bucket_name"], Key=key, Body=payload, ContentType=mime_type)

    if storage_cfg.get("public_base_url"):
        base = storage_cfg["public_base_url"].rstrip("/")
        url = f"{base}/{key}"
    else:
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": storage_cfg["bucket_name"], "Key": key},
            ExpiresIn=S3_PRESIGN_TTL_SECONDS,
        )

    return {
        "mode": "object_store",
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "url": url,
        "storage_key": key,
    }


def _scalar_output(node_id: str, field: str, value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        output_type = "text"
    elif isinstance(value, bool):
        output_type = "boolean"
    elif isinstance(value, (int, float)):
        output_type = "number"
    else:
        output_type = "json"
    return {"node_id": node_id, "field": field, "type": output_type, "data": value}


def collect_outputs(job_id: str, prompt_history: dict[str, Any], output_mode: str) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for node_id, node_output in (prompt_history.get("outputs") or {}).items():
        for field, value in node_output.items():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("filename"):
                        if item.get("type") == "temp":
                            continue
                        filename = item["filename"]
                        payload = fetch_output_bytes(item)
                        storage = store_output_payload(job_id, filename, payload, output_mode)
                        outputs.append(
                            {
                                "node_id": node_id,
                                "field": field,
                                "type": KNOWN_FILE_OUTPUT_TYPES.get(field, mimetypes.guess_type(filename)[0] or "file"),
                                "filename": filename,
                                **storage,
                            }
                        )
                    else:
                        outputs.append(_scalar_output(node_id, field, item))
            elif isinstance(value, dict) and value.get("filename"):
                payload = fetch_output_bytes(value)
                storage = store_output_payload(job_id, value["filename"], payload, output_mode)
                outputs.append(
                    {
                        "node_id": node_id,
                        "field": field,
                        "type": KNOWN_FILE_OUTPUT_TYPES.get(field, mimetypes.guess_type(value["filename"])[0] or "file"),
                        "filename": value["filename"],
                        **storage,
                    }
                )
            else:
                outputs.append(_scalar_output(node_id, field, value))
    return outputs


def handler(job: dict[str, Any]) -> dict[str, Any]:
    if is_network_volume_debug_enabled():
        run_network_volume_diagnostics()

    _progress(job, "validating request")
    normalized = validate_input(job.get("input"))

    if not check_server(f"http://{COMFY_HOST}/system_stats"):
        raise RuntimeError(f"ComfyUI server is not reachable on {COMFY_HOST}")

    job_id = job.get("id") or uuid.uuid4().hex
    workflow = normalized["workflow"]
    assets = normalized["assets"]
    prompt_id: str | None = None
    prompt_history: dict[str, Any] | None = None

    try:
        if assets:
            _progress(job, "preparing assets")
            remap = prepare_assets(job_id, assets)
            workflow = remap_workflow_paths(workflow, remap)

        if normalized["overrides"]:
            _progress(job, "applying workflow overrides")
            apply_overrides(workflow, normalized["overrides"])

        client_id = uuid.uuid4().hex
        _progress(job, "queueing workflow")
        prompt_id = queue_workflow(workflow, client_id, normalized.get("comfy_org_api_key"))

        _progress(job, "waiting for comfyui execution")
        wait_for_completion(client_id, prompt_id)

        _progress(job, "collecting outputs")
        prompt_history = wait_for_prompt_history(prompt_id)
        outputs = collect_outputs(job_id, prompt_history, normalized["output_mode"])

        result: dict[str, Any] = {
            "prompt_id": prompt_id,
            "outputs": outputs,
            "refresh_worker": REFRESH_WORKER,
        }

        status = prompt_history.get("status", {})
        if status:
            result["comfy_status"] = status
        if not outputs:
            result["warnings"] = ["workflow completed without outputs"]
        return result

    except Exception as exc:
        LOGGER.error("handler failed: %s", exc)
        LOGGER.debug(traceback.format_exc())
        raise

    finally:
        if prompt_id and prompt_history is None:
            try:
                prompt_history = wait_for_prompt_history(prompt_id, retries=10, delay_ms=200)
            except Exception:
                LOGGER.warning("failed to fetch prompt history during cleanup for %s", prompt_id, exc_info=True)

        if prompt_history:
            cleanup_prompt_outputs(prompt_history)
        shutil.rmtree(COMFY_INPUT_ROOT / job_id, ignore_errors=True)


if __name__ == "__main__":
    if runpod is None:
        raise RuntimeError("runpod package is required to run the handler directly")
    runpod.serverless.start({"handler": handler})
