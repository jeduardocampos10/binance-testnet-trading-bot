from __future__ import annotations

import importlib.util
import io
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
