#!/usr/bin/env python3
"""Install ComfyUI custom nodes declared in config/custom_nodes.json."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


COMFY_ROOT = Path("/comfyui")
CUSTOM_NODES_DIR = COMFY_ROOT / "custom_nodes"


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 1800) -> None:
    print("[custom-nodes]", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True, timeout=timeout)


def install_registry_node(node_id: str) -> None:
    run(["comfy-node-install", node_id], timeout=3600)


def install_git_node(url: str, commit: str | None = None, name: str | None = None) -> None:
    node_name = name or url.rstrip("/").split("/")[-1]
    dest = CUSTOM_NODES_DIR / node_name

    if dest.exists():
        print(f"[custom-nodes] {node_name} already exists, skipping")
        return

    run(["git", "clone", "--filter=blob:none", url, str(dest)])
    if commit:
        run(["git", "fetch", "--depth", "1", "origin", commit], cwd=dest)
        run(["git", "checkout", commit], cwd=dest)

    requirements = dest / "requirements.txt"
    if requirements.exists():
        run(["python", "-m", "pip", "install", "--no-cache-dir", "-r", str(requirements)], timeout=3600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    nodes = config.get("nodes", [])
    if not nodes:
        print("[custom-nodes] no nodes configured")
        return

    CUSTOM_NODES_DIR.mkdir(parents=True, exist_ok=True)

    for node in nodes:
        if node.get("disabled"):
            continue

        install_type = (node.get("install") or {}).get("type", node.get("type", "git"))
        if install_type == "registry":
            node_id = (node.get("install") or {}).get("id", node.get("id"))
            if not node_id:
                raise ValueError(f"registry node is missing 'id': {node}")
            install_registry_node(node_id)
            continue

        if install_type != "git":
            raise ValueError(f"unsupported node install type: {install_type}")

        install_cfg = node.get("install") or node
        url = install_cfg.get("url")
        commit = install_cfg.get("commit")
        if not url:
            raise ValueError(f"git node is missing 'url': {node}")

        install_git_node(url=url, commit=commit, name=node.get("name"))


if __name__ == "__main__":
    main()
