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


def _validate_sources(root: Path) -> None:
    compose = (root / "compose.unified.yml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile.dashboard").read_text(encoding="utf-8")
    required_compose_fragments = (
        "image: ${PAPER_DASHBOARD_IMAGE:",
        "image: ${BINANCE_TESTNET_BRIDGE_IMAGE:",
        "name: ${DASHBOARD_PROXY_NETWORK:",
        "bridge_private:\n    internal: true",
        "- dashboard_auth_token",
        "- bridge_auth_token",
        "- binance_testnet_api_key",
        "- binance_testnet_secret_key",
    )
    missing = [fragment for fragment in required_compose_fragments if fragment not in compose]
    if missing:
        raise ValueError("unified Compose is missing required immutable/isolation contracts")
    if "    build:" in compose or "    ports:" in compose:
        raise ValueError("unified Compose must use published images and expose no host ports")
    first_line = dockerfile.splitlines()[0] if dockerfile else ""
    if not re.fullmatch(r"FROM python:3\.12-slim@sha256:[0-9a-f]{64}", first_line):
        raise ValueError("dashboard base image must be pinned by digest")


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
    _validate_sources(args.root.resolve())
    _validate_secrets(args.secrets_dir.resolve())
    print("unified deployment preflight: ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"preflight failed: {exc}") from None
