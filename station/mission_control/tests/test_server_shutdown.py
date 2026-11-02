import asyncio
from pathlib import Path
import socket
import tempfile
import unittest

import uvicorn
from websockets.asyncio.client import connect

from mission_control.server.app import create_app
from mission_control.server.config import Settings


TOKEN = "phase-1c-shutdown-token-strong"
ORIGIN = "http://127.0.0.1:8000"


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class UvicornShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_graceful_shutdown_with_idle_active_websocket(self):
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(
                Settings(
                    database_path=Path(temp) / "shutdown.sqlite3",
                    control_token=TOKEN,
                    allowed_origins=(ORIGIN,),
                )
            )
            run = app.state.database.create_run("e" * 32, "baseline", "MOCK")
            port = unused_port()
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    log_level="critical",
                    lifespan="on",
                )
            )
            server.install_signal_handlers = lambda: None
            server_task = asyncio.create_task(server.serve())
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(server.started)
                uri = f"ws://127.0.0.1:{port}/ws/v1/runs/{run['run_id']}?token={TOKEN}"
                websocket = await connect(uri, origin=ORIGIN)
                try:
                    for _ in range(100):
                        if app.state.broker.subscriber_count == 1:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(app.state.broker.subscriber_count, 1)
                    server.should_exit = True
                    await asyncio.wait_for(server_task, timeout=5)
                    self.assertEqual(app.state.broker.subscriber_count, 0)
                finally:
                    await websocket.close()
            finally:
                if not server_task.done():
                    server.should_exit = True
                    await asyncio.wait_for(server_task, timeout=5)


if __name__ == "__main__":
    unittest.main()
