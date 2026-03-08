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
TEMPLATE_UPDATE_KEYS = {
    "containerDiskInGb",
    "containerRegistryAuthId",
    "dockerEntrypoint",
    "dockerStartCmd",
    "env",
    "imageName",
    "isPublic",
    "name",
    "ports",
    "readme",
    "volumeInGb",
    "volumeMountPath",
}
ENDPOINT_UPDATE_KEYS = {
    "allowedCudaVersions",
    "cpuFlavorIds",
    "dataCenterIds",
    "executionTimeoutMs",
    "flashboot",
    "gpuCount",
    "gpuTypeIds",
    "idleTimeout",
    "minCudaVersion",
    "name",
    "networkVolumeId",
    "networkVolumeIds",
    "scalerType",
    "scalerValue",
    "templateId",
    "vcpuCount",
    "workersMax",
    "workersMin",
}
GRAPHQL_ENDPOINT_QUERY = """
query {
  myself {
    endpoints {
      id
      name
      templateId
      gpuIds
      workersMin
      workersMax
      idleTimeout
      locations
      scalerType
      scalerValue
      networkVolumeId
      networkVolumeIds {
        networkVolumeId
        dataCenterId
      }
      gpuCount
      instanceIds
      workersPFBTarget
      allowedCudaVersions
      minCudaVersion
      executionTimeoutMs
      flashBootType
      flashEnvironmentId
      type
      modelReferences
    }
    clientBalance
  }
}
"""
GRAPHQL_SAVE_ENDPOINT_MUTATION = """
mutation SaveEndpoint($input: EndpointInput!) {
  saveEndpoint(input: $input) {
    id
    name
    templateId
    gpuIds
    workersMin
    workersMax
    idleTimeout
    locations
    scalerType
    scalerValue
    networkVolumeId
    networkVolumeIds {
      networkVolumeId
      dataCenterId
    }
    gpuCount
    allowedCudaVersions
    minCudaVersion
    executionTimeoutMs
    flashBootType
    flashEnvironmentId
    type
    modelReferences
  }
}
"""


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
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"REST {method} {path} failed with {response.status_code}: {response.text}"
            ) from exc
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

    def list_graphql_endpoints(self) -> list[dict[str, Any]]:
        data = self.graphql(GRAPHQL_ENDPOINT_QUERY)
        return data["myself"]["endpoints"]

    def get_client_balance(self) -> float:
        data = self.graphql("query { myself { clientBalance } }")
        return float(data["myself"]["clientBalance"])

    def save_graphql_endpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.graphql(GRAPHQL_SAVE_ENDPOINT_MUTATION, {"input": payload})
        return data["saveEndpoint"]

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


def normalize_network_volume_configs(full_config: dict[str, Any]) -> list[dict[str, Any]]:
    multi_cfg = full_config.get("network_volumes")
    if multi_cfg:
        if isinstance(multi_cfg, dict):
            if not multi_cfg.get("enabled", True):
                return []
            volume_cfgs = multi_cfg.get("volumes", [])
        elif isinstance(multi_cfg, list):
            volume_cfgs = multi_cfg
        else:
            raise RuntimeError("network_volumes must be either an object or an array")
        if not isinstance(volume_cfgs, list):
            raise RuntimeError("network_volumes.volumes must be an array")
        return volume_cfgs

    single_cfg = full_config.get("network_volume")
    if single_cfg:
        if not single_cfg.get("enabled"):
            return []
        return [single_cfg]

    endpoint_cfg = full_config.get("endpoint", {})
    network_volume_ids = endpoint_cfg.get("network_volume_ids", [])
    if network_volume_ids:
        if not isinstance(network_volume_ids, list):
            raise RuntimeError("endpoint.network_volume_ids must be an array")
        return [{"id": item} for item in network_volume_ids]

    network_volume_id = endpoint_cfg.get("network_volume_id")
    if network_volume_id:
        return [{"id": network_volume_id}]

    return []

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
    network_volumes: list[dict[str, Any]],
) -> dict[str, Any]:
    endpoint_cfg = full_config["endpoint"]
    network_volume_ids = [item["id"] for item in network_volumes]
    network_volume_dcs = [item["dataCenterId"] for item in network_volumes]
    data_center_ids = endpoint_cfg.get("data_center_ids", [])

    if len(set(network_volume_dcs)) != len(network_volume_dcs):
        raise RuntimeError("each attached network volume must belong to a distinct data center")

    if network_volume_ids:
        if data_center_ids and set(data_center_ids) != set(network_volume_dcs):
            raise RuntimeError(
                "endpoint.data_center_ids must match the attached network volume data centers when network volumes are attached: "
                f"{', '.join(sorted(network_volume_dcs))}"
            )
        if not data_center_ids:
            data_center_ids = network_volume_dcs.copy()

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

    if len(network_volume_ids) == 1:
        payload["networkVolumeId"] = network_volume_ids[0]
    elif len(network_volume_ids) > 1:
        raise RuntimeError("build_endpoint_payload only supports REST/single-volume endpoint creation")

    return payload


def pick_update_payload(payload: dict[str, Any], allowed_keys: set[str]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key in allowed_keys}


def normalize_endpoint_name(name: str) -> str:
    return name.removesuffix(" -fb")


def parse_locations(locations: str | None) -> list[str]:
    if not locations:
        return []
    return [item.strip() for item in locations.split(",") if item.strip()]


def endpoint_output_record(endpoint: dict[str, Any]) -> dict[str, Any]:
    endpoint_type = endpoint.get("type") or "QB"
    if endpoint_type == "LB":
        url = f"https://{endpoint['id']}.api.runpod.ai"
    else:
        url = f"https://api.runpod.ai/v2/{endpoint['id']}"
    return {
        "id": endpoint["id"],
        "name": endpoint["name"],
        "url": url,
        "data_center_ids": parse_locations(endpoint.get("locations")),
        "type": endpoint_type,
    }


def select_graphql_endpoint(client: RunpodClient, name: str) -> dict[str, Any] | None:
    expected_name = normalize_endpoint_name(name)
    return next(
        (item for item in client.list_graphql_endpoints() if normalize_endpoint_name(item["name"]) == expected_name),
        None,
    )


def build_graphql_endpoint_input(
    existing_endpoint: dict[str, Any],
    template_id: str,
    full_config: dict[str, Any],
    network_volumes: list[dict[str, Any]],
) -> dict[str, Any]:
    endpoint_cfg = full_config["endpoint"]
    network_volume_dcs = [item["dataCenterId"] for item in network_volumes]
    data_center_ids = endpoint_cfg.get("data_center_ids", [])

    if len(set(network_volume_dcs)) != len(network_volume_dcs):
        raise RuntimeError("each attached network volume must belong to a distinct data center")

    if network_volume_dcs and data_center_ids and set(data_center_ids) != set(network_volume_dcs):
        raise RuntimeError(
            "endpoint.data_center_ids must match the attached network volume data centers when network volumes are attached: "
            f"{', '.join(sorted(network_volume_dcs))}"
        )
    if network_volume_dcs and not data_center_ids:
        data_center_ids = network_volume_dcs.copy()
    elif not data_center_ids and existing_endpoint.get("locations"):
        data_center_ids = parse_locations(existing_endpoint.get("locations"))

    gpu_ids = existing_endpoint.get("gpuIds")
    if not gpu_ids:
        raise RuntimeError("failed to resolve GraphQL gpuIds for native multi-region endpoint")

    flash_boot_type = endpoint_cfg.get("flash_boot_type")
    if not flash_boot_type:
        flash_boot_type = "FLASHBOOT" if endpoint_cfg.get("flashboot", True) else "OFF"

    payload: dict[str, Any] = {
        "id": existing_endpoint["id"],
        "name": endpoint_cfg["name"],
        "templateId": template_id,
        "gpuIds": gpu_ids,
        "workersMin": endpoint_cfg.get("workers_min", 0),
        "workersMax": endpoint_cfg.get("workers_max", 3),
        "idleTimeout": endpoint_cfg.get("idle_timeout", 5),
        "locations": ",".join(data_center_ids),
        "scalerType": endpoint_cfg.get("scaler_type", "QUEUE_DELAY"),
        "scalerValue": endpoint_cfg.get("scaler_value", 4),
        "gpuCount": endpoint_cfg.get("gpu_count", 1),
        "executionTimeoutMs": endpoint_cfg.get("execution_timeout_ms", 900000),
        "flashBootType": flash_boot_type,
        "bindEndpoint": endpoint_cfg.get("bind_endpoint", True),
        "type": endpoint_cfg.get("type") or existing_endpoint.get("type") or "QB",
    }
    if network_volumes:
        payload["networkVolumeIds"] = [{"networkVolumeId": item["id"]} for item in network_volumes]

    allowed_cuda_versions = endpoint_cfg.get("allowed_cuda_versions")
    if allowed_cuda_versions:
        payload["allowedCudaVersions"] = ",".join(allowed_cuda_versions)
    elif existing_endpoint.get("allowedCudaVersions"):
        payload["allowedCudaVersions"] = existing_endpoint["allowedCudaVersions"]

    min_cuda_version = endpoint_cfg.get("min_cuda_version")
    if min_cuda_version:
        payload["minCudaVersion"] = min_cuda_version
    elif existing_endpoint.get("minCudaVersion"):
        payload["minCudaVersion"] = existing_endpoint["minCudaVersion"]

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


def ensure_network_volume(client: RunpodClient, volume_cfg: dict[str, Any]) -> dict[str, Any]:
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


def ensure_network_volumes(client: RunpodClient, full_config: dict[str, Any]) -> list[dict[str, Any]]:
    volume_cfgs = normalize_network_volume_configs(full_config)
    if not volume_cfgs:
        return []

    ensured = [ensure_network_volume(client, volume_cfg) for volume_cfg in volume_cfgs]
    data_center_ids = [item["dataCenterId"] for item in ensured]
    if len(set(data_center_ids)) != len(data_center_ids):
        raise RuntimeError("network volumes must be unique per data center")
    return ensured


def upsert_template(client: RunpodClient, payload: dict[str, Any]) -> dict[str, Any]:
    existing = next((item for item in client.list_templates() if item["name"] == payload["name"]), None)
    if existing:
        template_id = existing["id"]
        updated = client.rest("PATCH", f"/templates/{template_id}", pick_update_payload(payload, TEMPLATE_UPDATE_KEYS))
        print(f"[runpod] template updated: {payload['name']} ({template_id})")
        return updated

    created = client.rest("POST", "/templates", payload)
    print(f"[runpod] template created: {payload['name']} ({created['id']})")
    return created


def upsert_endpoint(client: RunpodClient, payload: dict[str, Any]) -> dict[str, Any]:
    expected_name = normalize_endpoint_name(payload["name"])
    existing = next(
        (item for item in client.list_endpoints() if normalize_endpoint_name(item["name"]) == expected_name),
        None,
    )
    if existing:
        endpoint_id = existing["id"]
        updated = client.rest("PATCH", f"/endpoints/{endpoint_id}", pick_update_payload(payload, ENDPOINT_UPDATE_KEYS))
        print(f"[runpod] endpoint updated: {payload['name']} ({endpoint_id})")
        return updated

    created = client.rest("POST", "/endpoints", payload)
    print(f"[runpod] endpoint created: {payload['name']} ({created['id']})")
    return created


def upsert_graphql_endpoint(
    client: RunpodClient,
    template_id: str,
    full_config: dict[str, Any],
    network_volumes: list[dict[str, Any]],
) -> dict[str, Any]:
    endpoint_name = full_config["endpoint"]["name"]
    existing_graphql = select_graphql_endpoint(client, endpoint_name)

    if not existing_graphql:
        bootstrap_config = json.loads(json.dumps(full_config))
        bootstrap_config["endpoint"]["data_center_ids"] = [network_volumes[0]["dataCenterId"]]
        bootstrap_endpoint = upsert_endpoint(
            client,
            build_endpoint_payload(template_id, bootstrap_config, [network_volumes[0]]),
        )
        existing_graphql = select_graphql_endpoint(client, bootstrap_endpoint["name"])
        if not existing_graphql:
            raise RuntimeError("failed to bootstrap native multi-region endpoint via REST before GraphQL update")

    graphql_payload = build_graphql_endpoint_input(existing_graphql, template_id, full_config, network_volumes)
    saved = client.save_graphql_endpoint(graphql_payload)
    print(f"[runpod] endpoint updated via GraphQL: {saved['name']} ({saved['id']})")
    return saved


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
    network_volumes = ensure_network_volumes(client, full_config)

    template_payload = build_template_payload(config_dir, full_config, container_registry_auth_id)
    template = upsert_template(client, template_payload)
    multi_region_mode = full_config.get("endpoint", {}).get("multi_region_mode", "native")
    endpoint_type = full_config.get("endpoint", {}).get("type", "QB")

    if endpoint_type != "QB" or (len(network_volumes) > 1 and multi_region_mode == "native"):
        endpoints = [upsert_graphql_endpoint(client, template["id"], full_config, network_volumes)]
    else:
        endpoint_payload = build_endpoint_payload(template["id"], full_config, network_volumes)
        endpoints = [upsert_endpoint(client, endpoint_payload)]

    network_volume_s3_endpoints = [
        {
            "id": item["id"],
            "data_center_id": item["dataCenterId"],
            "endpoint_url": s3_endpoint_for_datacenter(item["dataCenterId"]),
        }
        for item in network_volumes
    ]

    network_volume = network_volumes[0] if len(network_volumes) == 1 else None
    endpoint = endpoints[0] if len(endpoints) == 1 else None
    client_balance = client.get_client_balance()

    output = {
        "template_id": template["id"],
        "endpoint_id": endpoint["id"] if endpoint else None,
        "endpoint_url": endpoint_output_record(endpoint)["url"] if endpoint else None,
        "network_volume_id": network_volume["id"] if network_volume else None,
        "network_volume_data_center_id": network_volume.get("dataCenterId") if network_volume else None,
        "network_volume_s3_endpoint": (
            s3_endpoint_for_datacenter(network_volume.get("dataCenterId", "")) if network_volume else None
        ),
        "endpoints": [endpoint_output_record(item) for item in endpoints],
        "network_volume_ids": [item["id"] for item in network_volumes],
        "network_volume_data_center_ids": [item["dataCenterId"] for item in network_volumes],
        "network_volume_s3_endpoints": network_volume_s3_endpoints,
        "container_registry_auth_id": container_registry_auth_id,
        "client_balance_usd": client_balance,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[runpod] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
