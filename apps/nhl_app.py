#!/usr/bin/env python3
"""NHL sports sub-app for BusyBar controller."""

import argparse
import json
import logging
import math
import os
import signal
import time
from pathlib import Path
from io import BytesIO
from typing import Optional, Sequence, Union

import redis
import requests
from PIL import Image

from icon_uploader import IconUploader
from sound_player import SoundPlayer

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

HORN_LEFT_ICON = "horn_left.png"
HORN_RIGHT_ICON = "horn_right.png"
NOTO_HORN_URL = "https://fonts.gstatic.com/s/e/notoemoji/latest/1f4e2/512.png"

DEFAULT_WSH_LOGO = "washington-capitals-seeklogo.png"
ALT_WSH_LOGOS = (
    "washington-capitals-seeklogo.png",
    "washington-capitals-seeklogo copy.png",
    "washington-capitals-seeklogo-2.png",
)

# Keyframes for goal celebration animation: moving left/right horns + bouncing/flashing text
GOAL_ANIMATION_FRAMES = [
    {
        "left_x": 0,
        "left_y": 0,
        "right_x": 56,
        "right_y": 0,
        "text_x": 36,
        "text_y": 0,
        "sub_x": 36,
        "sub_y": 8,
        "color": "#FF0000FF",
        "sub_color": "#FFFFFFFF",
    },
    {
        "left_x": 3,
        "left_y": -2,
        "right_x": 53,
        "right_y": 2,
        "text_x": 34,
        "text_y": 2,
        "sub_x": 38,
        "sub_y": 9,
        "color": "#FFD700FF",
        "sub_color": "#FF0000FF",
    },
    {
        "left_x": 6,
        "left_y": 2,
        "right_x": 50,
        "right_y": -2,
        "text_x": 38,
        "text_y": 0,
        "sub_x": 34,
        "sub_y": 8,
        "color": "#FFFFFFFF",
        "sub_color": "#FFD700FF",
    },
    {
        "left_x": 2,
        "left_y": -1,
        "right_x": 54,
        "right_y": 1,
        "text_x": 36,
        "text_y": 1,
        "sub_x": 36,
        "sub_y": 9,
        "color": "#FF3300FF",
        "sub_color": "#FFFFFFFF",
    },
    {
        "left_x": 5,
        "left_y": 1,
        "right_x": 51,
        "right_y": -1,
        "text_x": 35,
        "text_y": 0,
        "sub_x": 37,
        "sub_y": 8,
        "color": "#FFD700FF",
        "sub_color": "#FF0000FF",
    },
    {
        "left_x": 1,
        "left_y": 2,
        "right_x": 55,
        "right_y": -2,
        "text_x": 37,
        "text_y": 2,
        "sub_x": 35,
        "sub_y": 9,
        "color": "#FF0000FF",
        "sub_color": "#FFD700FF",
    },
]

# Map NHL API 3-letter team abbreviations to ESPN CDN logo slugs where they differ
ESPN_SLUG_MAP = {
    "LAK": "la",
    "NJD": "nj",
    "SJS": "sj",
    "TBL": "tb",
    "UTA": "utah",
}


def _parse_teams(
    team: Union[str, Sequence[str], None],
    teams: Union[str, Sequence[str], None] = None,
) -> list[str]:
    """Normalize one or more team abbreviations into an uppercase list."""
    raw_items: list[str] = []
    for source in (teams, team):
        if not source:
            continue
        if isinstance(source, str):
            raw_items.extend(part.strip().upper() for part in source.split(",") if part.strip())
        else:
            for item in source:
                if isinstance(item, str):
                    raw_items.extend(part.strip().upper() for part in item.split(",") if part.strip())
    deduped: list[str] = []
    for abbrev in raw_items:
        if abbrev not in deduped:
            deduped.append(abbrev)
    return deduped or ["WSH"]


def ensure_icon_16x16(path: Path) -> None:
    """Convert a local PNG icon in-place to 16x16 RGBA (aspect-ratio preserved) if needed."""
    if not path.exists():
        return
    try:
        with Image.open(path) as im:
            if im.size == (ICON_SIZE, ICON_SIZE) and im.mode == "RGBA":
                return
            rgba = im.convert("RGBA")
        scale = min(ICON_SIZE / rgba.width, ICON_SIZE / rgba.height)
        nw = max(1, round(rgba.width * scale))
        nh = max(1, round(rgba.height * scale))
        resized = rgba.resize((nw, nh), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        canvas.paste(resized, ((ICON_SIZE - nw) // 2, (ICON_SIZE - nh) // 2))
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        canvas.save(path)
    except Exception as e:
        logger.warning(f"Failed to normalize icon {path} to {ICON_SIZE}x{ICON_SIZE}: {e}")


class NHLApp:
    def __init__(
        self,
        app_id: str,
        team: Union[str, Sequence[str]] = "WSH",
        my_logo: Optional[str] = DEFAULT_WSH_LOGO,
        priority: int = 70,
        interval_seconds: int = 60,
        stale_buffer_seconds: int = 5,
        active_seconds: float = 30.0,
        rest_seconds: float = 30.0,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        device_ip: str = "busybar.local",
        sound: bool = True,
        goal_sound: str = "goal_horn.wav",
        bad_sound: str = "bad_sound.wav",
        enable_device_audio: bool = True,
        enable_local_audio: bool = True,
        teams: Optional[Union[str, Sequence[str]]] = None,
    ):
        self.app_id = app_id
        self.teams = _parse_teams(team, teams)
        self.team = self.teams[0]
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
        self._prepare_candidate_logos()
        
        self.sound_enabled = sound
        self.goal_sound = goal_sound
        self.bad_sound = bad_sound
        self.sound_dir = Path(__file__).parent / "nhl" / "sounds"
        self.sound_dir.mkdir(parents=True, exist_ok=True)
        self.sound_player: Optional[SoundPlayer] = None
        if self.sound_enabled:
            self.sound_player = SoundPlayer(
                device_ip=device_ip,
                sound_folder=self.sound_dir,
                enable_device_audio=enable_device_audio,
                enable_local_audio=enable_local_audio,
            )
        
        self.last_score = -1
        self.last_opp_score = -1
        self._game_scores: dict[str, tuple[int, int]] = {}
        self.last_fetch = 0.0
        self.current_game: Optional[dict] = None
        self.current_games: list[dict] = []
        
        # Animation state
        self.animating_goal = False
        self.animating_opp_goal = False
        self.scoring_team: str = self.team
        self.animation_started_at = 0.0
        self.animation_duration = 15.0
        self.animation_frame = 0
        
        # Deduplication tracking so static/scrolling text isn't republished every second
        self._last_published_signature: Optional[str] = None
        self._last_published_at: float = 0.0
        self._last_published_duration: int = 0
        
        self.shutdown = False
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, sig, frame):
        self.shutdown = True

    def _configured_teams(self) -> list[str]:
        """Return all configured team abbreviations (uppercase)."""
        return _parse_teams(self.team, self.teams)

    def _prepare_candidate_logos(self) -> None:
        """Ensure dropped Capitals logos are 16x16 RGBA and uploaded to the device."""
        for fname in ALT_WSH_LOGOS:
            p = self.icon_dir / fname
            if p.exists():
                ensure_icon_16x16(p)
                self.icon_uploader.upload(fname)

    def get_team_logo(self, abbrev: str) -> str:
        """Download and resize the NHL team logo (or custom my_logo if configured)."""
        team_code = (abbrev or self.team).upper()
        if team_code == self.team and self.my_logo:
            my_path = self.icon_dir / self.my_logo
            if my_path.exists():
                ensure_icon_16x16(my_path)
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
        else:
            ensure_icon_16x16(path)
                
        return filename

    def ensure_horn_icons(self) -> tuple[str, str]:
        """Ensure left and right 16x16 Noto Emoji horn icons exist locally and upload them."""
        left_path = self.icon_dir / HORN_LEFT_ICON
        right_path = self.icon_dir / HORN_RIGHT_ICON
        
        if not left_path.exists() or not right_path.exists():
            try:
                logger.info(f"Downloading Noto Emoji horn icon from {NOTO_HORN_URL}")
                resp = requests.get(NOTO_HORN_URL, timeout=10)
                resp.raise_for_status()
                img = Image.open(BytesIO(resp.content)).convert("RGBA")
                img_left = img.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
                img_right = img_left.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                img_left.save(left_path)
                img_right.save(right_path)
            except Exception as e:
                logger.error(f"Failed to fetch horn icon from {NOTO_HORN_URL}: {e}")
                
        self.icon_uploader.upload(HORN_LEFT_ICON)
        self.icon_uploader.upload(HORN_RIGHT_ICON)
        return HORN_LEFT_ICON, HORN_RIGHT_ICON

    def fetch_games(self) -> list[dict]:
        """Fetch all active/scheduled games today involving any configured team."""
        url = "https://api-web.nhle.com/v1/score/now"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        configured = set(self._configured_teams())
        matched_games: list[dict] = []
        seen_keys: set[str] = set()
        
        for game in data.get("games", []):
            away = (game.get("awayTeam", {}).get("abbrev") or "").upper()
            home = (game.get("homeTeam", {}).get("abbrev") or "").upper()
            if away in configured or home in configured:
                key = str(game.get("id") or f"{away}@{home}")
                if key not in seen_keys:
                    seen_keys.add(key)
                    matched_games.append(game)
        self.current_games = matched_games
        return matched_games

    def fetch_game(self) -> Optional[dict]:
        games = self.fetch_games()
        return games[0] if games else None

    def build_animation_elements(self) -> list:
        if self.animating_goal:
            horn_left, horn_right = self.ensure_horn_icons()
            frame = GOAL_ANIMATION_FRAMES[self.animation_frame % len(GOAL_ANIMATION_FRAMES)]
            self.animation_frame += 1
            scorer = (self.scoring_team or self.team).upper()
            
            return [
                {
                    "id": "horn_left",
                    "type": "image",
                    "path": horn_left,
                    "x": int(frame["left_x"]),
                    "y": int(frame["left_y"]),
                    "display": "front",
                },
                {
                    "id": "horn_right",
                    "type": "image",
                    "path": horn_right,
                    "x": int(frame["right_x"]),
                    "y": int(frame["right_y"]),
                    "display": "front",
                },
                {
                    "id": "anim",
                    "type": "text",
                    "text": "GOAL!",
                    "x": int(frame["text_x"]),
                    "y": int(frame["text_y"]),
                    "font": "small",
                    "color": frame["color"],
                    "width": CENTER_TEXT_WIDTH,
                    "align": "top_mid",
                    "scroll_rate": 0,
                    "scroll_start_delay": 0,
                    "scroll_repeat_delay": 0,
                    "display": "front",
                },
                {
                    "id": "anim_team",
                    "type": "text",
                    "text": f"{scorer} GOAL!",
                    "x": int(frame["sub_x"]),
                    "y": int(frame["sub_y"]),
                    "font": "tiny",
                    "color": frame["sub_color"],
                    "width": CENTER_TEXT_WIDTH,
                    "align": "top_mid",
                    "scroll_rate": 0,
                    "scroll_start_delay": 0,
                    "scroll_repeat_delay": 0,
                    "display": "front",
                },
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
                    "scroll_start_delay": 0,
                    "scroll_repeat_delay": 0,
                    "display": "front",
                }
            ]
        return []

    def build_game_elements(self, game: Optional[dict] = None) -> list:
        target_game = game if game is not None else self.current_game
        if not target_game:
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
            
        # Away team is always listed first (left icon, first score)
        # Home team (in the game) is always listed second (right icon, second score)
        away = target_game.get("awayTeam", {})
        home = target_game.get("homeTeam", {})
        
        away_abbrev = away.get("abbrev", "NHL")
        home_abbrev = home.get("abbrev", "NHL")
        away_score = away.get("score", 0)
        home_score = home.get("score", 0)
        
        away_icon = self.get_team_logo(away_abbrev)
        home_icon = self.get_team_logo(home_abbrev)
        
        self.icon_uploader.upload(away_icon)
        self.icon_uploader.upload(home_icon)
        
        state = target_game.get("gameState")
        
        elements = [
            {"id": "away_logo", "type": "image", "path": away_icon, "x": 0, "y": 0, "display": "front"},
            {"id": "home_logo", "type": "image", "path": home_icon, "x": 56, "y": 0, "display": "front"},
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
                "scroll_start_delay": 0,
                "scroll_repeat_delay": 0,
                "display": "front",
            })
        elif state in ["FINAL", "OFF"]:
            elements.append({
                "id": "score",
                "type": "text",
                "text": f"{away_score}-{home_score}",
                "x": CENTER_TEXT_X,
                "y": 0,
                "font": "small",
                "color": "#FFFFFFFF",
                "width": CENTER_TEXT_WIDTH,
                "align": "top_mid",
                "scroll_rate": 0,
                "scroll_start_delay": 0,
                "scroll_repeat_delay": 0,
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
                "scroll_start_delay": 0,
                "scroll_repeat_delay": 0,
                "display": "front",
            })
        else:  # LIVE or CRIT
            elements.append({
                "id": "score",
                "type": "text",
                "text": f"{away_score}-{home_score}",
                "x": CENTER_TEXT_X,
                "y": 0,
                "font": "small",
                "color": "#FFFFFFFF",
                "width": CENTER_TEXT_WIDTH,
                "align": "top_mid",
                "scroll_rate": 0,
                "scroll_start_delay": 0,
                "scroll_repeat_delay": 0,
                "display": "front",
            })
            
            clock = target_game.get("clock", {})
            time_rem = clock.get("timeRemaining", "")
            
            if clock.get("inIntermission", False):
                period = target_game.get("periodDescriptor", {}).get("number", 1)
                status_str = f"P{period} INT"
            else:
                if clock.get("running", False):
                    seconds_rem = clock.get("secondsRemaining", 0)
                    elapsed = time.time() - self.last_fetch
                    current_rem = max(0, int(seconds_rem - elapsed))
                    mins = current_rem // 60
                    secs = current_rem % 60
                    time_rem = f"{mins:02d}:{secs:02d}"
                period = target_game.get("periodDescriptor", {}).get("number", 1)
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
                "scroll_start_delay": 0,
                "scroll_repeat_delay": 0,
                "display": "front",
            })
            
        return elements

    def _game_key(self, game: dict) -> str:
        away_abbrev = (game.get("awayTeam", {}).get("abbrev") or "").upper()
        home_abbrev = (game.get("homeTeam", {}).get("abbrev") or "").upper()
        if game.get("id") is not None:
            return f"{game.get('id')}:{away_abbrev}@{home_abbrev}"
        return f"{away_abbrev}@{home_abbrev}"

    def check_goals(self):
        games_to_check: list[dict] = []
        if self.current_game is not None:
            games_to_check.append(self.current_game)
        for g in self.current_games:
            if g not in games_to_check:
                games_to_check.append(g)
        if not games_to_check:
            return
            
        configured = set(self._configured_teams())
        
        for game in games_to_check:
            away = game.get("awayTeam", {})
            home = game.get("homeTeam", {})
            away_abbrev = (away.get("abbrev") or "").upper()
            home_abbrev = (home.get("abbrev") or "").upper()
            away_score = away.get("score", 0)
            home_score = home.get("score", 0)
            
            away_is_configured = away_abbrev in configured
            home_is_configured = home_abbrev in configured
            
            my_team = away if away_is_configured else home
            opp_team = home if away_is_configured else away
            my_score = my_team.get("score", 0)
            opp_score = opp_team.get("score", 0)
            
            key = self._game_key(game)
            has_baseline = key in self._game_scores and not (
                game is self.current_game and self.last_score == -1
            )
            
            if has_baseline:
                prev_away_score, prev_home_score = self._game_scores[key]
                away_scored = away_score > prev_away_score
                home_scored = home_score > prev_home_score
                
                configured_scorers: list[str] = []
                opp_scorers: list[str] = []
                
                if away_scored:
                    if away_is_configured:
                        configured_scorers.append(away_abbrev)
                    else:
                        opp_scorers.append(away_abbrev)
                if home_scored:
                    if home_is_configured:
                        configured_scorers.append(home_abbrev)
                    else:
                        opp_scorers.append(home_abbrev)
                
                if configured_scorers:
                    for scorer in configured_scorers:
                        logger.info(
                            f"GOAL for configured team {scorer}! Playing goal horn ({self.goal_sound})"
                        )
                        self.scoring_team = scorer
                        self.animating_goal = True
                        self.animating_opp_goal = False
                        self.animation_started_at = time.time()
                        self.animation_frame = 0
                        self._last_published_signature = None
                        if self.sound_player:
                            self.sound_player.play(self.goal_sound)
                elif opp_scorers:
                    for opp_scorer in opp_scorers:
                        logger.info(
                            f"Opponent {opp_scorer} scored! Playing bad sound ({self.bad_sound})"
                        )
                        self.scoring_team = opp_scorer
                        self.animating_opp_goal = True
                        self.animating_goal = False
                        self.animation_started_at = time.time()
                        self.animation_frame = 0
                        self._last_published_signature = None
                        if self.sound_player:
                            self.sound_player.play(self.bad_sound)
                            
            self._game_scores[key] = (away_score, home_score)
            if game is self.current_game:
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
        logger.info(f"Starting NHL app for {', '.join(self._configured_teams())}")
        
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
                
            sleep_steps = 5 if (self.animating_goal or self.animating_opp_goal) else 10
            for _ in range(sleep_steps):
                if self.shutdown:
                    break
                time.sleep(0.1)
                
        self.icon_uploader.close()
        if self.sound_player:
            self.sound_player.close()
        logger.info("NHL app stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BusyBar NHL sub-app")
    parser.add_argument("--app_id", default="nhl")
    parser.add_argument(
        "--team",
        default="WSH",
        help="Team abbreviation(s), comma-separated for multiple configured teams (e.g. WSH or WSH,CAR)",
    )
    parser.add_argument(
        "--teams",
        default=None,
        help="Optional comma-separated list of configured team abbreviations (e.g. WSH,CAR)",
    )
    parser.add_argument(
        "--my_logo",
        default=DEFAULT_WSH_LOGO,
        help=(
            f"Custom logo filename in apps/nhl/icons/ "
            f"(default: {DEFAULT_WSH_LOGO}; alternate: 'washington-capitals-seeklogo copy.png' or 'washington-capitals-seeklogo-2.png')"
        ),
    )
    parser.add_argument("--priority", type=int, default=70)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--active_seconds", type=float, default=30.0)
    parser.add_argument("--rest_seconds", type=float, default=30.0)
    parser.add_argument("--device_ip", default="busybar.local")
    parser.add_argument("--redis_host", default="localhost")
    parser.add_argument("--redis_port", type=int, default=6379)
    parser.add_argument(
        "--sound",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable goal and opponent score sound effects (default: on)",
    )
    parser.add_argument(
        "--goal_sound",
        default="goal_horn.wav",
        help="Filename of goal sound in apps/nhl/sounds/ (default: goal_horn.wav)",
    )
    parser.add_argument(
        "--bad_sound",
        default="bad_sound.wav",
        help="Filename of opponent score sound in apps/nhl/sounds/ (default: bad_sound.wav)",
    )
    parser.add_argument(
        "--device_audio",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Play sound on BusyBar device via /api/audio/play (default: on)",
    )
    parser.add_argument(
        "--local_audio",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Play sound on local host speakers via afplay (default: on)",
    )
    args = parser.parse_args()
    
    app = NHLApp(
        app_id=args.app_id,
        team=args.team,
        teams=args.teams,
        my_logo=args.my_logo,
        priority=args.priority,
        interval_seconds=args.interval,
        active_seconds=args.active_seconds,
        rest_seconds=args.rest_seconds,
        device_ip=args.device_ip,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        sound=args.sound,
        goal_sound=args.goal_sound,
        bad_sound=args.bad_sound,
        enable_device_audio=args.device_audio,
        enable_local_audio=args.local_audio,
    )
    app.run()
