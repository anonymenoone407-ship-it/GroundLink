"""Shared, opaque identifier for correlating events from one experiment run."""

import re
import uuid
from typing import NewType


RunId = NewType("RunId", str)
_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def new_run_id() -> RunId:
    """Create a run ID compatible with MonitoringDB's uuid4().hex format."""
    return RunId(uuid.uuid4().hex)


def validate_run_id(value: object) -> RunId:
    if not isinstance(value, str) or _RUN_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("run_id must be 32 lowercase hexadecimal characters")
    return RunId(value)
