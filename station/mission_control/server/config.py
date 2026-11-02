"""Server configuration with conservative local-development defaults."""

from dataclasses import dataclass
import os
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).parents[1] / "state" / "mission_control.sqlite3"
DEFAULT_ORIGINS = ("http://127.0.0.1:8000", "http://localhost:8000")


@dataclass(frozen=True)
class Settings:
    database_path: Path
    control_token: str
    allowed_origins: tuple[str, ...] = DEFAULT_ORIGINS

    def __post_init__(self) -> None:
        if len(self.control_token) < 16:
            raise ValueError("control_token must contain at least 16 characters")
        if not self.allowed_origins:
            raise ValueError("at least one WebSocket origin is required")

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("GL_MISSION_CONTROL_TOKEN")
        if not token:
            raise RuntimeError("GL_MISSION_CONTROL_TOKEN is required")
        origins = tuple(
            value.strip()
            for value in os.environ.get(
                "GL_MISSION_CONTROL_ORIGINS", ",".join(DEFAULT_ORIGINS)
            ).split(",")
            if value.strip()
        )
        return cls(
            database_path=Path(os.environ.get("GL_MISSION_CONTROL_DB", DEFAULT_DB_PATH)),
            control_token=token,
            allowed_origins=origins,
        )
