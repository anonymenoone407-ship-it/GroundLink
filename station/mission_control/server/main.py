"""ASGI entry point. Configuration is loaded from the environment at import."""

from .app import create_app
from .config import Settings


app = create_app(Settings.from_env())
