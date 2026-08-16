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

logging.basicConfig(
    level=logging.INFO,
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


class WeatherApp:
    """Fetches weather and publishes to Redis for the controller."""
    
    def __init__(
        self,
        app_id: str,
        priority: int,
        interval_seconds: int,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        device_ip: str = "10.0.4.20",
        city_name: str = "Washington DC",
        latitude: float = 38.9072,
        longitude: float = -77.0369,
    ):
        """Initialize weather app.
        
        Args:
            app_id: Unique identifier for this app
            priority: Display priority (0-100)
            interval_seconds: How often to fetch and publish
            redis_host: Redis server host
            redis_port: Redis server port
            device_ip: BusyBar device IP (for uploading icons)
            city_name: City name to display
            latitude: Location latitude
            longitude: Location longitude
        """
        self.app_id = app_id
        self.priority = priority
        self.interval_seconds = interval_seconds
        self.city_name = city_name
        self.latitude = latitude
        self.longitude = longitude
        self.device_ip = device_ip
        
        # Redis client
        self.redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True,
        )
        self.redis_channel = f"busybar:app:{app_id}"
        
        # Icon folder (same directory as this script)
        self.icon_folder = Path(__file__).parent / "weather" / "icons"
        
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
            (temperature, weather_code) tuple
            
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
            
            logger.debug(f"Fetched weather: {temp}°F, code={code}")
            return temp, code
        except Exception as e:
            logger.error(f"Failed to fetch weather: {e}")
            raise
    
    def get_icon_path(self, weather_code: int) -> str:
        """Get icon filename for weather code.
        
        Args:
            weather_code: Open-Meteo weather code
            
        Returns:
            Icon filename (e.g., "sun.png")
        """
        return WEATHER_ICON_MAP.get(weather_code, "sun.png")
    
    def upload_icon_to_device(self, icon_filename: str) -> str:
        """Upload icon to device and return path for display.
        
        Uploads from local /weather/icons/ folder to device.
        
        Args:
            icon_filename: Filename (e.g., "sun.png")
            
        Returns:
            Path to use in display elements (e.g., "sun.png")
        """
        local_path = self.icon_folder / icon_filename
        
        if not local_path.exists():
            logger.error(f"Icon not found: {local_path}")
            return "sun.png"  # Fallback
        
        try:
            # Read icon bytes
            with open(local_path, "rb") as f:
                icon_data = f.read()
            
            # Upload to device (use busybar_controller app name so controller can find icons)
            url = (
                f"http://{self.device_ip}/api/assets/upload"
                f"?application_name=busybar_controller"
                f"&file={icon_filename}"
            )
            
            response = requests.post(
                url,
                data=icon_data,
                headers={"Content-Type": "application/octet-stream"},
                timeout=5,
            )
            
            if response.status_code == 200:
                logger.debug(f"Uploaded icon: {icon_filename}")
                return icon_filename
            else:
                logger.warning(
                    f"Failed to upload icon {icon_filename}: "
                    f"{response.status_code} {response.text}"
                )
                return icon_filename  # Try anyway, device might have it cached
        
        except Exception as e:
            logger.error(f"Error uploading icon {icon_filename}: {e}")
            return icon_filename  # Try anyway
    
    def render_elements(
        self,
        temp: float,
        icon_filename: str,
    ) -> list:
        """Render BusyBar display elements (text only for now).
        
        Args:
            temp: Temperature in Fahrenheit
            icon_filename: Icon filename (unused, for future image support)
            
        Returns:
            List of BusyBar element dicts
        """
        return [
            {
                "id": "weather",
                "type": "text",
                "text": f"{self.city_name} {temp:.0f}F",
                "x": 10,
                "y": 8,
                "font": "small",
                "color": "#FFFF00FF",
                "width": 62,
                "scroll_rate": 0,
                "timeout": 6,
            },
        ]
    
    def publish_to_redis(self, elements: list) -> None:
        """Publish display message to Redis.
        
        Args:
            elements: List of BusyBar elements
        """
        message = {
            "app_id": self.app_id,
            "priority": self.priority,
            "duration_seconds": self.interval_seconds,
            "timestamp": time.time(),
            "elements": elements,
        }
        
        msg_json = json.dumps(message)
        self.redis_client.publish(self.redis_channel, msg_json)
        
        logger.info(
            f"Published to {self.redis_channel} "
            f"(priority={self.priority}, duration={self.interval_seconds}s)"
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
        while not self.shutdown:
            try:
                # Fetch weather
                temp, code = self.fetch_weather()
                
                # Get and upload icon
                icon_filename = self.get_icon_path(code)
                icon_path = self.upload_icon_to_device(icon_filename)
                
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
        help="Update interval in seconds (default: 300 = 5 min)",
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
        default="Washington DC",
        help="City name to display (default: Washington DC)",
    )
    parser.add_argument(
        "--lat",
        type=float,
        default=38.9072,
        help="Latitude (default: DC)",
    )
    parser.add_argument(
        "--lon",
        type=float,
        default=-77.0369,
        help="Longitude (default: DC)",
    )
    
    args = parser.parse_args()
    
    app = WeatherApp(
        app_id=args.app_id,
        priority=args.priority,
        interval_seconds=args.interval,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        device_ip=args.device_ip,
        city_name=args.city,
        latitude=args.lat,
        longitude=args.lon,
    )
    
    app.run()


if __name__ == "__main__":
    main()
