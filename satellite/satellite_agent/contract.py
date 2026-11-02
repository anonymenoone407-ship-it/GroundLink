"""Imports and validates against the immutable distributed contract v1.0.0."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

CONTRACT_ROOT = (
    Path(__file__).resolve().parents[1]
    / "dist"
    / "groundlink-agent-contract-1.0.0"
)
if str(CONTRACT_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTRACT_ROOT))

from mission_control.agent_contract import (  # noqa: E402
    AGENT_CONTRACT_VERSION,
    AgentCapabilities,
    AgentIdentity,
    AgentState,
    HealthReport,
    Heartbeat,
    IngestedTelemetry,
    PrepareCommand,
    ReadinessReport,
    StartCommand,
    StopCommand,
    command_from_dict,
)
from mission_control.models import SatelliteEvent, ValidationError  # noqa: E402
from mission_control.scenarios import SCENARIO_ALLOWLIST  # noqa: E402

if AGENT_CONTRACT_VERSION != "1.0.0":
    raise RuntimeError("Satellite agent requires distributed contract 1.0.0")

_SCHEMA_PATH = CONTRACT_ROOT / "mission_control" / "schemas" / "agent-contract-v1.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_COMMAND_SCHEMAS = {
    name: Draft202012Validator(
        {"$ref": f"#/$defs/{name}", "$defs": _SCHEMA["$defs"]},
        format_checker=FormatChecker(),
    )
    for name in ("prepare", "start", "stop")
}


def strict_command_from_dict(data: Mapping[str, Any]):
    """Apply JSON Schema and reference-model validation before state mutation."""

    if not isinstance(data, Mapping):
        raise ValidationError("command must be an object")
    kind = data.get("command_type")
    validator = _COMMAND_SCHEMAS.get(str(kind).lower())
    if validator is None:
        raise ValidationError("command_type is not supported")
    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.path))
    if errors:
        raise ValidationError(errors[0].message)
    return command_from_dict(data)
