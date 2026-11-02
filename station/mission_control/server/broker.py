"""Validated, ordered, per-run event persistence and fan-out."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from mission_control.models import EventType, ValidationError, event_from_dict
from mission_control.agent_contract import ChannelActionEvent, IngestedTelemetry

from .database import ConflictError, MissionControlDB


class EventAuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class EventSource:
    role: str
    authenticated: bool


class EventBroker:
    def __init__(self, database: MissionControlDB):
        self.database = database
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._subscribers: defaultdict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    async def publish(
        self, raw_event: Mapping[str, Any], *, source: Optional[EventSource] = None
    ) -> tuple[dict[str, Any], bool]:
        event = event_from_dict(raw_event)
        run = self.database.get_run(str(event.run_id))
        if event.scenario != run["scenario"]:
            raise ValidationError("event scenario does not match its run")
        if event.event_type == EventType.STATION:
            if source is None or not source.authenticated or source.role != "station_receiver":
                raise EventAuthorizationError(
                    "Station decisions require an authenticated station_receiver source"
                )
            if run["mode"] == "MOCK":
                raise EventAuthorizationError("MOCK runs cannot publish Station security decisions")
        async with self._locks[str(event.run_id)]:
            stored, duplicate = self.database.store_event(event.to_dict())
            if not duplicate:
                for queue in tuple(self._subscribers[str(event.run_id)]):
                    queue.put_nowait(stored)
        return stored, duplicate

    async def publish_ingested(self, message: IngestedTelemetry) -> tuple[dict[str, Any], bool]:
        """Persist an already transport-authorized agent envelope."""
        event = message.event
        run = self.database.get_run(str(event.run_id))
        if run["mode"] != "LIVE":
            raise EventAuthorizationError("agent telemetry is accepted only for LIVE runs")
        if event.scenario != run["scenario"]:
            raise ValidationError("event scenario does not match its run")
        if event.event_type == EventType.STATION:
            raise EventAuthorizationError("remote Station ingestion is disabled")
        if event.event_type == EventType.CHANNEL and not self.database.has_frame_event(
            str(event.run_id), message.frame_id, EventType.SATELLITE.value
        ):
            raise ValidationError("channel event has no correlated Satellite frame")
        async with self._locks[str(event.run_id)]:
            stored, duplicate = self.database.store_event(
                event.to_dict(), frame_id=message.frame_id,
                source_agent_id=message.identity.agent_id, source_role=message.identity.role,
            )
            if not duplicate:
                self._fanout(str(event.run_id), stored)
        return stored, duplicate

    async def publish_channel_action(
        self, action: ChannelActionEvent, *, agent_id: str, role: str
    ) -> tuple[dict[str, Any], bool]:
        run = self.database.get_run(str(action.run_id))
        if run["mode"] != "LIVE":
            raise EventAuthorizationError("channel actions are accepted only for LIVE runs")
        if action.scenario != run["scenario"]:
            raise ValidationError("channel action scenario does not match its run")
        if not self.database.has_frame_event(
            str(action.run_id), action.frame_id, EventType.SATELLITE.value
        ):
            raise ValidationError("channel action has no correlated Satellite frame")
        wire = action.to_dict()
        wire["event_type"] = wire.pop("message_type")
        async with self._locks[str(action.run_id)]:
            stored, duplicate = self.database.store_event(
                wire, frame_id=action.frame_id, source_agent_id=agent_id, source_role=role,
            )
            if not duplicate:
                self._fanout(str(action.run_id), stored)
        return stored, duplicate

    def _fanout(self, run_id: str, stored: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers[run_id]):
            queue.put_nowait(stored)

    def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        self.database.get_run(run_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(run_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(run_id, None)

    @property
    def subscriber_count(self) -> int:
        """Number of live subscriber queues, exposed for lifecycle diagnostics."""
        return sum(len(subscribers) for subscribers in self._subscribers.values())
