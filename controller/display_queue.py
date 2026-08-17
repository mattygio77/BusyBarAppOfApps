"""Priority- and duration-based display ownership manager."""

from typing import Dict, Optional, List
import time
from .message import DisplayMessage


class DisplayQueue:
    """Manages display ownership among sub-apps by priority and duration.
    
    Rules:
    - Each app has at most one live message at a time; publishing a new one
      (via add_message) replaces whatever that app had queued or showing.
    - Whichever message currently owns the display is guaranteed to stay up
      for its own duration_seconds, counted from the moment it *became* the
      owner - not from when it was published. Time spent waiting in the
      queue behind a higher-priority owner never eats into that guarantee.
    - Only a STRICTLY higher priority message can boot the current owner off
      early. Equal or lower priority messages simply wait their turn.
    - If the app that owns the display publishes again while it still owns
      it, the new message takes over immediately (redraws) and its own
      duration restarts the ownership clock. This is how a live app (e.g.
      one republishing on an interval) keeps itself on screen indefinitely:
      each fresh message renews its turn before the previous one runs out.
    - A message that is still waiting for its turn (never yet been the
      owner) is dropped if it's been waiting longer than its own
      duration_seconds, since by the time it would be shown the data it
      carries is presumably stale. A live app recovers automatically by
      publishing a fresh message; this also cleans up after apps that
      crashed while queued behind something else.
    - If no app owns the display, the caller should show idle content.
    """
    
    def __init__(self):
        """Initialize empty queue."""
        self.messages: Dict[str, DisplayMessage] = {}  # app_id -> latest message
        self.current_owner_app_id: Optional[str] = None
        self.current_owner_started_at: Optional[float] = None
    
    def add_message(self, msg: DisplayMessage) -> None:
        """Add or update a message from an app.
        
        Replaces any previous message (shown or waiting) from this app_id.
        If this app currently owns the display, the new message renews its
        turn: its content takes effect on the next get_current() call and
        its duration_seconds restarts the ownership clock from now.
        """
        self.messages[msg.app_id] = msg
        
        if msg.app_id == self.current_owner_app_id:
            self.current_owner_started_at = time.time()
    
    def _best_candidate(self, exclude_app_id: Optional[str] = None) -> Optional[str]:
        """Return the app_id of the highest-priority pending message.
        
        Ties are broken in favor of whichever message has been waiting
        longest (earliest publish timestamp), then by app_id for
        determinism.
        """
        candidate_ids = [
            aid for aid in self.messages if aid != exclude_app_id
        ]
        if not candidate_ids:
            return None
        return min(
            candidate_ids,
            key=lambda aid: (
                -self.messages[aid].priority,
                self.messages[aid].timestamp,
                aid,
            ),
        )
    
    def get_current(self, current_time: Optional[float] = None) -> Optional[DisplayMessage]:
        """Get the message that should currently be on screen.
        
        Drops stale never-shown messages, retires the current owner once
        its guaranteed duration has elapsed, lets a strictly higher
        priority message preempt early, and otherwise keeps the current
        owner on screen. Returns None if nothing is available to show
        (caller should fall back to idle content).
        """
        if current_time is None:
            current_time = time.time()
        
        # Drop messages that have been waiting for their turn longer than
        # their own requested duration and never got one.
        stale_waiting = [
            aid for aid, msg in self.messages.items()
            if aid != self.current_owner_app_id and msg.is_expired(current_time)
        ]
        for aid in stale_waiting:
            del self.messages[aid]
        
        owner_id = self.current_owner_app_id
        owner_msg = self.messages.get(owner_id) if owner_id is not None else None
        
        if owner_msg is not None:
            turn_elapsed = (
                current_time - self.current_owner_started_at
            ) >= owner_msg.duration_seconds
            
            if turn_elapsed:
                # This message has had its full guaranteed turn; retire it
                # and let the next call hand the display to whoever's best.
                del self.messages[owner_id]
                owner_id = None
                owner_msg = None
                self.current_owner_app_id = None
                self.current_owner_started_at = None
            else:
                # Still within its guaranteed window: only a strictly
                # higher priority challenger can boot it off early.
                challenger_id = self._best_candidate(exclude_app_id=owner_id)
                if (
                    challenger_id is not None
                    and self.messages[challenger_id].priority > owner_msg.priority
                ):
                    owner_id = challenger_id
                    owner_msg = self.messages[owner_id]
                    self.current_owner_app_id = owner_id
                    self.current_owner_started_at = current_time
                # else: current owner keeps the display. Its content is
                # already current - add_message() always stores the latest
                # message for this app_id.
        
        if owner_msg is None:
            # No current owner (first run, previous owner's turn just
            # ended, or it was cleared) - grant the display to the best
            # remaining candidate, if any.
            best_id = self._best_candidate()
            if best_id is not None:
                owner_id = best_id
                owner_msg = self.messages[owner_id]
                self.current_owner_app_id = owner_id
                self.current_owner_started_at = current_time
        
        return owner_msg
    
    def clear_app(self, app_id: str) -> None:
        """Remove all messages from an app (e.g., app stopped)."""
        if app_id in self.messages:
            del self.messages[app_id]
        if self.current_owner_app_id == app_id:
            self.current_owner_app_id = None
            self.current_owner_started_at = None
    
    def clear_all(self) -> None:
        """Clear entire queue."""
        self.messages.clear()
        self.current_owner_app_id = None
        self.current_owner_started_at = None
    
    def get_queue_snapshot(self, current_time: Optional[float] = None) -> List[dict]:
        """Get list of queued messages for debugging/state reporting.
        
        Returns them in priority order, current owner first.
        """
        if current_time is None:
            current_time = time.time()
        
        ordered_ids = sorted(
            self.messages.keys(),
            key=lambda aid: (
                aid != self.current_owner_app_id,  # owner sorts first
                -self.messages[aid].priority,
                self.messages[aid].timestamp,
            ),
        )
        
        result = []
        for app_id in ordered_ids:
            msg = self.messages[app_id]
            is_owner = app_id == self.current_owner_app_id
            entry = {
                "app_id": msg.app_id,
                "priority": msg.priority,
                "is_current_owner": is_owner,
                "queued_at": msg.timestamp,
            }
            if is_owner and self.current_owner_started_at is not None:
                entry["owner_since"] = self.current_owner_started_at
                entry["expires_at"] = (
                    self.current_owner_started_at + msg.duration_seconds
                )
            else:
                # Time by which this message must be granted the display
                # or it gets dropped as stale.
                entry["expires_at"] = msg.timestamp + msg.duration_seconds
            result.append(entry)
        return result