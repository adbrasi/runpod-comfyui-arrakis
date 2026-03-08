#!/usr/bin/env python3
"""Upload models from a manifest into a Runpod Network Volume via the S3 API."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import requests


S3_ENDPOINTS = {
    "EUR-IS-1": "https://s3api-eur-is-1.runpod.io",
    "EUR-NO-1": "https://s3api-eur-no-1.runpod.io",
    "EU-RO-1": "https://s3api-eu-ro-1.runpod.io",
    "EU-CZ-1": "https://s3api-eu-cz-1.runpod.io",
    "US-CA-2": "https://s3api-us-ca-2.runpod.io",
    "US-GA-2": "https://s3api-us-ga-2.runpod.io",
    "US-KS-2": "https://s3api-us-ks-2.runpod.io",
    "US-MD-1": "https://s3api-us-md-1.runpod.io",
    "US-MO-2": "https://s3api-us-mo-2.runpod.io",
    "US-NC-1": "https://s3api-us-nc-1.runpod.io",
    "US-NC-2": "https://s3api-us-nc-2.runpod.io",
}
COMFY_MODEL_ROOT = "models/"


def ensure_target_path(target_path: str) -> str:
    normalized = Path(target_path)
    if normalized.is_absolute():
        raise ValueError(f"target_path must be relative, got: {target_path}")
    normalized_str = normalized.as_posix()
    if not normalized_str.startswith(COMFY_MODEL_ROOT):
        raise ValueError(f"target_path must start with '{COMFY_MODEL_ROOT}', got: {target_path}")
    return normalized_str


def endpoint_for_datacenter(data_center_id: str) -> str:
    try:
        return S3_ENDPOINTS[data_center_id]
    except KeyError as exc:
        supported = ", ".join(sorted(S3_ENDPOINTS))
        raise ValueError(
            f"S3-compatible API is not available for data center '{data_center_id}'. "
            f"Supported data centers: {supported}"
        ) from exc


def parse_target(value: str) -> tuple[str, str]:
    volume_id, separator, data_center_id = value.partition(":")
    if not separator or not volume_id or not data_center_id:
        raise ValueError("target must use the format <volume_id>:<data_center_id>")
    return volume_id, data_center_id


def create_s3_client(endpoint_url: str, access_key: str, secret_key: str, region: str):
    try:
        import boto3
        from botocore.client import Config
    except ModuleNotFoundError as exc:
        raise RuntimeError("boto3 is required to sync models to a Runpod volume") from exc

    retry_config = Config(
        retries={"max_attempts": int(os.environ.get("AWS_MAX_ATTEMPTS", "10")), "mode": "standard"},
        signature_version="s3v4",
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=retry_config,
    )


def object_exists(client, bucket: str, key: str, min_size_bytes: int) -> bool:
    try:
        metadata = client.head_object(Bucket=bucket, Key=key)
    except Exception:
        return False
    return int(metadata.get("ContentLength", 0)) >= min_size_bytes


def download_from_hf(model: dict[str, Any], workdir: Path) -> Path:
    from huggingface_hub import hf_hub_download

    repo_id = model["repo_id"]
    filename = model["filename"]
    token_env = model.get("token_env")
    token = os.environ.get(token_env) if token_env else None

    print(f"[volume-sync] hf://{repo_id}/{filename}")
    return Path(
        hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            local_dir=str(workdir),
            token=token,
            local_dir_use_symlinks=False,
        )
    )


def download_from_url(model: dict[str, Any], workdir: Path) -> Path:
    url = model["url"]
    filename = Path(model.get("filename") or Path(url).name).name
    target = workdir / filename
    headers = {}

    auth_env = model.get("auth_env")
    if auth_env and os.environ.get(auth_env):
        headers["Authorization"] = f"Bearer {os.environ[auth_env]}"

    print(f"[volume-sync] {url}")
    with requests.get(url, stream=True, timeout=600, headers=headers) as response:
        response.raise_for_status()
        with target.open("wb") as fh:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    fh.write(chunk)
    return target


def resolve_local_source(model: dict[str, Any]) -> Path:
    local_path = Path(model["local_path"]).expanduser().resolve()
    if not local_path.exists():
        raise FileNotFoundError(f"local model not found: {local_path}")
    return local_path


def materialize_model(model: dict[str, Any], workdir: Path) -> Path:
    source = model.get("source", "url")
    if source == "hf":
        return download_from_hf(model, workdir)
    if source == "url":
        return download_from_url(model, workdir)
    if source == "local":
        return resolve_local_source(model)
    raise ValueError(f"unsupported model source: {source}")


def resolve_targets(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    if args.target:
        resolved_targets = []
        for raw_target in args.target:
            volume_id, data_center_id = parse_target(raw_target)
            resolved_targets.append((volume_id, data_center_id, endpoint_for_datacenter(data_center_id)))
        return resolved_targets

    if not args.volume_id:
        raise RuntimeError("set --volume-id or RUNPOD_VOLUME_ID, or provide one or more --target values")

    endpoint_url = args.endpoint_url
    if not endpoint_url:
        if not args.data_center_id:
            raise RuntimeError("set --endpoint-url or provide --data-center-id / RUNPOD_VOLUME_DATA_CENTER_ID")
        endpoint_url = endpoint_for_datacenter(args.data_center_id)

    return [(args.volume_id, args.data_center_id or os.environ.get("AWS_REGION") or "us-east-1", endpoint_url)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to model manifest JSON")
    parser.add_argument("--volume-id", default=os.environ.get("RUNPOD_VOLUME_ID"), help="Runpod network volume ID")
    parser.add_argument(
        "--data-center-id",
        default=os.environ.get("RUNPOD_VOLUME_DATA_CENTER_ID"),
        help="Runpod data center ID for the volume",
    )
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("RUNPOD_VOLUME_S3_ENDPOINT"),
        help="Override the Runpod S3-compatible endpoint URL",
    )
    parser.add_argument(
        "--target",
        action="append",
        help="Repeatable target in the format <volume_id>:<data_center_id> to seed multiple volumes in one run",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned operations without uploading")
    args = parser.parse_args()

    access_key = os.environ.get("RUNPOD_VOLUME_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("RUNPOD_VOLUME_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        raise RuntimeError(
            "set RUNPOD_VOLUME_ACCESS_KEY_ID/RUNPOD_VOLUME_SECRET_ACCESS_KEY "
            "or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY"
        )

    manifest = json.loads(Path(args.config).read_text(encoding="utf-8"))
    models = manifest.get("models", [])
    if not models:
        print("[volume-sync] no models configured")
        return

    targets = resolve_targets(args)
    try:
        from boto3.s3.transfer import TransferConfig
    except ModuleNotFoundError as exc:
        raise RuntimeError("boto3 is required to sync models to a Runpod volume") from exc

    transfer = TransferConfig(multipart_threshold=64 * 1024 * 1024, multipart_chunksize=64 * 1024 * 1024)

    for volume_id, region, endpoint_url in targets:
        client = create_s3_client(endpoint_url, access_key, secret_key, region)
        print(f"[volume-sync] target volume={volume_id} region={region} endpoint={endpoint_url}")

        for model in models:
            if model.get("disabled"):
                continue

            key = ensure_target_path(model["target_path"])
            min_size_bytes = int(model.get("min_size_bytes", 1_000_000))

            print(f"[volume-sync] upload target: s3://{volume_id}/{key}")
            if args.dry_run:
                continue

            if object_exists(client, volume_id, key, min_size_bytes):
                print(f"[volume-sync] cached: s3://{volume_id}/{key}")
                continue

            with tempfile.TemporaryDirectory(prefix="runpod-volume-sync-") as tmpdir:
                local_file = materialize_model(model, Path(tmpdir))
                client.upload_file(str(local_file), volume_id, key, Config=transfer)
                print(f"[volume-sync] uploaded: s3://{volume_id}/{key}")


if __name__ == "__main__":
    main()
