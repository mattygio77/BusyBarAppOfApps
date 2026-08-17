"""Main BusyBar controller orchestrating display priority and routing."""

import subprocess
import time
import json
import sys
import signal
import logging
from typing import Optional, Dict, Any
from threading import Thread, Event

import redis

from busylib import BusyBar
from busylib.exceptions import BusyBarError

from .message import DisplayMessage
from .display_queue import DisplayQueue
from .config import ConfigManager


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# application_name the controller uses when talking to the device. All
# elements drawn under this name are what _clear_device removes.
APPLICATION_NAME = "busybar_controller"


class BusyBarController:
    """Main controller: receives messages from sub-apps, manages display ownership.
    
    Flow:
    1. Load config (apps, display settings)
    2. Ensure Redis is running
    3. Subscribe to Redis channels per enabled app
    4. Main loop: check priority queue, draw to device, rotate idle content
    5. Persist state on shutdown
    """
    
    def __init__(self, config_path: str, state_path: str):
        """Initialize controller.
        
        Args:
            config_path: Path to apps.json
            state_path: Path to state.json
        """
        self.config_manager = ConfigManager(config_path, state_path)
        self.config = self.config_manager.load_config()
        self.state = self.config_manager.load_state()
        
        self.device_ip = self.config.get("device_ip", "10.0.4.20")
        self.redis_host = self.config.get("redis_host", "localhost")
        self.redis_port = self.config.get("redis_port", 6379)
        
        self.redis_client: Optional[redis.Redis] = None
        self.redis_process: Optional[subprocess.Popen] = None
        
        # BusyBar device client (busylib). One long-lived client reused for
        # every draw/clear call instead of opening a new connection per
        # request.
        self.busybar = BusyBar(self.device_ip, timeout=5.0)
        
        self.display_queue = DisplayQueue()
        self.shutdown_event = Event()
        self.listener_thread: Optional[Thread] = None
        
        # Idle rotation state
        self.idle_config = self.config.get("idle_rotation", {})
        self.idle_content = self.idle_config.get("content", [])
        self.idle_index = 0
        self.last_idle_rotation = time.time()
        self.idle_cycle = self.idle_config.get("cycle_seconds", 10)
        # Cached idle DisplayMessage. Only rebuilt when idle content actually
        # rotates, so its identity (app_id, timestamp) stays stable between
        # rotations - see _get_idle_message().
        self._idle_message: Optional[DisplayMessage] = None
    
    def _ensure_redis(self) -> None:
        """Check if Redis is running; if not, start it."""
        logger.info(f"Checking Redis on {self.redis_host}:{self.redis_port}...")
        
        for attempt in range(3):
            try:
                test_client = redis.Redis(
                    host=self.redis_host,
                    port=self.redis_port,
                    socket_connect_timeout=2,
                )
                test_client.ping()
                logger.info("Redis is running")
                return
            except (redis.ConnectionError, redis.TimeoutError):
                if attempt < 2:
                    logger.warning(
                        f"Redis not responding (attempt {attempt + 1}/3), "
                        "will try to start it..."
                    )
                    time.sleep(0.5)
        
        # Start Redis
        logger.info("Starting Redis server...")
        try:
            self.redis_process = subprocess.Popen(
                ["redis-server", "--port", str(self.redis_port)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(1)
            
            # Verify it started
            client = redis.Redis(
                host=self.redis_host,
                port=self.redis_port,
                socket_connect_timeout=2,
            )
            client.ping()
            logger.info("Redis started successfully")
        except Exception as e:
            logger.error(f"Failed to start Redis: {e}")
            raise
    
    def _connect_redis(self) -> None:
        """Create Redis client connection."""
        self.redis_client = redis.Redis(
            host=self.redis_host,
            port=self.redis_port,
            decode_responses=True,
        )
        logger.info(f"Connected to Redis at {self.redis_host}:{self.redis_port}")
    
    def _subscribe_to_apps(self) -> None:
        """Subscribe to Redis channels for all enabled apps."""
        pubsub = self.redis_client.pubsub()
        
        enabled_apps = [
            app for app in self.config.get("apps", [])
            if app.get("enabled", True)
        ]
        
        for app in enabled_apps:
            channel = f"busybar:app:{app['id']}"
            pubsub.subscribe(channel)
            logger.info(f"Subscribed to {channel}")
        
        # Start listener thread
        self.listener_thread = Thread(
            target=self._listen_for_messages,
            args=(pubsub,),
            daemon=True,
        )
        self.listener_thread.start()
    
    def _listen_for_messages(self, pubsub) -> None:
        """Listen for published messages in a thread."""
        logger.info("Message listener started")
        
        for message in pubsub.listen():
            if self.shutdown_event.is_set():
                break
            
            if message["type"] != "message":
                continue
            
            try:
                msg = DisplayMessage.from_json(message["data"])
                logger.debug(
                    f"Received message from {msg.app_id} "
                    f"(priority={msg.priority}, duration={msg.duration_seconds}s)"
                )
                self.display_queue.add_message(msg)
            except Exception as e:
                logger.error(f"Failed to parse message: {e}")
        
        logger.info("Message listener stopped")
    
    def _clear_device(self) -> bool:
        """Clear all display elements owned by this controller.

        /api/display/draw is additive: it upserts elements by id rather
        than replacing the whole screen. Without an explicit clear, an
        element from the previous display owner (e.g. weather's icon)
        stays on screen after we switch to a new owner that doesn't
        redeclare that element id.

        Returns:
            True if successful, False otherwise.
        """
        try:
            self.busybar.display_clear(application_name=APPLICATION_NAME)
            logger.debug("Display cleared")
            return True
        except BusyBarError as e:
            logger.warning(f"Failed to clear display: {e}")
            return False
    
    def _draw_to_device(self, elements: list) -> bool:
        """Send display elements to BusyBar device.
        
        Clears any elements left over from the previous display owner
        first, since draws are additive/upsert-by-id, not a full replace.
        The clear is best-effort: if it fails we still attempt the draw
        rather than bailing out entirely (mirrors the previous behavior
        of calling _clear_device() and ignoring its return value).
        
        Returns:
            True if successful, False otherwise.
        """
        
        try:
            self.busybar.display_draw({
                "application_name": APPLICATION_NAME,
                "elements": elements,
                }, 
                clear_before_draw=True
            )
            logger.debug(f"Display updated ({len(elements)} elements)")
            return True
        except BusyBarError as e:
            logger.warning(f"Failed to draw to device: {e}")
            return False
    
    def _get_idle_message(self) -> DisplayMessage:
        """Get the current idle message to display.
        
        Rotates through idle_content at idle_cycle rate. Returns the same
        cached DisplayMessage instance between rotations - a fresh instance
        (with a fresh timestamp) is only built when the idle content
        actually changes, so the main loop's identity check doesn't fire a
        needless redraw every tick while idle content is unchanged.
        """
        current_time = time.time()
        
        # Check if it's time to rotate
        rotated = False
        if (current_time - self.last_idle_rotation) > self.idle_cycle:
            self.idle_index = (self.idle_index + 1) % max(len(self.idle_content), 1)
            self.last_idle_rotation = current_time
            rotated = True
        
        if self._idle_message is not None and not rotated:
            return self._idle_message
        
        # If no idle content, use a default message
        if not self.idle_content:
            self._idle_message = DisplayMessage(
                app_id="idle",
                priority=0,
                duration_seconds=999999,
                elements=[
                    {
                        "id": "idle",
                        "type": "text",
                        "text": "Ready...",
                        "x": 15,
                        "y": 7,
                        "font": "small",
                        "color": "#FFFFFFFF",
                        "width": 72,
                        "scroll_rate": 0,
                        "timeout": 6,
                    }
                ],
            )
        else:
            content = self.idle_content[self.idle_index]
            self._idle_message = DisplayMessage(
                app_id="idle",
                priority=0,
                duration_seconds=999999,
                elements=[content],
            )
        
        return self._idle_message
    
    def start(self) -> None:
        """Main event loop."""
        logger.info("Starting BusyBar controller...")
        
        # Setup
        self._ensure_redis()
        self._connect_redis()
        self._subscribe_to_apps()
        
        # Signal handlers
        def on_signal(sig, frame):
            logger.info("Shutdown signal received")
            self.shutdown_event.set()
        
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        
        logger.info("Controller ready, entering main loop...")
        
        # Identity of what's currently drawn on the device: (app_id,
        # message timestamp). Comparing on the message's own timestamp,
        # not just app_id, is what lets an app that's already the display
        # owner push an updated draw (e.g. weather's temperature ticking
        # over) instead of being silently ignored because its app_id
        # hasn't changed.
        current_display_key: Optional[tuple] = None
        current_display_owner: Optional[str] = None
        current_owner_since: Optional[float] = None
        
        try:
            while not self.shutdown_event.is_set():
                current_time = time.time()
                
                # Get current display owner: highest priority message,
                # guaranteed its own duration_seconds of screen time unless
                # boot off by something strictly higher priority.
                current_msg = self.display_queue.get_current(current_time)
                
                # If no app owns display, show idle content
                if current_msg is None:
                    current_msg = self._get_idle_message()
                
                display_key = (current_msg.app_id, current_msg.timestamp)
                
                # Redraw whenever the message actually on screen has
                # changed - whether that's a new app taking ownership or
                # the same app pushing fresh content while it still owns
                # the display.
                if display_key != current_display_key:
                    owner_changed = current_msg.app_id != current_display_owner
                    if owner_changed:
                        logger.info(
                            f"Display ownership: {current_display_owner} → "
                            f"{current_msg.app_id} "
                            f"(priority={current_msg.priority})"
                        )
                        current_owner_since = current_time
                    else:
                        logger.debug(
                            f"Refreshing display content for "
                            f"{current_msg.app_id} "
                            f"(priority={current_msg.priority})"
                        )
                    
                    self._draw_to_device(current_msg.elements)
                    current_display_key = display_key
                    current_display_owner = current_msg.app_id
                    
                    # Update state
                    self.state["current_owner"] = {
                        "app_id": current_msg.app_id,
                        "priority": current_msg.priority,
                        "since": current_owner_since,
                        "expires_at": current_owner_since + current_msg.duration_seconds,
                    }
                    self.state["queue"] = self.display_queue.get_queue_snapshot(
                        current_time
                    )
                    self.config_manager.save_state(self.state)
                
                # Sleep briefly before next iteration (2 fps)
                time.sleep(0.5)
        
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
        
        finally:
            self.shutdown()
    
    def shutdown(self) -> None:
        """Clean shutdown."""
        logger.info("Shutting down controller...")
        
        self.shutdown_event.set()
        
        if self.listener_thread:
            self.listener_thread.join(timeout=2)
        
        if self.redis_process:
            logger.info("Stopping Redis...")
            self.redis_process.terminate()
            try:
                self.redis_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.redis_process.kill()
        
        self.busybar.close()
        
        self.config_manager.save_state(self.state)
        logger.info("Controller stopped")