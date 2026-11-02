"""GroundLink Mission Control Phase 1B API application factory."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from mission_control.models import ValidationError
from mission_control.run_id import new_run_id, validate_run_id
from mission_control.scenarios import Scenario, validate_scenario

from .auth import BrowserSessions, require_control_token, token_matches, websocket_authorized
from .broker import EventBroker
from .config import Settings
from .database import ConflictError, MissionControlDB, NotFoundError
from .mock import mock_events


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str
    mode: Literal["MOCK", "LIVE"] = "MOCK"


def _checked_run_id(run_id: str) -> str:
    try:
        return str(validate_run_id(run_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_app(settings: Settings) -> FastAPI:
    database = MissionControlDB(settings.database_path)
    broker = EventBroker(database)
    browser_sessions = BrowserSessions()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="GroundLink Mission Control", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.broker = broker
    app.state.browser_sessions = browser_sessions
    control = require_control_token(settings, browser_sessions)
    frontend = Path(__file__).parents[1] / "frontend"
    app.mount("/static", StaticFiles(directory=frontend), name="mission-control-static")

    @app.get("/", include_in_schema=False)
    def frontend_index():
        return FileResponse(frontend / "index.html")

    @app.post("/api/v1/browser-session", status_code=status.HTTP_204_NO_CONTENT)
    def create_browser_session(
        response: Response, authorization: str | None = Header(default=None)
    ):
        prefix = "Bearer "
        candidate = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else None
        if not token_matches(candidate, settings.control_token):
            raise HTTPException(status_code=401, detail="valid bearer token required")
        session_id = browser_sessions.create()
        response.set_cookie(
            BrowserSessions.cookie_name,
            session_id,
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=browser_sessions.ttl_seconds,
            path="/",
        )

    @app.delete("/api/v1/browser-session", status_code=status.HTTP_204_NO_CONTENT)
    def delete_browser_session(request: Request, response: Response):
        origin = request.headers.get("origin")
        if origin not in settings.allowed_origins:
            raise HTTPException(status_code=403, detail="origin rejected")
        browser_sessions.revoke(request.cookies.get(BrowserSessions.cookie_name))
        response.delete_cookie(BrowserSessions.cookie_name, path="/", samesite="strict")

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "service": "groundlink-mission-control", "live_agents": "NOT_READY"}

    @app.get("/api/v1/scenarios")
    def scenarios():
        return {
            "scenarios": [item.value for item in Scenario],
            "note": "Scenario labels never determine security decisions",
        }

    @app.post("/api/v1/runs", status_code=status.HTTP_201_CREATED, dependencies=[Depends(control)])
    async def create_run(request: CreateRunRequest):
        try:
            scenario = validate_scenario(request.scenario)
            run = database.create_run(str(new_run_id()), scenario, request.mode)
            if request.mode == "MOCK":
                for event in mock_events(run["run_id"], scenario):
                    await broker.publish(event)
            return run
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str):
        try:
            return database.get_run(_checked_run_id(run_id))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/api/v1/runs/{run_id}/events")
    def get_events(run_id: str, limit: int = Query(default=1000, ge=1, le=1000)):
        try:
            checked = _checked_run_id(run_id)
            return {"run_id": checked, "events": database.list_events(checked, limit)}
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post("/api/v1/runs/{run_id}/stop", dependencies=[Depends(control)])
    def stop_run(run_id: str):
        try:
            return database.stop_run(_checked_run_id(run_id))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.websocket("/ws/v1/runs/{run_id}")
    async def run_events(websocket: WebSocket, run_id: str):
        if not websocket_authorized(websocket, settings, browser_sessions):
            await websocket.close(code=1008, reason="origin or token rejected")
            return
        try:
            checked = str(validate_run_id(run_id))
            queue = broker.subscribe(checked)
        except (ValueError, NotFoundError):
            await websocket.close(code=1008, reason="run not found")
            return
        sent: set[str] = set()
        receive_task: asyncio.Task | None = None
        event_task: asyncio.Task | None = None
        try:
            await websocket.accept()
            receive_task = asyncio.create_task(websocket.receive())
            for event in database.list_events(checked):
                sent.add(event["event_id"])
                await websocket.send_json(event)
            while True:
                event_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    (receive_task, event_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task in done:
                    message = receive_task.result()
                    if message["type"] == "websocket.disconnect":
                        break
                    receive_task = asyncio.create_task(websocket.receive())
                if event_task not in done:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    event_task = None
                    continue
                event = event_task.result()
                event_task = None
                if event["event_id"] not in sent:
                    sent.add(event["event_id"])
                    await websocket.send_json(event)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            pending = [task for task in (receive_task, event_task) if task is not None and not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                try:
                    await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
                except asyncio.CancelledError:
                    # A client disconnect may cancel the ASGI scope while its child
                    # receives are being drained. Cleanup is still complete because
                    # every owned task was cancelled above.
                    pass
            broker.unsubscribe(checked, queue)

    return app
