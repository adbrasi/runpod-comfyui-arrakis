#!/usr/bin/env python3
"""Audit model references in a ComfyUI workflow against a local models directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


MODEL_HINT_KEYS = {
    "audio_vae",
    "ckpt_name",
    "clip_name",
    "clip_vision",
    "control_net_name",
    "controlnet_name",
    "embedding",
    "lora",
    "lora_name",
    "model_name",
    "text_encoder",
    "unet_name",
    "upscale_model",
    "upscale_model_name",
    "vae_name",
}
MODEL_EXTENSIONS = {".bin", ".ckpt", ".onnx", ".pt", ".pth", ".safetensors"}


def looks_like_model_ref(key: str | None, value: str) -> bool:
    if Path(value).suffix.lower() in MODEL_EXTENSIONS:
        return True
    if key and key.lower() in MODEL_HINT_KEYS:
        return True
    return False


def collect_refs(obj: Any, key: str | None = None) -> set[str]:
    refs: set[str] = set()
    if isinstance(obj, dict):
        for child_key, child_value in obj.items():
            refs.update(collect_refs(child_value, child_key))
        return refs
    if isinstance(obj, list):
        for item in obj:
            refs.update(collect_refs(item, key))
        return refs
    if isinstance(obj, str) and looks_like_model_ref(key, obj):
        refs.add(obj)
    return refs


def find_matches(models_root: Path, reference: str) -> list[Path]:
    reference_path = Path(reference)
    candidates: list[Path] = []

    direct = models_root / reference_path
    if direct.exists():
        candidates.append(direct)

    basename = reference_path.name
    for match in models_root.rglob(basename):
        suffix = match.relative_to(models_root).as_posix()
        if reference_path.as_posix() in suffix or basename == suffix or match.name == basename:
            candidates.append(match)

    unique: list[Path] = []
    seen = set()
    for item in candidates:
        resolved = item.resolve()
        if resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    return unique


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", required=True, help="Path to workflow JSON")
    parser.add_argument(
        "--models-root",
        default="/runpod-volume/models",
        help="Directory containing ComfyUI models",
    )
    args = parser.parse_args()

    workflow = json.loads(Path(args.workflow).read_text(encoding="utf-8"))
    models_root = Path(args.models_root).resolve()
    if not models_root.exists():
        raise RuntimeError(f"models root does not exist: {models_root}")

    refs = sorted(collect_refs(workflow))
    if not refs:
        print("[workflow-audit] no model references found")
        return

    missing: list[str] = []
    for ref in refs:
        matches = find_matches(models_root, ref)
        if not matches:
            missing.append(ref)
            print(f"[workflow-audit] MISSING: {ref}")
            continue
        for match in matches:
            print(f"[workflow-audit] OK: {ref} -> {match}")

    if missing:
        print(f"[workflow-audit] missing {len(missing)} model references", file=sys.stderr)
        sys.exit(1)

    print(f"[workflow-audit] all {len(refs)} model references resolved")


if __name__ == "__main__":
    main()
