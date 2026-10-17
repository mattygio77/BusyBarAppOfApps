#!/usr/bin/env python3
"""NHL sports sub-app for BusyBar controller."""

import argparse
import json
import logging
import math
import signal
import time
from pathlib import Path
from io import BytesIO
from typing import Optional

import redis
import requests
from PIL import Image

from icon_uploader import IconUploader

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] nhl_app: %(message)s",
)
logger = logging.getLogger(__name__)

FRONT_WIDTH = 72
ICON_SIZE = 16
# Center text box sits strictly between left logo (x=0..15) and right logo (x=56..71)
CENTER_TEXT_X = 36
CENTER_TEXT_WIDTH = 38

# Map NHL API 3-letter team abbreviations to ESPN CDN logo slugs where they differ
ESPN_SLUG_MAP = {
    "LAK": "la",
    "NJD": "nj",
    "SJS": "sj",
    "TBL": "tb",
    "UTA": "utah",
}


class NHLApp:
    def __init__(
        self,
        app_id: str,
        team: str,
        my_logo: Optional[str],
        priority: int,
        interval_seconds: int = 60,
        stale_buffer_seconds: int = 5,
        active_seconds: float = 30.0,
        rest_seconds: float = 30.0,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        device_ip: str = "busybar.local",
    ):
        self.app_id = app_id
        self.team = team.upper()
        self.my_logo = my_logo
        self.priority = priority
        self.interval_seconds = interval_seconds
        self.stale_buffer_seconds = stale_buffer_seconds
        
        self.active_seconds = active_seconds
        self.rest_seconds = rest_seconds
        self.duration_seconds = math.ceil(active_seconds) + stale_buffer_seconds
        
        self.cycle_state = "active"
        self.cycle_started_at = time.time()
        
        self.redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
        self.redis_channel = f"busybar:app:{app_id}"
        
        self.icon_dir = Path(__file__).parent / "nhl" / "icons"
        self.icon_dir.mkdir(parents=True, exist_ok=True)
        self.icon_uploader = IconUploader(device_ip, icon_folder=self.icon_dir)
        
        self.last_score = -1
        self.last_opp_score = -1
        self.last_fetch = 0.0
        self.current_game = None
        
        # Animation state
        self.animating_goal = False
        self.animating_opp_goal = False
        self.animation_started_at = 0.0
        self.animation_duration = 15.0
        
        # Deduplication tracking so static/scrolling text isn't republished every second
        self._last_published_signature: Optional[str] = None
        self._last_published_at: float = 0.0
        self._last_published_duration: int = 0
        
        self.shutdown = False
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, sig, frame):
        self.shutdown = True

    def get_team_logo(self, abbrev: str) -> str:
        """Download and resize the stock NHL team logo (e.g. wsh.png for WSH)."""
        team_code = (abbrev or self.team).upper()
        if team_code == self.team and self.my_logo:
            my_path = self.icon_dir / self.my_logo
            if my_path.exists():
                return self.my_logo
            logger.warning(
                f"Custom logo '{self.my_logo}' not found in {self.icon_dir}; "
                f"falling back to stock logo for {team_code}"
            )
            
        filename = f"{team_code.lower()}.png"
        path = self.icon_dir / filename
        
        if not path.exists():
            espn_slug = ESPN_SLUG_MAP.get(team_code, team_code.lower())
            url = f"https://a.espncdn.com/i/teamlogos/nhl/500/{espn_slug}.png"
            try:
                logger.info(f"Downloading stock logo for {team_code} from {url}")
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                img = Image.open(BytesIO(resp.content)).convert("RGBA")
                img = img.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
                img.save(path)
            except Exception as e:
                logger.error(f"Failed to fetch stock logo for {team_code} from {url}: {e}")
                
        return filename

    def fetch_game(self) -> Optional[dict]:
        url = "https://api-web.nhle.com/v1/score/now"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        for game in data.get("games", []):
            away = game.get("awayTeam", {}).get("abbrev")
            home = game.get("homeTeam", {}).get("abbrev")
            if away == self.team or home == self.team:
                return game
        return None

    def build_animation_elements(self) -> list:
        if self.animating_goal:
            return [
                {
                    "id": "anim",
                    "type": "text",
                    "text": f"GOAL {self.team} GOAL!",
                    "x": 0,
                    "y": 4,
                    "font": "small",
                    "color": "#FF0000FF",
                    "width": FRONT_WIDTH,
                    "align": "top_left",
                    "scroll_rate": 400,
                    "scroll_start_delay": 500,
                    "scroll_repeat_delay": 1000,
                    "display": "front",
                }
            ]
        elif self.animating_opp_goal:
            return [
                {
                    "id": "anim",
                    "type": "text",
                    "text": "OOF...",
                    "x": 36,
                    "y": 4,
                    "font": "small",
                    "color": "#4444FFFF",
                    "width": FRONT_WIDTH,
                    "align": "top_mid",
                    "scroll_rate": 0,
                    "display": "front",
                }
            ]
        return []

    def build_game_elements(self) -> list:
        if not self.current_game:
            return [
                {
                    "id": "no_game",
                    "type": "text",
                    "text": f"No {self.team} game today",
                    "x": 0,
                    "y": 4,
                    "font": "small",
                    "color": "#FFFFFFFF",
                    "width": FRONT_WIDTH,
                    "align": "top_left",
                    "scroll_rate": 400,
                    "scroll_start_delay": 1000,
                    "scroll_repeat_delay": 1500,
                    "display": "front",
                }
            ]
            
        away = self.current_game.get("awayTeam", {})
        home = self.current_game.get("homeTeam", {})
        
        my_team = away if away.get("abbrev") == self.team else home
        opp_team = home if away.get("abbrev") == self.team else away
        
        my_icon = self.get_team_logo(my_team.get("abbrev", self.team))
        opp_icon = self.get_team_logo(opp_team.get("abbrev", "NHL"))
        
        self.icon_uploader.upload(my_icon)
        self.icon_uploader.upload(opp_icon)
        
        state = self.current_game.get("gameState")
        
        elements = [
            {"id": "my_logo", "type": "image", "path": my_icon, "x": 0, "y": 0, "display": "front"},
            {"id": "opp_logo", "type": "image", "path": opp_icon, "x": 56, "y": 0, "display": "front"},
        ]
        
        if state in ["FUT", "PRE"]:
            elements.append({
                "id": "status",
                "type": "text",
                "text": "LATER",
                "x": CENTER_TEXT_X,
                "y": 4,
                "font": "small",
                "color": "#FFFFFFFF",
                "width": CENTER_TEXT_WIDTH,
                "align": "top_mid",
                "scroll_rate": 0,
                "display": "front",
            })
        elif state in ["FINAL", "OFF"]:
            elements.append({
                "id": "score",
                "type": "text",
                "text": f"{my_team.get('score', 0)}-{opp_team.get('score', 0)}",
                "x": CENTER_TEXT_X,
                "y": 0,
                "font": "small",
                "color": "#FFFFFFFF",
                "width": CENTER_TEXT_WIDTH,
                "align": "top_mid",
                "scroll_rate": 0,
                "display": "front",
            })
            elements.append({
                "id": "status",
                "type": "text",
                "text": "FINAL",
                "x": CENTER_TEXT_X,
                "y": 8,
                "font": "tiny",
                "color": "#AAAAAAFF",
                "width": CENTER_TEXT_WIDTH,
                "align": "top_mid",
                "scroll_rate": 0,
                "display": "front",
            })
        else:  # LIVE or CRIT
            elements.append({
                "id": "score",
                "type": "text",
                "text": f"{my_team.get('score', 0)}-{opp_team.get('score', 0)}",
                "x": CENTER_TEXT_X,
                "y": 0,
                "font": "small",
                "color": "#FFFFFFFF",
                "width": CENTER_TEXT_WIDTH,
                "align": "top_mid",
                "scroll_rate": 0,
                "display": "front",
            })
            
            clock = self.current_game.get("clock", {})
            time_rem = clock.get("timeRemaining", "")
            
            if clock.get("inIntermission", False):
                period = self.current_game.get("periodDescriptor", {}).get("number", 1)
                status_str = f"P{period} INT"
            else:
                if clock.get("running", False):
                    seconds_rem = clock.get("secondsRemaining", 0)
                    elapsed = time.time() - self.last_fetch
                    current_rem = max(0, int(seconds_rem - elapsed))
                    mins = current_rem // 60
                    secs = current_rem % 60
                    time_rem = f"{mins:02d}:{secs:02d}"
                period = self.current_game.get("periodDescriptor", {}).get("number", 1)
                status_str = f"P{period} {time_rem}".strip()
            
            elements.append({
                "id": "status",
                "type": "text",
                "text": status_str,
                "x": CENTER_TEXT_X,
                "y": 8,
                "font": "tiny",
                "color": "#AAAAAAFF",
                "width": CENTER_TEXT_WIDTH,
                "align": "top_mid",
                "scroll_rate": 0,
                "display": "front",
            })
            
        return elements

    def check_goals(self):
        if not self.current_game:
            return
            
        away = self.current_game.get("awayTeam", {})
        home = self.current_game.get("homeTeam", {})
        my_team = away if away.get("abbrev") == self.team else home
        opp_team = home if away.get("abbrev") == self.team else away
        
        my_score = my_team.get("score", 0)
        opp_score = opp_team.get("score", 0)
        
        if self.last_score != -1:
            if my_score > self.last_score:
                self.animating_goal = True
                self.animating_opp_goal = False
                self.animation_started_at = time.time()
                self._last_published_signature = None
            elif opp_score > self.last_opp_score:
                self.animating_opp_goal = True
                self.animating_goal = False
                self.animation_started_at = time.time()
                self._last_published_signature = None
                
        self.last_score = my_score
        self.last_opp_score = opp_score

    def _current_card_id(self) -> str:
        """Return a stable card_id for this team's game slot."""
        return self.team

    def _publish_if_needed(self, elements: list, priority: int, duration_seconds: int, now: float) -> None:
        """Publish to Redis only when content/priority changed or lease needs renewal."""
        card_id = self._current_card_id()
        sig = json.dumps(
            {"card_id": card_id, "priority": priority, "elements": elements},
            sort_keys=True,
        )
        lease_remaining = (self._last_published_at + self._last_published_duration) - now
        if sig == self._last_published_signature and lease_remaining > self.stale_buffer_seconds:
            return

        msg = {
            "app_id": self.app_id,
            "card_id": card_id,
            "priority": priority,
            "duration_seconds": max(1, int(duration_seconds)),
            "timestamp": now,
            "elements": elements,
        }
        self.redis_client.publish(self.redis_channel, json.dumps(msg))
        self._last_published_signature = sig
        self._last_published_at = now
        self._last_published_duration = max(1, int(duration_seconds))

    def run(self):
        logger.info(f"Starting NHL app for {self.team}")
        
        try:
            self.redis_client.ping()
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            return
            
        while not self.shutdown:
            now = time.time()
            
            if self.last_fetch == 0.0 or (now - self.last_fetch) >= self.interval_seconds:
                try:
                    self.current_game = self.fetch_game()
                    self.check_goals()
                except Exception as e:
                    logger.error(f"Failed to fetch game: {e}")
                self.last_fetch = now
                
            if self.animating_goal or self.animating_opp_goal:
                anim_elapsed = now - self.animation_started_at
                if anim_elapsed >= self.animation_duration:
                    self.animating_goal = False
                    self.animating_opp_goal = False
                    self.cycle_state = "active"
                    self.cycle_started_at = now
                    self._last_published_signature = None
            
            cycle_elapsed = now - self.cycle_started_at
            
            if self.animating_goal or self.animating_opp_goal:
                remaining_anim = max(1, math.ceil(self.animation_duration - (now - self.animation_started_at)))
                elements = self.build_animation_elements()
                self._publish_if_needed(elements, priority=100, duration_seconds=remaining_anim + 2, now=now)
            elif self.cycle_state == "active":
                if cycle_elapsed >= self.active_seconds:
                    # Enter resting window: stop publishing so ownership lapses
                    # and hands control back to stocks/weather.
                    self.cycle_state = "resting"
                    self.cycle_started_at = now
                    self._last_published_signature = None
                    logger.debug(f"Active window complete, resting for {self.rest_seconds}s")
                else:
                    remaining_active = max(1, math.ceil(self.active_seconds - cycle_elapsed))
                    elements = self.build_game_elements()
                    state = self.current_game.get("gameState") if self.current_game else "OFF"
                    prio = self.priority if state in ["LIVE", "CRIT"] else 40
                    self._publish_if_needed(
                        elements,
                        priority=prio,
                        duration_seconds=remaining_active + self.stale_buffer_seconds,
                        now=now,
                    )
            else:  # resting - stay silent so ownership expires cleanly
                if cycle_elapsed >= self.rest_seconds:
                    self.cycle_state = "active"
                    self.cycle_started_at = now
                    self._last_published_signature = None
                    logger.debug("Rest complete, starting active window")
                
            for _ in range(10):
                if self.shutdown:
                    break
                time.sleep(0.1)
                
        self.icon_uploader.close()
        logger.info("NHL app stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BusyBar NHL sub-app")
    parser.add_argument("--app_id", default="nhl")
    parser.add_argument("--team", default="WSH", help="Team abbreviation")
    parser.add_argument(
        "--my_logo",
        default=None,
        help="Optional custom logo filename in apps/nhl/icons/ (default: None, uses stock team logo e.g. wsh.png)",
    )
    parser.add_argument("--priority", type=int, default=70)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--active_seconds", type=float, default=30.0)
    parser.add_argument("--rest_seconds", type=float, default=30.0)
    parser.add_argument("--device_ip", default="busybar.local")
    parser.add_argument("--redis_host", default="localhost")
    parser.add_argument("--redis_port", type=int, default=6379)
    args = parser.parse_args()
    
    app = NHLApp(
        app_id=args.app_id,
        team=args.team,
        my_logo=args.my_logo,
        priority=args.priority,
        interval_seconds=args.interval,
        active_seconds=args.active_seconds,
        rest_seconds=args.rest_seconds,
        device_ip=args.device_ip,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
    )
    app.run()
