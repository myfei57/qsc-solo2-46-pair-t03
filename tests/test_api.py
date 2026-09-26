"""Integration tests for the console routing, HTTP server and boot snapshot."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from cranefleet.console import Console, Request
from cranefleet.server import serve
from cranefleet.wiring import build_app

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def dispatch(console: Console, method: str, path: str, body: dict | None = None):  # noqa: ANN201
    request = Request(method=method, path=path, query={}, body=body or {}, body_valid=True)
    return console.dispatch(request)


class DispatchFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_app("memory", web_root=str(WEB_ROOT))
        self.console = self.app.console

    def tearDown(self) -> None:
        self.app.shutdown()

    def test_state_snapshot_shape(self) -> None:
        status, payload = dispatch(self.console, "GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        for key in ("zone", "slew", "trolley", "hoist", "wind", "limit", "cluster", "crane", "config"):
            self.assertIn(key, data)
        self.assertEqual(data["cluster"]["nearest_pair"]["pair"]["distance"], 80.0)

    def test_unknown_route(self) -> None:
        status, payload = dispatch(self.console, "GET", "/api/unknown")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_conflict_flow_blocks_then_releases_slew(self) -> None:
        dispatch(self.console, "POST", "/api/cluster/survey", {"cranes": {"crane-2": {"x": 5, "y": 0}}})
        dispatch(self.console, "POST", "/api/zone/occupy", {"crane_id": "crane-1"})
        status, payload = dispatch(self.console, "POST", "/api/zone/occupy", {"crane_id": "crane-2"})
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]["conflicts"]), 1)
        status, payload = dispatch(self.console, "POST", "/api/slew/start")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "zone latch held")
        status, payload = dispatch(self.console, "POST", "/api/zone/clear")
        self.assertEqual(status, 200)
        self.assertFalse(payload["data"]["latch"])
        status, payload = dispatch(self.console, "POST", "/api/slew/start")
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["moving"])

    def test_occupy_requires_crane_id(self) -> None:
        status, _ = dispatch(self.console, "POST", "/api/zone/occupy", {})
        self.assertEqual(status, 400)
        request = Request("POST", "/api/zone/occupy", {}, {}, body_valid=False)
        status, _ = self.console.dispatch(request)
        self.assertEqual(status, 400)

    def test_wind_alarm_and_fault_path(self) -> None:
        dispatch(self.console, "POST", "/api/slew/start")
        status, payload = dispatch(self.console, "POST", "/api/wind/alarm", {"level": 14})
        self.assertEqual(status, 200)
        self.assertFalse(payload["data"]["active"] is False)
        dispatch(self.console, "POST", "/api/wind/clear")
        dispatch(self.console, "POST", "/api/slew/fault", {"faulted": True})
        status, payload = dispatch(self.console, "POST", "/api/wind/alarm", {"level": 15})
        self.assertEqual(status, 409)
        self.assertIn("slew", payload["error"])
        dispatch(self.console, "POST", "/api/slew/fault", {"faulted": False})
        self.assertFalse(self.console.slew.is_faulted())

    def test_hoist_and_trolley_flow(self) -> None:
        dispatch(self.console, "POST", "/api/crane/command", {"source": "cab", "action": "lower"})
        status, payload = dispatch(self.console, "GET", "/api/hoist/state")
        self.assertTrue(payload["data"]["hook_lowered"])
        status, payload = dispatch(self.console, "POST", "/api/hoist/release")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "feedback not confirmed")
        dispatch(self.console, "POST", "/api/hoist/feedback", {"speed": 1.4})
        status, payload = dispatch(self.console, "POST", "/api/hoist/release")
        self.assertEqual(status, 200)
        self.assertFalse(payload["data"]["brake_engaged"])
        status, payload = dispatch(self.console, "POST", "/api/trolley/resume")
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["trolley"]["moving"])
        dispatch(self.console, "POST", "/api/hoist/payout", {"length": 3})
        status, payload = dispatch(self.console, "POST", "/api/hoist/stop")
        self.assertFalse(payload["data"]["moving"])

    def test_command_stop_priority(self) -> None:
        dispatch(self.console, "POST", "/api/slew/start")
        dispatch(self.console, "POST", "/api/crane/command", {"source": "cab", "action": "stop"})
        status, payload = dispatch(
            self.console, "POST", "/api/crane/command", {"source": "remote", "action": "swing", "angle": 30}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["action"], "stop")
        self.assertEqual(self.console.default_crane().blocked_moves_now(), ["swing"])
        dispatch(self.console, "POST", "/api/crane/reset")
        self.assertFalse(self.console.default_crane().stops_pending())

    def test_limit_check_and_target(self) -> None:
        dispatch(self.console, "POST", "/api/cluster/survey", {"cranes": {"crane-2": {"x": 0, "y": 150}}})
        status, payload = dispatch(self.console, "POST", "/api/limit/check")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["exceeded"], ["trolley"])
        self.assertIsNotNone(payload["data"]["arbitration"])
        status, payload = dispatch(self.console, "POST", "/api/slew/target", {"angle": 45})
        self.assertEqual(payload["data"]["ramp_target"], 45.0)
        self.assertEqual(status, 200)

    def test_bad_bodies_and_config(self) -> None:
        request = Request("POST", "/api/config", {}, {}, body_valid=False)
        status, _ = self.console.dispatch(request)
        self.assertEqual(status, 400)
        status, payload = dispatch(self.console, "POST", "/api/config", {"safe_separation": 0})
        self.assertEqual(status, 400)
        status, payload = dispatch(
            self.console, "POST", "/api/config", {"site_name": "华东一号", "safe_separation": 18}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["safe_separation"], 18.0)
        status, payload = dispatch(self.console, "GET", "/api/config")
        self.assertEqual(payload["data"]["site_name"], "华东一号")

    def test_survey_rejects_bad_points(self) -> None:
        status, _ = dispatch(self.console, "POST", "/api/cluster/survey", {"cranes": {"crane-1": {"x": 1}}})
        self.assertEqual(status, 400)
        status, _ = dispatch(self.console, "POST", "/api/cluster/survey", {"cranes": []})
        self.assertEqual(status, 400)

    def test_audit_query(self) -> None:
        status, payload = dispatch(self.console, "GET", "/api/audit")
        self.assertEqual(status, 200)
        self.assertTrue(any(entry["action"] == "boot" for entry in payload["data"]))
        request = Request("GET", "/api/audit", {"action": ["boot"]}, {})
        status, payload = self.console.dispatch(request)
        self.assertEqual([entry["action"] for entry in payload["data"]], ["boot"])


class HttpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_app("memory", web_root=str(WEB_ROOT))
        self.server = serve(self.app.console, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.app.shutdown()

    def _request(self, method: str, path: str, body: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type"), raw

    def test_api_over_http(self) -> None:
        status, content_type, raw = self._request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertTrue(json.loads(raw)["ok"])
        status, _, raw = self._request("POST", "/api/zone/occupy", {"crane_id": "crane-1"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["data"]["occupants"], ["crane-1"])

    def test_static_pages(self) -> None:
        status, content_type, raw = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("CraneFleet", raw.decode("utf-8"))
        status, content_type, _ = self._request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", content_type)
        status, _, _ = self._request("GET", "/missing.txt")
        self.assertEqual(status, 404)

    def test_static_traversal_is_blocked(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest("GET", "/%2e%2e/pyproject.toml")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 403)

    def test_concurrent_occupancy_registration(self) -> None:
        errors: list[Exception] = []

        def report(crane_id: str) -> None:
            try:
                status, _, _ = self._request("POST", "/api/zone/occupy", {"crane_id": crane_id})
                if status != 200:
                    errors.append(RuntimeError(f"status {status}"))
            except Exception as exc:  # pragma: no cover - surfaced via assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=report, args=("crane-1" if index % 2 == 0 else "crane-2",))
            for index in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        zone = self.app.console.zone
        from cranefleet import zone as zone_module

        self.assertEqual(sorted(zone_module.occupants_snapshot(zone)), ["crane-1", "crane-2"])
        self.assertEqual(zone_module.registration_count(zone), 8)
        self.assertEqual(zone_module.latest_registration_seq(zone), 8)


class BootSnapshotTests(unittest.TestCase):
    def test_state_survives_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = build_app(directory, web_root=str(WEB_ROOT))
            dispatch(
                first.console,
                "POST",
                "/api/cluster/survey",
                {"cranes": {"crane-2": {"x": 40, "y": 10}}},
            )
            dispatch(first.console, "POST", "/api/zone/occupy", {"crane_id": "crane-1"})
            dispatch(
                first.console,
                "POST",
                "/api/config",
                {"site_name": "华东六号", "safe_separation": 20},
            )
            first.shutdown()
            self.assertTrue((Path(directory) / "snapshot.json").is_file())

            second = build_app(directory, web_root=str(WEB_ROOT))
            self.assertTrue(second.console.zone.is_durable())
            self.assertIn("crane-1", second.console.zone.occupants)
            self.assertEqual(second.console.config.site_name, "华东六号")
            self.assertEqual(second.console.config.safe_separation, 20.0)
            baseline = second.console.cluster.baseline["crane-2"]
            self.assertAlmostEqual(baseline.x, 40.0)
            self.assertAlmostEqual(baseline.y, 10.0)
            self.assertTrue(any(entry.action == "boot" for entry in second.console.audit.snapshot()))
            self.assertTrue(any(entry.action == "shutdown" for entry in second.console.audit.snapshot()))
            second.shutdown()


if __name__ == "__main__":
    unittest.main()
