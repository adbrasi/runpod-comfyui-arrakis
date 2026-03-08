#!/usr/bin/env python3
"""Download models declared in config/models.json into /comfyui/models."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


COMFY_ROOT = Path("/comfyui")


def ensure_relative_target(target_path: str) -> Path:
    normalized = Path(target_path)
    if normalized.is_absolute():
        raise ValueError(f"target_path must be relative, got: {target_path}")
    if not str(normalized).startswith("models/"):
        raise ValueError(f"target_path must start with 'models/', got: {target_path}")
    return COMFY_ROOT / normalized


def download_from_hf(model: dict, target: Path) -> None:
    from huggingface_hub import hf_hub_download

    repo_id = model["repo_id"]
    filename = model["filename"]
    local_dir = target.parent
    local_dir.mkdir(parents=True, exist_ok=True)

    token_env = model.get("token_env")
    token = os.environ.get(token_env) if token_env else None

    print(f"[models] hf://{repo_id}/{filename} -> {target}")
    hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        local_dir=str(local_dir),
        token=token,
        local_dir_use_symlinks=False,
    )

    downloaded = local_dir / Path(filename).name
    if downloaded != target:
        downloaded.rename(target)


def download_from_url(model: dict, target: Path) -> None:
    import requests

    url = model["url"]
    headers = {}

    auth_env = model.get("auth_env")
    if auth_env and os.environ.get(auth_env):
        headers["Authorization"] = f"Bearer {os.environ[auth_env]}"

    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"[models] {url} -> {target}")
    with requests.get(url, stream=True, timeout=600, headers=headers) as response:
        response.raise_for_status()
        with target.open("wb") as fh:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    fh.write(chunk)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    models = config.get("models", [])
    if not models:
        print("[models] no models configured")
        return

    for model in models:
        if model.get("disabled"):
            continue

        target = ensure_relative_target(model["target_path"])
        min_size = int(model.get("min_size_bytes", 1_000_000))
        if target.exists() and target.stat().st_size >= min_size:
            print(f"[models] cached: {target}")
            continue

        source = model.get("source", "url")
        if source == "hf":
            download_from_hf(model, target)
        elif source == "url":
            download_from_url(model, target)
        else:
            raise ValueError(f"unsupported model source: {source}")


if __name__ == "__main__":
    main()
