from __future__ import annotations

import base64
import tempfile
import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path
from unittest.mock import patch

from app.dashboard import DashboardApplication, DashboardServer, _read_token


class DashboardSecurityTests(unittest.TestCase):
    def _app(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = Path(__file__).resolve().parents[1] / "config.example.json"
        config = root / "config.json"
        config.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return DashboardApplication(config, root / "dashboard.sqlite3", "a" * 32)

    def test_basic_auth_uses_fixed_admin_user_and_token(self) -> None:
        app = self._app()
        valid = base64.b64encode(("admin:" + "a" * 32).encode()).decode()
        wrong_user = base64.b64encode(("root:" + "a" * 32).encode()).decode()
        self.assertTrue(app.authorized("Basic " + valid))
        self.assertFalse(app.authorized("Basic " + wrong_user))
        self.assertFalse(app.authorized("Bearer " + "a" * 32))
        self.assertEqual(len(app.csrf_token), 64)

    def test_public_health_is_minimal_and_bridge_failure_does_not_break_paper(self) -> None:
        app = self._app()
        server = DashboardServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        with urlopen(base + "/health", timeout=2) as response:
            self.assertEqual(json.loads(response.read()), {"status": "ok"})

        credential = base64.b64encode(("admin:" + "a" * 32).encode()).decode()
        headers = {"Authorization": "Basic " + credential}
        with self.assertRaises(HTTPError) as unavailable:
            urlopen(Request(base + "/api/v1/testnet/balance", headers=headers), timeout=2)
        self.assertEqual(unavailable.exception.code, 503)
        unavailable_payload = json.loads(unavailable.exception.read())
        self.assertTrue(unavailable_payload["paper_available"])
        self.assertFalse(unavailable_payload["orders_enabled"])

        with urlopen(Request(base + "/api/v1/capabilities", headers=headers), timeout=2) as response:
            capabilities = json.loads(response.read())
        self.assertTrue(capabilities["paper_only"])
        self.assertFalse(capabilities["orders_enabled"])
        self.assertFalse(capabilities["bridge"]["enabled"])

    def test_static_ui_preserves_paper_testnet_boundary_and_accessibility_contract(self) -> None:
        root = Path(__file__).resolve().parents[1] / "app" / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        javascript = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "app.css").read_text(encoding="utf-8")
        for marker in (
            "PAPER ONLY",
            "ORDERS_ENABLED = FALSE",
            "NÃO ENVIADAS À BINANCE",
            "Conta virtual separada do patrimônio PAPER",
            "Não é recomendação de compra ou venda",
            'aria-controls="primaryNavigation"',
            'aria-describedby="chartSummary"',
            'aria-live="polite"',
        ):
            self.assertIn(marker, html)
        self.assertIn("syncNavigation", javascript)
        self.assertIn("aria-pressed", javascript)
        self.assertIn('aria-current="page"', html)
        self.assertIn("setAttribute('aria-current','page')", javascript)
        self.assertIn('id="results"', html)
        self.assertIn('id="trends"', html)
        self.assertIn("asset_performance", javascript)
        self.assertIn("eligible_ranked", javascript)
        self.assertIn("Promise.allSettled", javascript)
        self.assertIn("async function loadAnalytics()", javascript)
        self.assertIn("async function loadOverviewPositions()", javascript)
        self.assertNotIn("async function loadOverview(){const [", javascript)
        self.assertIn("prefers-reduced-motion", css)
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn("/api/v1/testnet/order", javascript)

    def test_setup_exposes_only_predefined_actions(self) -> None:
        app = self._app()
        setup = app.setup()
        self.assertEqual(setup["allowed_actions"], ["observe", "paper", "manual_analysis", "pause", "resume", "export"])
        self.assertIn("shell", setup["forbidden_actions"])
        self.assertFalse(setup["orders_enabled"])
        self.assertEqual(setup["configuration"]["strategy_version"], "monthly-top20-trend-cross-v0.5-crypto-only")

    def test_single_flight_rejects_second_cycle(self) -> None:
        app = self._app()
        entered = threading.Event()
        release = threading.Event()

        def fake_cycle(config, db, *, paper):
            entered.set()
            release.wait(2)
            return {"mode": "PAPER", "open_positions": [], "entry_candidates": []}

        with patch("app.dashboard.run_analysis_cycle", side_effect=fake_cycle):
            self.assertTrue(app.start_cycle("paper"))
            self.assertTrue(entered.wait(1))
            self.assertFalse(app.start_cycle("observe"))
            release.set()
            for _ in range(50):
                if not app.job_status()["running"]:
                    break
                time.sleep(0.01)
            self.assertFalse(app.job_status()["running"])

    def test_http_requires_auth_and_csrf_for_state_changes(self) -> None:
        app = self._app()
        server = DashboardServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with self.assertRaises(HTTPError) as unauthorized:
            urlopen(base + "/api/setup", timeout=2)
        self.assertEqual(unauthorized.exception.code, 401)
        self.assertEqual(
            unauthorized.exception.headers["Strict-Transport-Security"],
            "max-age=31536000; includeSubDomains",
        )

        credential = base64.b64encode(("admin:" + "a" * 32).encode()).decode()
        headers = {"Authorization": "Basic " + credential, "Content-Type": "application/json"}
        request = Request(base + "/api/actions/pause", data=b'{}', headers=headers, method="POST")
        with self.assertRaises(HTTPError) as forbidden:
            urlopen(request, timeout=2)
        self.assertEqual(forbidden.exception.code, 403)

        headers["X-CSRF-Token"] = app.csrf_token
        request = Request(
            base + "/api/actions/pause",
            data=json.dumps({"reason": "test"}).encode(),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["paused"])

    def test_modern_ui_capabilities_and_bridge_balance_are_authenticated(self) -> None:
        class FakeBridge:
            def balance(self):
                return {
                    "environment": "binance-spot-testnet",
                    "read_only": True,
                    "orders_enabled": False,
                    "balances": [{"asset": "USDT", "free": "1000", "locked": "0"}],
                }

        app = self._app()
        app.bridge_client = FakeBridge()
        server = DashboardServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        credential = base64.b64encode(("admin:" + "a" * 32).encode()).decode()
        headers = {"Authorization": "Basic " + credential}

        for path in ("/", "/assets/app.css", "/assets/app.js", "/api/v1/capabilities", "/api/v1/testnet/balance"):
            with urlopen(Request(base + path, headers=headers), timeout=2) as response:
                raw = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertTrue(raw)

        with urlopen(Request(base + "/api/v1/capabilities", headers=headers), timeout=2) as response:
            capabilities = json.loads(response.read())
        self.assertTrue(capabilities["paper_only"])
        self.assertFalse(capabilities["orders_enabled"])
        self.assertTrue(capabilities["bridge"]["read_only"])

        request = Request(
            base + "/api/v1/testnet/order",
            data=b'{}',
            headers={**headers, "Content-Type": "application/json", "X-CSRF-Token": app.csrf_token},
            method="POST",
        )
        with self.assertRaises(HTTPError) as missing_order_route:
            urlopen(request, timeout=2)
        self.assertEqual(missing_order_route.exception.code, 404)

    def test_invalid_or_duplicate_query_parameters_fail_closed(self) -> None:
        app = self._app()
        server = DashboardServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        credential = base64.b64encode(("admin:" + "a" * 32).encode()).decode()
        headers = {"Authorization": "Basic " + credential}
        for suffix in ("?unexpected=1", "?period=day&period=week"):
            with self.assertRaises(HTTPError) as invalid:
                urlopen(Request(base + "/api/analytics" + suffix, headers=headers), timeout=2)
            self.assertEqual(invalid.exception.code, 400)

    def test_token_file_must_be_long_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("short", encoding="utf-8")
            with self.assertRaises(ValueError):
                _read_token(path)
            path.write_text("z" * 32, encoding="utf-8")
            self.assertEqual(_read_token(path), "z" * 32)


if __name__ == "__main__":
    unittest.main()
