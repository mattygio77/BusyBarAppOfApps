"""Priority-based display queue manager."""

from typing import Dict, Optional, List
import time
from .message import DisplayMessage


class DisplayQueue:
    """Manages a priority queue of display messages from sub-apps.
    
    Behavior:
    - Highest priority, non-expired message "owns" the display
    - Lower priority messages queue below it
    - Expired messages are removed
    - If no app owns display, caller should show idle content
    """
    
    def __init__(self):
        """Initialize empty queue."""
        self.messages: Dict[str, DisplayMessage] = {}  # app_id -> message
        self.order: List[str] = []  # ordered app_ids by priority
    
    def add_message(self, msg: DisplayMessage) -> None:
        """Add or update a message from an app.
        
        If app already has a message, it's replaced (refresh).
        Queue is re-sorted by priority.
        """
        self.messages[msg.app_id] = msg
        
        # Re-sort by priority (descending)
        self.order = sorted(
            self.messages.keys(),
            key=lambda aid: self.messages[aid].priority,
            reverse=True,
        )
    
    def get_current(self, current_time: float = None) -> Optional[DisplayMessage]:
        """Get the current display owner (highest priority, not expired).
        
        Cleans up expired messages along the way.
        Returns None if queue is empty or all messages expired.
        """
        if current_time is None:
            current_time = time.time()
        
        # Remove expired messages
        expired_apps = [
            aid for aid in self.order
            if self.messages[aid].is_expired(current_time)
        ]
        for aid in expired_apps:
            del self.messages[aid]
            self.order.remove(aid)
        
        # Return highest priority (first in sorted order)
        if self.order:
            return self.messages[self.order[0]]
        return None
    
    def clear_app(self, app_id: str) -> None:
        """Remove all messages from an app (e.g., app stopped)."""
        if app_id in self.messages:
            del self.messages[app_id]
            self.order.remove(app_id)
    
    def clear_all(self) -> None:
        """Clear entire queue."""
        self.messages.clear()
        self.order.clear()
    
    def get_queue_snapshot(self, current_time: float = None) -> List[dict]:
        """Get list of queued messages for debugging/state reporting.
        
        Returns them in order (highest priority first).
        """
        if current_time is None:
            current_time = time.time()
        
        result = []
        for app_id in self.order:
            msg = self.messages[app_id]
            if not msg.is_expired(current_time):
                result.append({
                    "app_id": msg.app_id,
                    "priority": msg.priority,
                    "expires_at": msg.timestamp + msg.duration_seconds,
                    "queued_at": msg.timestamp,
                })
        return result
