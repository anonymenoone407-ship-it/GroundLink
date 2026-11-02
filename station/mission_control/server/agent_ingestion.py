"""Fail-closed local integration boundary for authenticated agent messages.

No HTTP route is registered here.  A future TLS listener must construct
RequestContext from the verified peer certificate, never from HTTP headers.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Mapping

from mission_control.agent_contract import (
    AgentId, AgentRole, AuthenticatedAgent, AuthenticationMechanism,
    authorize_ingestion, channel_action_from_dict, ingestion_from_dict,
)
from mission_control.models import EventType, ValidationError

from .broker import EventBroker
from .database import ConflictError


MAX_AGENT_REQUEST_BYTES = 64 * 1024
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True)
class RequestContext:
    """Metadata produced by a verified transport, never by request JSON."""
    peer: AuthenticatedAgent
    request_id: str
    sent_at: datetime
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AgentIngestionAdapter:
    def __init__(self, broker: EventBroker, *, freshness_seconds: int = 300):
        self.broker = broker
        self.freshness_seconds = freshness_seconds

    def _authorize_request(self, context: RequestContext, encoded_size: int) -> None:
        if encoded_size <= 0 or encoded_size > MAX_AGENT_REQUEST_BYTES:
            raise PermissionError("agent request size rejected")
        peer = context.peer
        if not peer.authenticated or peer.mechanism != AuthenticationMechanism.MTLS.value:
            raise PermissionError("verified mTLS agent transport is required")
        if not isinstance(context.request_id, str) or REQUEST_ID_PATTERN.fullmatch(context.request_id) is None:
            raise PermissionError("invalid request id")
        now = datetime.now(timezone.utc)
        sent = context.sent_at
        if sent.tzinfo is None or abs((now - sent).total_seconds()) > self.freshness_seconds:
            raise PermissionError("stale agent request")
        try:
            self.broker.database.claim_agent_request(
                peer.agent_id, context.request_id, sent.isoformat()
            )
        except ConflictError as exc:
            raise PermissionError("replayed agent request") from exc

    async def ingest_telemetry(
        self, raw: Mapping[str, Any], *, context: RequestContext, encoded_size: int
    ) -> tuple[dict[str, Any], bool]:
        self._authorize_request(context, encoded_size)
        message = ingestion_from_dict(raw)
        authorize_ingestion(message, context.peer)
        if message.event.event_type not in {EventType.SATELLITE, EventType.CHANNEL}:
            raise PermissionError("remote Station decisions are forbidden")
        return await self.broker.publish_ingested(message)

    async def ingest_channel_action(
        self, raw: Mapping[str, Any], *, context: RequestContext, encoded_size: int
    ) -> tuple[dict[str, Any], bool]:
        self._authorize_request(context, encoded_size)
        peer = context.peer
        if (peer.agent_id, peer.role) != (AgentId.CHANNEL.value, AgentRole.CHANNEL.value):
            raise PermissionError("only the authenticated Channel may publish channel actions")
        action = channel_action_from_dict(raw)
        return await self.broker.publish_channel_action(
            action, agent_id=peer.agent_id, role=peer.role
        )

