"""BusyBar multi-app controller with Redis pub/sub event bus."""

from .message import DisplayMessage
from .display_queue import DisplayQueue
from .config import ConfigManager

# BusyBarController imported lazily in main.py to avoid dependency issues

__all__ = ["DisplayMessage", "DisplayQueue", "ConfigManager"]
