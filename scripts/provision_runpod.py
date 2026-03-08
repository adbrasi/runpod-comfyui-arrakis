#!/usr/bin/env python3
"""Create or update Runpod network volumes, registry auths, templates, secrets and endpoints."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests


REST_BASE = "https://rest.runpod.io/v1"
GRAPHQL_BASE = "https://api.runpod.io/graphql"
S3_ENDPOINTS = {
    "EUR-IS-1": "https://s3api-eur-is-1.runpod.io",
    "EU-RO-1": "https://s3api-eu-ro-1.runpod.io",
    "EU-CZ-1": "https://s3api-eu-cz-1.runpod.io",
    "US-KS-2": "https://s3api-us-ks-2.runpod.io",
    "US-CA-2": "https://s3api-us-ca-2.runpod.io",
}


class RunpodClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.rest_headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def rest(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        response = requests.request(
            method,
            f"{REST_BASE}{path}",
            headers=self.rest_headers,
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        if response.text:
            return response.json()
        return None

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        response = requests.post(
            f"{GRAPHQL_BASE}?api_key={self.api_key}",
            json={"query": query, "variables": variables or {}},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            raise RuntimeError(data["errors"])
        return data["data"]

    def list_templates(self) -> list[dict[str, Any]]:
        data = self.rest("GET", "/templates")
        return data if isinstance(data, list) else data.get("items", [])

    def list_endpoints(self) -> list[dict[str, Any]]:
        data = self.rest("GET", "/endpoints")
        return data if isinstance(data, list) else data.get("items", [])

    def list_network_volumes(self) -> list[dict[str, Any]]:
        data = self.rest("GET", "/networkvolumes")
        return data if isinstance(data, list) else data.get("items", [])

    def get_network_volume(self, network_volume_id: str) -> dict[str, Any]:
        return self.rest("GET", f"/networkvolumes/{network_volume_id}")

    def list_container_registry_auths(self) -> list[dict[str, Any]]:
        data = self.rest("GET", "/containerregistryauth")
        return data if isinstance(data, list) else data.get("items", [])

    def create_container_registry_auth(self, name: str, username: str, password: str) -> dict[str, Any]:
        payload = {"name": name, "username": username, "password": password}
        return self.rest("POST", "/containerregistryauth", payload)

    def list_secrets(self) -> list[dict[str, Any]]:
        data = self.graphql("query { myself { secrets { id name description } } }")
        return data["myself"]["secrets"]

    def ensure_secret(self, name: str, value: str) -> None:
        existing = {item["name"] for item in self.list_secrets()}
        if name in existing:
            print(f"[runpod] secret exists: {name}")
            return

        mutation = """
        mutation SecretCreate($name: String!, $value: String!) {
          secretCreate(input: { name: $name, value: $value }) {
            id
            name
          }
        }
        """
        self.graphql(mutation, {"name": name, "value": value})
        print(f"[runpod] secret created: {name}")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_readme(config_dir: Path, template_cfg: dict[str, Any]) -> str:
    readme_path = template_cfg.get("readme_path")
    if not readme_path:
        return ""
    project_root = Path(__file__).resolve().parents[1]
    candidates = [
        (config_dir / readme_path).resolve(),
        (config_dir.parent / readme_path).resolve(),
        (Path.cwd() / readme_path).resolve(),
        (project_root / readme_path).resolve(),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(f"readme_path not found: {readme_path}")


def s3_endpoint_for_datacenter(data_center_id: str) -> str | None:
    return S3_ENDPOINTS.get(data_center_id)


def build_template_payload(
    config_dir: Path,
    full_config: dict[str, Any],
    container_registry_auth_id: str | None,
) -> dict[str, Any]:
    template_cfg = full_config["template"]
    env_map = dict(template_cfg.get("env", {}))
    image_name = template_cfg["image_name"]

    if "your-github-user" in image_name or "your-repo" in image_name:
        raise RuntimeError("template.image_name still contains placeholder values")

    for env_name, secret_name in template_cfg.get("secret_env", {}).items():
        env_map[env_name] = f"{{{{ RUNPOD_SECRET_{secret_name} }}}}"

    payload = {
        "name": template_cfg["name"],
        "imageName": image_name,
        "isServerless": True,
        "containerDiskInGb": template_cfg.get("container_disk_gb", 20),
        "dockerEntrypoint": template_cfg.get("docker_entrypoint", []),
        "dockerStartCmd": template_cfg.get("docker_start_cmd", []),
        "env": env_map,
        "ports": template_cfg.get("ports", []),
        "readme": read_readme(config_dir, template_cfg),
        "volumeInGb": 0,
    }
    if container_registry_auth_id:
        payload["containerRegistryAuthId"] = container_registry_auth_id
    return payload


def build_endpoint_payload(
    template_id: str,
    full_config: dict[str, Any],
    network_volume: dict[str, Any] | None,
) -> dict[str, Any]:
    endpoint_cfg = full_config["endpoint"]
    network_volume_id = network_volume["id"] if network_volume else None
    network_volume_dc = network_volume.get("dataCenterId") if network_volume else None
    data_center_ids = endpoint_cfg.get("data_center_ids", [])

    if network_volume_id and network_volume_dc:
        if data_center_ids and any(item != network_volume_dc for item in data_center_ids):
            raise RuntimeError(
                "endpoint.data_center_ids must match the network volume data center when a network volume is attached: "
                f"{network_volume_dc}"
            )
        if not data_center_ids:
            data_center_ids = [network_volume_dc]

    payload: dict[str, Any] = {
        "name": endpoint_cfg["name"],
        "templateId": template_id,
        "computeType": endpoint_cfg.get("compute_type", "GPU"),
        "idleTimeout": endpoint_cfg.get("idle_timeout", 5),
        "executionTimeoutMs": endpoint_cfg.get("execution_timeout_ms", 900000),
        "flashboot": endpoint_cfg.get("flashboot", True),
        "scalerType": endpoint_cfg.get("scaler_type", "QUEUE_DELAY"),
        "scalerValue": endpoint_cfg.get("scaler_value", 4),
        "workersMin": endpoint_cfg.get("workers_min", 0),
        "workersMax": endpoint_cfg.get("workers_max", 3),
        "dataCenterIds": data_center_ids,
    }

    if payload["computeType"] == "GPU":
        payload["gpuCount"] = endpoint_cfg.get("gpu_count", 1)
        payload["gpuTypeIds"] = endpoint_cfg["gpu_type_ids"]
        if endpoint_cfg.get("allowed_cuda_versions"):
            payload["allowedCudaVersions"] = endpoint_cfg["allowed_cuda_versions"]
    else:
        payload["cpuFlavorIds"] = endpoint_cfg["cpu_flavor_ids"]
        payload["vcpuCount"] = endpoint_cfg.get("vcpu_count", 2)

    if network_volume_id:
        payload["networkVolumeId"] = network_volume_id

    return payload


def ensure_secrets(client: RunpodClient, full_config: dict[str, Any]) -> None:
    for secret_name, secret_cfg in full_config.get("secrets", {}).items():
        source_env = secret_cfg["env_var"]
        secret_value = os.environ.get(source_env)
        if not secret_value:
            raise RuntimeError(f"missing required environment variable for secret '{secret_name}': {source_env}")
        client.ensure_secret(secret_name, secret_value)


def ensure_container_registry_auth(client: RunpodClient, full_config: dict[str, Any]) -> str | None:
    auth_cfg = full_config.get("container_registry_auth")
    if not auth_cfg or not auth_cfg.get("enabled"):
        return None

    if auth_cfg.get("id"):
        return auth_cfg["id"]

    existing = next((item for item in client.list_container_registry_auths() if item["name"] == auth_cfg["name"]), None)
    if existing:
        print(f"[runpod] container registry auth exists: {existing['name']} ({existing['id']})")
        return existing["id"]

    username_env = auth_cfg["username_env"]
    password_env = auth_cfg["password_env"]
    username = os.environ.get(username_env)
    password = os.environ.get(password_env)
    if not username or not password:
        raise RuntimeError(
            "missing required environment variables for container registry auth: "
            f"{username_env}, {password_env}"
        )

    created = client.create_container_registry_auth(auth_cfg["name"], username, password)
    print(f"[runpod] container registry auth created: {auth_cfg['name']} ({created['id']})")
    return created["id"]


def ensure_network_volume(client: RunpodClient, full_config: dict[str, Any]) -> dict[str, Any] | None:
    volume_cfg = full_config.get("network_volume")
    if not volume_cfg or not volume_cfg.get("enabled"):
        network_volume_id = full_config.get("endpoint", {}).get("network_volume_id")
        if not network_volume_id:
            return None
        return client.get_network_volume(network_volume_id)

    if volume_cfg.get("id"):
        return client.get_network_volume(volume_cfg["id"])

    existing = next((item for item in client.list_network_volumes() if item["name"] == volume_cfg["name"]), None)
    if existing:
        print(f"[runpod] network volume exists: {existing['name']} ({existing['id']})")
        return existing

    payload = {
        "name": volume_cfg["name"],
        "size": volume_cfg["size"],
        "dataCenterId": volume_cfg["data_center_id"],
    }
    created = client.rest("POST", "/networkvolumes", payload)
    print(f"[runpod] network volume created: {volume_cfg['name']} ({created['id']})")
    return created


def upsert_template(client: RunpodClient, payload: dict[str, Any]) -> dict[str, Any]:
    existing = next((item for item in client.list_templates() if item["name"] == payload["name"]), None)
    if existing:
        template_id = existing["id"]
        updated = client.rest("PATCH", f"/templates/{template_id}", payload)
        print(f"[runpod] template updated: {payload['name']} ({template_id})")
        return updated

    created = client.rest("POST", "/templates", payload)
    print(f"[runpod] template created: {payload['name']} ({created['id']})")
    return created


def upsert_endpoint(client: RunpodClient, payload: dict[str, Any]) -> dict[str, Any]:
    existing = next((item for item in client.list_endpoints() if item["name"] == payload["name"]), None)
    if existing:
        endpoint_id = existing["id"]
        updated = client.rest("PATCH", f"/endpoints/{endpoint_id}", payload)
        print(f"[runpod] endpoint updated: {payload['name']} ({endpoint_id})")
        return updated

    created = client.rest("POST", "/endpoints", payload)
    print(f"[runpod] endpoint created: {payload['name']} ({created['id']})")
    return created


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to deploy JSON config")
    args = parser.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        raise RuntimeError("set RUNPOD_API_KEY before running this script")

    config_path = Path(args.config).resolve()
    config_dir = config_path.parent
    full_config = load_json(config_path)
    client = RunpodClient(api_key=api_key)

    ensure_secrets(client, full_config)
    container_registry_auth_id = ensure_container_registry_auth(client, full_config)
    network_volume = ensure_network_volume(client, full_config)

    template_payload = build_template_payload(config_dir, full_config, container_registry_auth_id)
    template = upsert_template(client, template_payload)

    endpoint_payload = build_endpoint_payload(template["id"], full_config, network_volume)
    endpoint = upsert_endpoint(client, endpoint_payload)

    network_volume_s3_endpoint = None
    if network_volume:
        network_volume_s3_endpoint = s3_endpoint_for_datacenter(network_volume.get("dataCenterId", ""))

    output = {
        "template_id": template["id"],
        "endpoint_id": endpoint["id"],
        "endpoint_url": f"https://api.runpod.ai/v2/{endpoint['id']}",
        "network_volume_id": network_volume["id"] if network_volume else None,
        "network_volume_data_center_id": network_volume.get("dataCenterId") if network_volume else None,
        "network_volume_s3_endpoint": network_volume_s3_endpoint,
        "container_registry_auth_id": container_registry_auth_id,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[runpod] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
