#!/usr/bin/env python3
"""Fail-closed, secret-safe preflight for the unified non-production deployment."""

from __future__ import annotations

import argparse
import os
import re
import stat
from pathlib import Path

_IMAGE_BY_DIGEST = re.compile(r"^[^\s@:]+(?:/[^\s@:]+)+(?::[^\s@]+)?@sha256:[0-9a-f]{64}$")
_NETWORK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REQUIRED_SECRETS = {
    "dashboard_auth_token": 24,
    "bridge_auth_token": 32,
    "binance_testnet_api_key": 1,
    "binance_testnet_secret_key": 1,
}


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _validate_image(name: str) -> str:
    value = _required_environment(name)
    if not _IMAGE_BY_DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be an immutable repository@sha256 reference")
    return value


def _read_secret(path: Path, minimum_length: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"secret file {path.name} must be a regular non-symlink file")
    info = path.stat()
    if info.st_size > 4096:
        raise ValueError(f"secret file {path.name} is too large")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f"secret file {path.name} must not be accessible by group or others")
    value = path.read_bytes().rstrip(b"\r\n")
    if len(value) < minimum_length or b"\x00" in value:
        raise ValueError(f"secret file {path.name} does not satisfy its contract")
    return value


def _validate_secrets(directory: Path) -> None:
    values = {
        name: _read_secret(directory / name, minimum)
        for name, minimum in _REQUIRED_SECRETS.items()
    }
    if len(set(values.values())) != len(values):
        raise ValueError("deployment secrets must all be distinct")
    optional = [directory / "openai_api_key", directory / "openai_model"]
    if optional[0].exists() != optional[1].exists():
        raise ValueError("OpenAI secret files must be configured together")
    for path in optional:
        if path.exists():
            _read_secret(path, 1)


def _yaml_block(text: str, name: str, indent: int) -> str:
    """Extract one mapping block from the deliberately small deployment Compose."""
    lines = text.splitlines()
    header = " " * indent + name + ":"
    matches = [index for index, line in enumerate(lines) if line == header]
    if not matches:
        raise ValueError(f"unified Compose is missing {name}")
    if len(matches) != 1:
        raise ValueError(f"unified Compose contains duplicate {name} mappings")
    start = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        current_indent = len(line) - len(line.lstrip(" "))
        if current_indent <= indent:
            end = index
            break
    return "\n".join(lines[start + 1:end])


def _list_items(block: str, name: str, indent: int) -> set[str]:
    values = _yaml_block(block, name, indent)
    return {
        match.group(1)
        for line in values.splitlines()
        if (match := re.fullmatch(r"\s*-\s+([A-Za-z0-9_.-]+)\s*", line))
    }


def _direct_values(block: str, name: str, indent: int) -> list[str]:
    pattern = re.compile(rf"^{' ' * indent}{re.escape(name)}:\s*(.*?)\s*$")
    return [match.group(1) for line in block.splitlines() if (match := pattern.fullmatch(line))]


def _one_direct_value(block: str, name: str, indent: int) -> str:
    values = _direct_values(block, name, indent)
    if len(values) != 1:
        raise ValueError(f"unified Compose must define exactly one direct {name} value")
    return values[0]


def _validate_sources(root: Path) -> None:
    raw_compose = (root / "compose.unified.yml").read_text(encoding="utf-8")
    compose = "\n".join(line.split("#", 1)[0].rstrip() for line in raw_compose.splitlines())
    dockerfile = (root / "Dockerfile.dashboard").read_text(encoding="utf-8")
    services = _yaml_block(compose, "services", 0)
    dashboard = _yaml_block(services, "paper-dashboard", 2)
    bridge = _yaml_block(services, "binance-testnet-bridge", 2)
    networks = _yaml_block(compose, "networks", 0)
    secret_definitions = _yaml_block(compose, "secrets", 0)
    proxy_network = _yaml_block(networks, "proxy", 2)
    private_network = _yaml_block(networks, "bridge_private", 2)
    if _direct_values(networks, "bridge_egress", 2) != ["{}"]:
        raise ValueError("bridge_egress must be an explicit deployment network")

    if re.search(r"(?m)^\s+(?:build|ports):", services):
        raise ValueError("unified Compose must use published images and expose no host ports")
    dashboard_image = _one_direct_value(dashboard, "image", 4)
    bridge_image = _one_direct_value(bridge, "image", 4)
    if not re.fullmatch(r"\$\{PAPER_DASHBOARD_IMAGE:\?[^}]+}", dashboard_image):
        raise ValueError("dashboard image must be immutable and explicitly configured")
    if not re.fullmatch(r"\$\{BINANCE_TESTNET_BRIDGE_IMAGE:\?[^}]+}", bridge_image):
        raise ValueError("Bridge image must be immutable and explicitly configured")
    if _list_items(dashboard, "secrets", 4) != {"dashboard_auth_token", "bridge_auth_token"}:
        raise ValueError("dashboard secret boundary is invalid")
    if _list_items(bridge, "secrets", 4) != {
        "bridge_auth_token", "binance_testnet_api_key", "binance_testnet_secret_key"
    }:
        raise ValueError("Bridge secret boundary is invalid")
    if _list_items(dashboard, "networks", 4) != {"proxy", "bridge_private"}:
        raise ValueError("dashboard network boundary is invalid")
    if _list_items(bridge, "networks", 4) != {"bridge_private", "bridge_egress"}:
        raise ValueError("Bridge network boundary is invalid")
    expected_secret_files = {
        "dashboard_auth_token": "./secrets/dashboard_auth_token",
        "bridge_auth_token": "./secrets/bridge_auth_token",
        "binance_testnet_api_key": "./secrets/binance_testnet_api_key",
        "binance_testnet_secret_key": "./secrets/binance_testnet_secret_key",
    }
    defined_secret_names = {
        match.group(1)
        for line in secret_definitions.splitlines()
        if (match := re.fullmatch(r"  ([A-Za-z0-9_.-]+):\s*", line))
    }
    if defined_secret_names != set(expected_secret_files):
        raise ValueError("top-level Compose secret definitions are invalid")
    for name, expected_file in expected_secret_files.items():
        definition = _yaml_block(secret_definitions, name, 2)
        if _direct_values(definition, "file", 4) != [expected_file]:
            raise ValueError(f"Compose secret file binding for {name} is invalid")
    if re.search(r"binance_testnet_(?:api|secret)", dashboard):
        raise ValueError("Binance credentials must not be exposed to the dashboard")
    if _direct_values(private_network, "internal", 4) != ["true"]:
        raise ValueError("bridge_private must be internal")
    if _direct_values(proxy_network, "external", 4) != ["true"]:
        raise ValueError("proxy network must be external")
    proxy_name = _one_direct_value(proxy_network, "name", 4)
    if not re.fullmatch(r"\$\{DASHBOARD_PROXY_NETWORK:\?[^}]+}", proxy_name):
        raise ValueError("proxy network must be explicitly configured")

    first_line = dockerfile.splitlines()[0] if dockerfile else ""
    if not re.fullmatch(r"FROM python:3\.12-slim@sha256:[0-9a-f]{64}", first_line):
        raise ValueError("dashboard base image must be pinned by digest")


def _validate_secret_directory_binding(root: Path, directory: Path) -> None:
    if directory.resolve() != (root.resolve() / "secrets"):
        raise ValueError("--secrets-dir must be the Compose-bound ./secrets directory")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate unified deployment inputs without printing secrets")
    parser.add_argument("--secrets-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser


def main() -> int:
    args = _parser().parse_args()

    dashboard_image = _validate_image("PAPER_DASHBOARD_IMAGE")
    bridge_image = _validate_image("BINANCE_TESTNET_BRIDGE_IMAGE")
    if dashboard_image == bridge_image:
        raise ValueError("dashboard and Bridge images must be distinct")
    proxy_network = _required_environment("DASHBOARD_PROXY_NETWORK")
    if not _NETWORK_NAME.fullmatch(proxy_network):
        raise ValueError("DASHBOARD_PROXY_NETWORK has an invalid name")
    root = args.root.resolve()
    secrets_directory = args.secrets_dir.resolve()
    _validate_sources(root)
    _validate_secret_directory_binding(root, secrets_directory)
    _validate_secrets(secrets_directory)
    print("unified deployment preflight: ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"preflight failed: {exc}") from None
