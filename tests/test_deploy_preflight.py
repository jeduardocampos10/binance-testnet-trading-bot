from __future__ import annotations

import importlib.util
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


class UnifiedDeploymentPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        script = cls.root / "deploy" / "preflight-unified.py"
        spec = importlib.util.spec_from_file_location("unified_deploy_preflight", script)
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load deployment preflight")
        cls.preflight = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.preflight)

    def setUp(self) -> None:
        self.environment = {
            "PAPER_DASHBOARD_IMAGE": "registry.example/cashmaxx/dashboard@sha256:" + "a" * 64,
            "BINANCE_TESTNET_BRIDGE_IMAGE": "registry.example/cashmaxx/bridge@sha256:" + "b" * 64,
            "DASHBOARD_PROXY_NETWORK": "non-production-proxy",
        }

    @staticmethod
    def _secrets(directory: Path) -> None:
        values = {
            "dashboard_auth_token": "d" * 32,
            "bridge_auth_token": "r" * 40,
            "binance_testnet_api_key": "testnet-key",
            "binance_testnet_secret_key": "testnet-secret",
        }
        for name, value in values.items():
            path = directory / name
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)

    def test_accepts_digest_pinned_distinct_images_and_private_secret_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", self.environment, clear=True):
            secrets = Path(directory)
            self._secrets(secrets)
            dashboard = self.preflight._validate_image("PAPER_DASHBOARD_IMAGE")
            bridge = self.preflight._validate_image("BINANCE_TESTNET_BRIDGE_IMAGE")
            self.assertNotEqual(dashboard, bridge)
            self.preflight._validate_sources(self.root)
            self.preflight._validate_secrets(secrets)

    def test_rejects_secret_or_network_attached_to_the_wrong_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(self.root / "Dockerfile.dashboard", root / "Dockerfile.dashboard")
            compose = (self.root / "compose.unified.yml").read_text(encoding="utf-8")
            compose = compose.replace(
                "    secrets:\n      - dashboard_auth_token\n      - bridge_auth_token",
                "    secrets:\n      - dashboard_auth_token\n      - bridge_auth_token\n      - binance_testnet_api_key",
                1,
            )
            (root / "compose.unified.yml").write_text(compose, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dashboard secret boundary"):
                self.preflight._validate_sources(root)

    def test_nested_decoy_cannot_satisfy_direct_image_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(self.root / "Dockerfile.dashboard", root / "Dockerfile.dashboard")
            compose = (self.root / "compose.unified.yml").read_text(encoding="utf-8")
            compose = compose.replace(
                "    image: ${PAPER_DASHBOARD_IMAGE:?set an immutable digest-pinned dashboard image}",
                "    image: attacker/dashboard:latest\n    labels:\n      image: ${PAPER_DASHBOARD_IMAGE:?decoy}",
            )
            (root / "compose.unified.yml").write_text(compose, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dashboard image must be immutable"):
                self.preflight._validate_sources(root)

    def test_duplicate_critical_mapping_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(self.root / "Dockerfile.dashboard", root / "Dockerfile.dashboard")
            compose = (self.root / "compose.unified.yml").read_text(encoding="utf-8")
            compose = compose.replace("  bridge_private:\n    internal: true", "  bridge_private:\n    internal: true\n  bridge_private:\n    internal: false")
            (root / "compose.unified.yml").write_text(compose, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate bridge_private"):
                self.preflight._validate_sources(root)

    def test_comment_cannot_satisfy_a_missing_private_network_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(self.root / "Dockerfile.dashboard", root / "Dockerfile.dashboard")
            compose = (self.root / "compose.unified.yml").read_text(encoding="utf-8")
            compose = compose.replace("  bridge_private:\n    internal: true", "  bridge_private:\n    # internal: true")
            (root / "compose.unified.yml").write_text(compose, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "bridge_private must be internal"):
                self.preflight._validate_sources(root)

    def test_rejects_changed_top_level_secret_file_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(self.root / "Dockerfile.dashboard", root / "Dockerfile.dashboard")
            compose = (self.root / "compose.unified.yml").read_text(encoding="utf-8")
            compose = compose.replace(
                "    file: ./secrets/binance_testnet_api_key",
                "    file: ./other/unvalidated_api_key",
            )
            (root / "compose.unified.yml").write_text(compose, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "secret file binding"):
                self.preflight._validate_sources(root)

    def test_secrets_directory_must_match_the_compose_bound_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "secrets"
            self.preflight._validate_secret_directory_binding(root, expected)
            with self.assertRaisesRegex(ValueError, "Compose-bound"):
                self.preflight._validate_secret_directory_binding(root, root / "other-secrets")

    def test_cli_requires_secret_directory_for_a_complete_preflight(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.preflight._parser().parse_args([])

    def test_rejects_mutable_image_reference(self) -> None:
        environment = dict(self.environment)
        environment["PAPER_DASHBOARD_IMAGE"] = "registry.example/cashmaxx/dashboard:latest"
        with patch.dict("os.environ", environment, clear=True), self.assertRaisesRegex(ValueError, "immutable"):
            self.preflight._validate_image("PAPER_DASHBOARD_IMAGE")

    def test_rejects_reused_or_overexposed_secrets_without_printing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secrets = Path(directory)
            self._secrets(secrets)
            reused = "same-secret-value-that-must-not-appear"
            for name in ("dashboard_auth_token", "bridge_auth_token"):
                path = secrets / name
                path.write_text(reused, encoding="utf-8")
                path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "must all be distinct") as reused_error:
                self.preflight._validate_secrets(secrets)
            self.assertNotIn(reused, str(reused_error.exception))

            path = secrets / "dashboard_auth_token"
            path.write_text("d" * 32, encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "must not be accessible"):
                self.preflight._validate_secrets(secrets)


if __name__ == "__main__":
    unittest.main()
