import tempfile
from pathlib import Path
import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mission_control.server.app import create_app
from mission_control.server.config import Settings


TOKEN = "phase-1c-browser-token-strong"
ORIGIN = "http://127.0.0.1:8000"


class FrontendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(
            Settings(
                database_path=Path(self.temp.name) / "frontend.sqlite3",
                control_token=TOKEN,
                allowed_origins=(ORIGIN,),
            )
        )
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def establish_browser_session(self):
        return self.client.post(
            "/api/v1/browser-session",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    def test_frontend_and_static_assets_are_served(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("GROUNDLINK GUARDIAN", page.text)
        self.assertIn('id="missionSvg"', page.text)
        for asset in ("styles.css", "app.js", "animation.js"):
            response = self.client.get(f"/static/{asset}")
            self.assertEqual(response.status_code, 200)
            self.assertGreater(len(response.content), 100)

    def test_live_and_unsupported_controls_are_disabled(self):
        page = self.client.get("/").text
        self.assertIn('id="liveModeButton" type="button" disabled', page)
        self.assertIn("AWGN 10", page)
        self.assertIn("Fading Clean", page)
        self.assertGreaterEqual(page.count("NOT IN CONTRACT"), 3)

    def test_frontend_does_not_embed_or_persist_token(self):
        source = self.client.get("/static/app.js").text
        self.assertNotIn(TOKEN, source)
        self.assertNotIn("localStorage", source)
        self.assertNotIn("sessionStorage", source)
        self.assertIn("/api/v1/browser-session", source)

    def test_browser_session_uses_httponly_cookie_and_origin(self):
        denied = self.client.post("/api/v1/browser-session")
        self.assertEqual(denied.status_code, 401)
        session = self.establish_browser_session()
        self.assertEqual(session.status_code, 204)
        cookie = session.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)

        without_origin = self.client.post(
            "/api/v1/runs", json={"scenario": "baseline", "mode": "MOCK"}
        )
        self.assertEqual(without_origin.status_code, 401)
        allowed = self.client.post(
            "/api/v1/runs",
            headers={"origin": ORIGIN},
            json={"scenario": "baseline", "mode": "MOCK"},
        )
        self.assertEqual(allowed.status_code, 201)

    def test_websocket_accepts_session_cookie_but_rejects_bad_origin(self):
        self.establish_browser_session()
        run = self.client.post(
            "/api/v1/runs",
            headers={"origin": ORIGIN},
            json={"scenario": "baseline", "mode": "MOCK"},
        ).json()
        url = f"/ws/v1/runs/{run['run_id']}"
        with self.client.websocket_connect(url, headers={"origin": ORIGIN}) as websocket:
            event = websocket.receive_json()
            self.assertEqual(event["event_type"], "satellite.telemetry_transmitted")
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(url, headers={"origin": "https://evil.example"}):
                pass

    def test_mock_contract_never_contains_station_decision(self):
        self.establish_browser_session()
        run = self.client.post(
            "/api/v1/runs",
            headers={"origin": ORIGIN},
            json={"scenario": "replay", "mode": "MOCK"},
        ).json()
        events = self.client.get(f"/api/v1/runs/{run['run_id']}/events").json()["events"]
        self.assertTrue(events)
        self.assertTrue(all(event["event_type"] != "station.frame_decided" for event in events))
        self.assertTrue(all("decision" not in event for event in events))

    def test_station_verdict_code_requires_authoritative_event(self):
        source = self.client.get("/static/app.js").text
        animation = self.client.get("/static/animation.js").text
        self.assertIn('event.event_type === "station.frame_decided" && event.decision_source === "station_receiver"', source)
        self.assertNotIn('decisionCardValue.textContent = "ACCEPT"', source)
        self.assertNotIn('decisionCardValue.textContent = "REJECT"', source)
        self.assertIn('this.packet("channel-rx", event)', animation)
        self.assertNotIn('this.packet("forward", event);\n      return;', animation)


if __name__ == "__main__":
    unittest.main()
