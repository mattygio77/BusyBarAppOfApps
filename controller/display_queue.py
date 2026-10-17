"""Priority- and duration-based display ownership manager supporting multiple cards per app."""

from typing import Dict, Optional, List, Tuple
import time
from .message import DisplayMessage

SlotKey = Tuple[str, str]


class DisplayQueue:
    """Manages display ownership across sub-apps and multiple display cards per app.
    
    Rules:
    - Each display slot is keyed by (app_id, card_id) (where card_id defaults
      to "default" if omitted). This lets an app publish multiple distinct
      cards (e.g. multiple stock tickers or multiple games) as well as
      separate apps queueing displays simultaneously without overwriting
      each other.
    - Publishing a message with the same (app_id, card_id) replaces that
      slot's queued or active message.
    - Whichever slot currently owns the display is guaranteed to stay up
      for its own duration_seconds, counted from the moment it *became* the
      owner - not from when it was published. Time spent waiting in the
      queue behind another owner never eats into that guarantee.
    - Only a STRICTLY higher priority message can boot the current owner off
      early (or an in-place update from the same app_id with higher priority,
      such as a goal animation). Equal or lower priority messages wait their
      turn.
    - When selecting the next owner among equal-priority candidates, the queue
      interleaves fairly across apps (preferring apps that haven't just owned
      the screen) and preserves FIFO publish order among cards of the same app.
    - A waiting message is dropped if it has waited longer than its
      ttl_seconds (or duration_seconds when ttl_seconds is not set).
    - If no app owns the display, the caller should show idle content.
    """
    
    def __init__(self, max_cards_per_app: int = 10):
        """Initialize empty queue."""
        self.max_cards_per_app = max_cards_per_app
        self.messages: Dict[SlotKey, DisplayMessage] = {}
        self.current_owner_key: Optional[SlotKey] = None
        self.current_owner_started_at: Optional[float] = None
        self._app_last_turn_seq: Dict[str, int] = {}
        self._turn_seq: int = 0
    
    @property
    def current_owner_app_id(self) -> Optional[str]:
        """Backward-compatible property returning the active owner's app_id."""
        return self.current_owner_key[0] if self.current_owner_key else None
    
    def _enforce_app_limit(self, app_id: str) -> None:
        """Keep at most max_cards_per_app queued slots for any single app_id."""
        app_keys = [
            k for k in self.messages
            if k[0] == app_id and k != self.current_owner_key
        ]
        if len(app_keys) <= self.max_cards_per_app:
            return
        app_keys.sort(key=lambda k: self.messages[k].timestamp)
        for k in app_keys[: len(app_keys) - self.max_cards_per_app]:
            del self.messages[k]
    
    def add_message(self, msg: DisplayMessage) -> None:
        """Add or update a display card from an app.
        
        Replaces any previous message with the same (app_id, card_id) slot_key.
        If this exact slot currently owns the display, the new message renews
        its turn and updates on screen immediately.
        """
        key = msg.slot_key
        self.messages[key] = msg
        self._enforce_app_limit(msg.app_id)
        
        if key == self.current_owner_key:
            self.current_owner_started_at = time.time()
    
    def _best_candidate(self, exclude_key: Optional[SlotKey] = None) -> Optional[SlotKey]:
        """Return the slot_key of the highest-priority pending message.
        
        Ties in priority are broken by:
        1. Fair cross-app interleaving (the app that least recently owned the
           screen goes first, so an app with multiple queued cards shares the
           display cleanly with other apps at the same priority).
        2. Earliest publish timestamp (FIFO within each app's cards).
        3. Deterministic (app_id, card_id) lexical tie-breaker.
        """
        candidate_keys = [
            k for k in self.messages if k != exclude_key
        ]
        if not candidate_keys:
            return None
        return min(
            candidate_keys,
            key=lambda k: (
                -self.messages[k].priority,
                self._app_last_turn_seq.get(k[0], 0),
                self.messages[k].timestamp,
                k[0],
                k[1],
            ),
        )
    
    def _set_owner(self, key: SlotKey, current_time: float) -> DisplayMessage:
        """Grant display ownership to `key` and record turn order."""
        self.current_owner_key = key
        self.current_owner_started_at = current_time
        self._turn_seq += 1
        self._app_last_turn_seq[key[0]] = self._turn_seq
        return self.messages[key]
    
    def get_current(self, current_time: Optional[float] = None) -> Optional[DisplayMessage]:
        """Get the message that should currently be on screen.
        
        Drops stale never-shown messages, retires the current owner slot once
        its guaranteed duration has elapsed, lets a strictly higher priority
        message preempt early, and otherwise keeps the current owner on screen.
        Returns None if nothing is available to show (caller falls back to idle).
        """
        if current_time is None:
            current_time = time.time()
        
        # Drop messages that have been waiting longer than their TTL / duration
        # and never got a turn.
        stale_waiting = [
            k for k, msg in self.messages.items()
            if k != self.current_owner_key and msg.is_expired(current_time)
        ]
        for k in stale_waiting:
            del self.messages[k]
        
        owner_key = self.current_owner_key
        owner_msg = self.messages.get(owner_key) if owner_key is not None else None
        
        if owner_msg is not None:
            turn_elapsed = (
                current_time - self.current_owner_started_at
            ) >= owner_msg.duration_seconds
            
            if turn_elapsed:
                # This card has had its full guaranteed turn; retire it
                # and hand the display to the next best candidate.
                del self.messages[owner_key]
                owner_key = None
                owner_msg = None
                self.current_owner_key = None
                self.current_owner_started_at = None
            else:
                # Still within its guaranteed window: only a strictly
                # higher priority challenger can boot it off early.
                challenger_key = self._best_candidate(exclude_key=owner_key)
                if (
                    challenger_key is not None
                    and self.messages[challenger_key].priority > owner_msg.priority
                ):
                    # If the challenger is from the same app (e.g. a goal animation
                    # preempting that app's normal scoreboard), retire the old slot
                    # so it doesn't linger behind.
                    if challenger_key[0] == owner_key[0]:
                        del self.messages[owner_key]
                    owner_msg = self._set_owner(challenger_key, current_time)
                    owner_key = challenger_key
        
        if owner_msg is None:
            best_key = self._best_candidate()
            if best_key is not None:
                owner_msg = self._set_owner(best_key, current_time)
        
        return owner_msg
    
    def clear_app(self, app_id: str) -> None:
        """Remove all queued/active cards belonging to `app_id`."""
        keys_to_remove = [k for k in self.messages if k[0] == app_id]
        for k in keys_to_remove:
            del self.messages[k]
        if self.current_owner_key and self.current_owner_key[0] == app_id:
            self.current_owner_key = None
            self.current_owner_started_at = None
    
    def clear_all(self) -> None:
        """Clear entire queue."""
        self.messages.clear()
        self.current_owner_key = None
        self.current_owner_started_at = None
    
    def get_queue_snapshot(self, current_time: Optional[float] = None) -> List[dict]:
        """Get list of queued messages for debugging/state reporting.
        
        Returns them in priority order, current owner first.
        """
        if current_time is None:
            current_time = time.time()
        
        ordered_keys = sorted(
            self.messages.keys(),
            key=lambda k: (
                k != self.current_owner_key,  # owner sorts first
                -self.messages[k].priority,
                self._app_last_turn_seq.get(k[0], 0),
                self.messages[k].timestamp,
            ),
        )
        
        result = []
        for key in ordered_keys:
            msg = self.messages[key]
            is_owner = key == self.current_owner_key
            entry = {
                "app_id": msg.app_id,
                "card_id": msg.card_id or "default",
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
                effective_ttl = (
                    msg.ttl_seconds
                    if msg.ttl_seconds is not None
                    else msg.duration_seconds
                )
                entry["expires_at"] = msg.timestamp + effective_ttl
            result.append(entry)
        return result
