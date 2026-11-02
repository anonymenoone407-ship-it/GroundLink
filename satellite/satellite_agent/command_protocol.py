"""Pinned imports and schema validation for command transport protocol 1.0."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

TRANSPORT_ROOT = (
    Path(__file__).resolve().parents[1] / "dist" / "groundlink-command-transport-1.0.0"
)
if str(TRANSPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(TRANSPORT_ROOT))

from groundlink_command_transport.command_protocol import (  # noqa: E402
    COMMAND_PROTOCOL_VERSION,
    MAX_COMMAND_WIRE_BYTES,
    CommandAcknowledgement,
    CommandRequest,
    command_ack_from_dict,
    command_request_from_dict,
)

if COMMAND_PROTOCOL_VERSION != "1.0":
    raise RuntimeError("Satellite server requires command protocol 1.0")

_SCHEMA = json.loads(
    (TRANSPORT_ROOT / "schemas" / "command-transport-v1.schema.json").read_text(encoding="utf-8")
)
_REQUEST_VALIDATOR = Draft202012Validator(
    {"$ref": "#/$defs/request", "$defs": _SCHEMA["$defs"]},
    format_checker=FormatChecker(),
)


def strict_request_from_dict(data: Mapping[str, Any]) -> CommandRequest:
    errors = sorted(_REQUEST_VALIDATOR.iter_errors(data), key=lambda error: list(error.path))
    if errors:
        raise ValueError(errors[0].message)
    return command_request_from_dict(data)
