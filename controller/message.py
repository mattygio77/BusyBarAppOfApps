"""Display message data structure and validation."""

from dataclasses import dataclass, field
from typing import List, Dict, Any
import time
import json


@dataclass
class DisplayMessage:
    """A display message published by a sub-app to the controller.
    
    Attributes:
        app_id: Unique identifier for the publishing app
        priority: 0-100, higher = more urgent
        duration_seconds: How long this message "owns" the display if it wins
        elements: List of BusyBar display elements (JSON-compatible dicts)
        timestamp: When the message was published (Unix timestamp)
    """
    
    app_id: str
    priority: int
    duration_seconds: int
    elements: List[Dict[str, Any]]
    timestamp: float = field(default_factory=time.time)
    
    def __post_init__(self):
        """Validate message on construction."""
        if not self.app_id or not isinstance(self.app_id, str):
            raise ValueError("app_id must be a non-empty string")
        
        if not isinstance(self.priority, int) or not (0 <= self.priority <= 100):
            raise ValueError("priority must be int 0-100")
        
        if not isinstance(self.duration_seconds, int) or self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive int")
        
        if not isinstance(self.elements, list) or len(self.elements) == 0:
            raise ValueError("elements must be non-empty list")
        
        # Validate each element has required fields
        for elem in self.elements:
            if not isinstance(elem, dict):
                raise ValueError("Each element must be a dict")
            if "id" not in elem or "type" not in elem:
                raise ValueError("Each element must have 'id' and 'type'")
            if elem["type"] not in ("text", "image"):
                raise ValueError(f"Unknown element type: {elem['type']}")
    
    def is_expired(self, current_time: float = None) -> bool:
        """Check if message has exceeded its duration."""
        if current_time is None:
            current_time = time.time()
        return (current_time - self.timestamp) > self.duration_seconds
    
    @classmethod
    def from_json(cls, json_str: str) -> "DisplayMessage":
        """Deserialize from JSON string (as published to Redis)."""
        data = json.loads(json_str)
        return cls(
            app_id=data["app_id"],
            priority=data["priority"],
            duration_seconds=data["duration_seconds"],
            elements=data["elements"],
            timestamp=data.get("timestamp", time.time()),
        )
    
    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps({
            "app_id": self.app_id,
            "priority": self.priority,
            "duration_seconds": self.duration_seconds,
            "elements": self.elements,
            "timestamp": self.timestamp,
        })
