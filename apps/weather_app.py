#!/usr/bin/env python3
"""Weather sub-app for BusyBar controller.

Fetches weather from Open-Meteo API and publishes display elements to Redis.
Used by the main BusyBar controller to manage display priority.

Usage:
    python3 apps/weather_app.py --app_id weather --priority 50 --interval 300
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import redis
import requests

from icon_uploader import IconUploader

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] weather_app: %(message)s",
)
logger = logging.getLogger(__name__)


# Weather code to icon filename mapping (Open-Meteo codes)
WEATHER_ICON_MAP = {
    0: "sun.png",      # Clear sky
    1: "partly.png",   # Mainly clear
    2: "partly.png",   # Partly cloudy
    3: "cloud.png",    # Overcast
    45: "fog.png",     # Fog
    48: "fog.png",     # Depositing rime fog
    51: "rain.png",    # Drizzle
    53: "rain.png",
    55: "rain.png",
    61: "rain.png",    # Rain
    63: "rain.png",
    65: "rain.png",
    71: "snow.png",    # Snow
    73: "snow.png",
    75: "snow.png",
    80: "rain.png",    # Rain showers
    81: "rain.png",
    82: "rain.png",
    95: "rain.png",    # Thunderstorm
    96: "rain.png",
    99: "rain.png",
}

# Night-time icon overrides. Only conditions whose daytime icon depicts the
# sun need a swap after dark (clear / mainly clear / partly cloudy); cloud,
# fog, rain, and snow icons don't show the sun so they look correct at any
# hour and are left alone.
NIGHT_ICON_MAP = {
    "sun.png": "moon.png",
    "partly.png": "moon.png",
}


class WeatherApp:
    """Fetches weather and publishes to Redis for the controller."""
    
    def __init__(
        self,
        app_id: str,
        priority: int,
        interval_seconds: int,
        stale_buffer_seconds: int = 10,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        device_ip: str = "10.0.4.20",
        city_name: str = "Washington DC",
        latitude: float = 38.9072,
        longitude: float = -77.0369,
        show_city: bool = True,
    ):
        """Initialize weather app.
        
        Args:
            app_id: Unique identifier for this app
            priority: Display priority (0-100)
            interval_seconds: How often to fetch weather from the API
            stale_buffer_seconds: Grace period added on top of
                interval_seconds for the published message's duration_seconds.
                As long as the app is alive and fetching successfully, each
                new publish refreshes the display before the previous
                message would expire, so weather stays up as the effective
                "default" screen instead of blipping back to idle between
                fetches. If the app stops publishing (crashed, killed, API
                down past this buffer), the stale message eventually
                expires and idle correctly takes back over.
            redis_host: Redis server host
            redis_port: Redis server port
            device_ip: BusyBar device IP (for uploading icons)
            city_name: City name to display
            latitude: Location latitude
            longitude: Location longitude
            show_city: Whether to prefix the display text with city_name.
                Defaults on. The front display is only 72px wide, so with
                the city name included the text scrolls to fit next to the
                icon rather than getting clipped.
        """
        self.app_id = app_id
        self.priority = priority
        self.interval_seconds = interval_seconds
        self.duration_seconds = interval_seconds + stale_buffer_seconds
        self.city_name = city_name
        self.latitude = latitude
        self.longitude = longitude
        self.device_ip = device_ip
        self.show_city = show_city
        
        # Redis client
        self.redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True,
        )
        self.redis_channel = f"busybar:app:{app_id}"
        
        # BusyBar device client, wrapped in the shared IconUploader (used
        # by every sub-app that needs to push local icon PNGs to the
        # device before the controller can reference them by filename).
        self.icon_uploader = IconUploader(
            device_ip,
            icon_folder=Path(__file__).parent / "weather" / "icons",
        )
        
        # Shutdown flag
        self.shutdown = False
        
        # Signal handlers
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
    
    def _on_signal(self, sig, frame):
        """Handle shutdown signal."""
        logger.info("Shutdown signal received")
        self.shutdown = True
    
    def fetch_weather(self) -> tuple:
        """Fetch weather from Open-Meteo API.
        
        Returns:
            (temperature, weather_code, is_day) tuple. is_day is Open-Meteo's
            own day/night flag for the request's lat/lon (True for day,
            False for night) - it's an astronomical calculation based on
            sunrise/sunset at that location, not just a fixed local clock
            time, so it stays correct across seasons and locations.
            
        Raises:
            Exception on API error
        """
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.latitude}&longitude={self.longitude}"
            f"&current_weather=true&temperature_unit=fahrenheit"
        )
        
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            current = data.get("current_weather", {})
            
            temp = current.get("temperature")
            code = current.get("weathercode", 0)
            is_day = bool(current.get("is_day", 1))
            
            logger.debug(
                f"Fetched weather: {temp}°F, code={code}, "
                f"is_day={is_day}"
            )
            return temp, code, is_day
        except Exception as e:
            logger.error(f"Failed to fetch weather: {e}")
            raise
    
    def get_icon_path(self, weather_code: int, is_day: bool = True) -> str:
        """Get icon filename for weather code, day or night.
        
        Args:
            weather_code: Open-Meteo weather code
            is_day: Whether it's currently daytime at the location. When
                False, the daytime icon is looked up first and then swapped
                via NIGHT_ICON_MAP if a night equivalent exists (e.g.
                "sun.png" -> "moon.png"). Conditions without a sun in their
                icon (cloud/fog/rain/snow) have no entry in NIGHT_ICON_MAP
                and pass through unchanged.
            
        Returns:
            Icon filename (e.g., "sun.png", "moon.png")
        """
        icon = WEATHER_ICON_MAP.get(weather_code, "sun.png")
        if not is_day:
            icon = NIGHT_ICON_MAP.get(icon, icon)
        return icon
    
    
    def render_elements(
        self,
        temp: float,
        icon_filename: str,
    ) -> list:
        """Render BusyBar display elements: icon + temperature text.
        
        Front display is 72x16. The icon fills the full 16px height on the
        left (x=0..16), and text takes the remaining width on the right
        (x=19..72, 53px, with a 3px gap from the icon). Icons are 16x16
        already so no width/height needs to be specified on the image
        element.
        
        Args:
            temp: Temperature in Fahrenheit
            icon_filename: Filename of the icon already uploaded to the device
            
        Returns:
            List of BusyBar element dicts
        """
        text = f"{self.city_name} {temp:.0f}F" if self.show_city else f"{temp:.0f}F"


        return [
            {
                "id": "icon",
                "type": "image",
                "path": icon_filename,
                "x": 0,
                "y": 0,
                "display": "front"
            },
            {
                "id": "city",
                "type": "text",
                "text": self.city_name,
                "x": 18,
                "y": 0,
                "font": "small",
                "color": "#FFFFFFFF",
                "width": 54,
                "display": "front",
                "scroll_rate": 500
            },
            {
                "id": "temp",
                "type": "text",
                "text": f"{temp}°F",
                "x": 18,
                "y": 6,
                "font": "normal",
                "color": "#FFFF00FF",
                "width": 54,
                "scroll_rate": 60,
                "display": "front"
            }
        ]
    
    def publish_to_redis(self, elements: list) -> None:
        """Publish display message to Redis.
        
        Args:
            elements: List of BusyBar elements
        """
        message = {
            "app_id": self.app_id,
            "priority": self.priority,
            "duration_seconds": self.duration_seconds,
            "timestamp": time.time(),
            "elements": elements,
        }
        
        msg_json = json.dumps(message)
        self.redis_client.publish(self.redis_channel, msg_json)
        
        logger.info(
            f"Published to {self.redis_channel} "
            f"(priority={self.priority}, duration={self.duration_seconds}s)"
        )
    
    def run(self) -> None:
        """Main loop: fetch weather, render, publish."""
        logger.info(
            f"Starting weather app: {self.city_name} "
            f"(lat={self.latitude}, lon={self.longitude})"
        )
        logger.info(f"Update interval: {self.interval_seconds}s, priority: {self.priority}")
        
        # Test Redis connection
        try:
            self.redis_client.ping()
            logger.info("Connected to Redis")
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            return
        
        # Main loop
        try:
            while not self.shutdown:
                try:
                    # Fetch weather
                    temp, code, is_day = self.fetch_weather()
                    
                    # Get and upload icon (day/night aware)
                    icon_filename = self.get_icon_path(code, is_day)
                    icon_path = self.icon_uploader.upload(
                        icon_filename, fallback="sun.png"
                    )
                    
                    # Render elements
                    elements = self.render_elements(temp, icon_path)
                    
                    # Publish to Redis
                    self.publish_to_redis(elements)
                
                except Exception as e:
                    logger.error(f"Error in publish loop: {e}")
                
                # Wait for next update (check shutdown frequently, every 100ms)
                for _ in range(int(self.interval_seconds * 10)):
                    if self.shutdown:
                        break
                    time.sleep(0.1)
        finally:
            self.icon_uploader.close()
        
        logger.info("Weather app stopped")


def main():
    """Entry point for weather app."""
    parser = argparse.ArgumentParser(
        description="BusyBar weather sub-app (publishes to Redis)"
    )
    parser.add_argument(
        "--app_id",
        default="weather",
        help="Unique app identifier (default: weather)",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=50,
        help="Display priority 0-100 (default: 50)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="How often to fetch weather, in seconds (default: 300 = 5 min)",
    )
    parser.add_argument(
        "--stale_buffer",
        type=int,
        default=10,
        help=(
            "Grace period (seconds) added on top of --interval before a "
            "published weather display is considered stale (default: 10). "
            "While the app is running and fetching successfully, each "
            "publish refreshes the display before this expires, so weather "
            "stays up as the default screen instead of blipping back to "
            "idle between fetches. If the app stops updating for longer "
            "than interval + this buffer, idle takes back over."
        ),
    )
    parser.add_argument(
        "--show_city",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Prefix display text with the city name, scrolling to fit "
            "(default: on). Use --no-show_city for temperature only."
        ),
    )
    parser.add_argument(
        "--redis_host",
        default="localhost",
        help="Redis host (default: localhost)",
    )
    parser.add_argument(
        "--redis_port",
        type=int,
        default=6379,
        help="Redis port (default: 6379)",
    )
    parser.add_argument(
        "--device_ip",
        default="10.0.4.20",
        help="BusyBar device IP (default: 10.0.4.20)",
    )
    parser.add_argument(
        "--city",
        default="Reston, VA",
        help="City name to display (default: Washington DC)",
    )
    parser.add_argument(
        "--lat",
        type=float,
        default=38.935094,
        help="Latitude (default: DC)",
    )
    parser.add_argument(
        "--lon",
        type=float,
        default=-77.366724,
        help="Longitude (default: DC)",
    )
    
    args = parser.parse_args()
    
    app = WeatherApp(
        app_id=args.app_id,
        priority=args.priority,
        interval_seconds=args.interval,
        stale_buffer_seconds=args.stale_buffer,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        device_ip=args.device_ip,
        city_name=args.city,
        latitude=args.lat,
        longitude=args.lon,
        show_city=args.show_city,
    )
    
    app.run()


if __name__ == "__main__":
    main()