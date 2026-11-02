import tempfile
from pathlib import Path
import time
import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mission_control.server.app import create_app
from mission_control.server.config import Settings


TOKEN = "phase-1b-test-token-strong"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = "http://127.0.0.1:8000"


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings = Settings(
            database_path=Path(self.temp.name) / "mission-control.sqlite3",
            control_token=TOKEN,
            allowed_origins=(ORIGIN,),
        )
        self.client_context = TestClient(create_app(settings))
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def create_mock(self):
        return self.client.post(
            "/api/v1/runs",
            headers=AUTH,
            json={"scenario": "baseline", "mode": "MOCK"},
        )

    def test_health_and_scenarios(self):
        self.assertEqual(self.client.get("/api/v1/health").json()["live_agents"], "NOT_READY")
        self.assertIn("baseline", self.client.get("/api/v1/scenarios").json()["scenarios"])

    def test_mutations_require_authorization(self):
        denied = self.client.post("/api/v1/runs", json={"scenario": "baseline", "mode": "MOCK"})
        self.assertEqual(denied.status_code, 401)
        created = self.create_mock()
        run_id = created.json()["run_id"]
        self.assertEqual(self.client.post(f"/api/v1/runs/{run_id}/stop").status_code, 401)

    def test_run_lifecycle_only_one_active_and_safe_stop(self):
        created = self.create_mock()
        self.assertEqual(created.status_code, 201)
        run = created.json()
        self.assertEqual((run["mode"], run["status"]), ("MOCK", "ACTIVE"))
        self.assertEqual(self.create_mock().status_code, 409)
        stopped = self.client.post(f"/api/v1/runs/{run['run_id']}/stop", headers=AUTH).json()
        stopped_again = self.client.post(f"/api/v1/runs/{run['run_id']}/stop", headers=AUTH).json()
        self.assertEqual(stopped["status"], "STOPPED")
        self.assertEqual(stopped["stopped_at"], stopped_again["stopped_at"])
        self.assertEqual(self.client.get(f"/api/v1/runs/{run['run_id']}").status_code, 200)

    def test_live_run_returns_not_ready_without_fabricated_events(self):
        response = self.client.post(
            "/api/v1/runs", headers=AUTH, json={"scenario": "baseline", "mode": "LIVE"}
        )
        self.assertEqual(response.status_code, 201)
        run = response.json()
        self.assertEqual(run["status"], "NOT_READY")
        events = self.client.get(f"/api/v1/runs/{run['run_id']}/events").json()["events"]
        self.assertEqual(events, [])

    def test_mock_events_are_non_security_and_ordered(self):
        run = self.create_mock().json()
        events = self.client.get(f"/api/v1/runs/{run['run_id']}/events").json()["events"]
        self.assertEqual([event["run_sequence"] for event in events], [1, 2])
        self.assertEqual(
            [event["event_type"] for event in events],
            ["satellite.telemetry_transmitted", "channel.rf_observed"],
        )
        self.assertTrue(all("decision" not in event for event in events))

    def test_websocket_origin_token_and_event_delivery(self):
        run = self.create_mock().json()
        url = f"/ws/v1/runs/{run['run_id']}?token={TOKEN}"
        with self.client.websocket_connect(url, headers={"origin": ORIGIN}) as websocket:
            first = websocket.receive_json()
            second = websocket.receive_json()
        self.assertEqual([first["run_sequence"], second["run_sequence"]], [1, 2])
        self.assertTrue(first["event_id"].startswith("mock:"))

        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(url, headers={"origin": "https://evil.example"}):
                pass

    def test_repeated_websocket_disconnects_release_subscribers(self):
        run = self.create_mock().json()
        url = f"/ws/v1/runs/{run['run_id']}?token={TOKEN}"
        for _ in range(12):
            with self.client.websocket_connect(url, headers={"origin": ORIGIN}) as websocket:
                websocket.receive_json()
                websocket.receive_json()
                websocket.close()
                for _ in range(100):
                    if self.client.app.state.broker.subscriber_count == 0:
                        break
                    time.sleep(0.001)
            self.assertEqual(self.client.app.state.broker.subscriber_count, 0)


if __name__ == "__main__":
    unittest.main()
