#!/usr/bin/env python3
"""Weather sub-app for BusyBar controller.

Fetches current observed weather from the National Weather Service (NWS)
API and publishes display elements to Redis.

The NWS observation API is used intentionally instead of a forecast/model
API because this app is intended to show what is happening RIGHT NOW.

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


# NWS condition text -> existing BusyBar icon.
#
# These deliberately use the existing local icon assets.  We are changing
# the weather data source, not the icon system.
def get_icon_for_condition(description: str) -> str:
    """Map an NWS observed weather description to an existing icon."""

    text = (description or "").lower()

    # Thunderstorms first so they don't get classified as ordinary rain.
    if any(
        word in text
        for word in (
            "thunderstorm",
            "thunderstorms",
            "t-storm",
            "tstm",
        )
    ):
        return "rain.png"

    # Snow / freezing precipitation.
    if any(
        word in text
        for word in (
            "snow",
            "sleet",
            "ice pellets",
            "freezing rain",
            "wintry mix",
        )
    ):
        return "snow.png"

    # Rain / drizzle / showers.
    if any(
        word in text
        for word in (
            "rain",
            "drizzle",
            "shower",
            "showers",
        )
    ):
        return "rain.png"

    # Fog / mist.
    if any(
        word in text
        for word in (
            "fog",
            "mist",
            "haze",
        )
    ):
        return "fog.png"

    # Clear conditions.
    if any(
        word in text
        for word in (
            "fair",
            "clear",
            "sunny",
        )
    ):
        return "sun.png"

    # Partly cloudy / mostly clear.
    if any(
        phrase in text
        for phrase in (
            "partly cloudy",
            "mostly clear",
            "mostly sunny",
            "partly sunny",
        )
    ):
        return "partly.png"

    # Cloudy / overcast.
    if any(
        word in text
        for word in (
            "cloudy",
            "overcast",
        )
    ):
        return "cloud.png"

    # If NWS gives us something unusual that we don't recognize, cloud is
    # safer than showing a misleading sunny icon.
    return "cloud.png"


# Existing night-time icon behavior.
# Only icons containing a sun are changed after dark.
NIGHT_ICON_MAP = {
    "sun.png": "moon.png",
    "partly.png": "moon.png",
}


class WeatherApp:
    """Fetch current observed weather and publish it to Redis."""

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
            app_id: Unique identifier for this app.
            priority: Display priority (0-100).
            interval_seconds: How often to fetch current observations.
            stale_buffer_seconds: Grace period added to the published
                duration.
            redis_host: Redis server host.
            redis_port: Redis server port.
            device_ip: BusyBar device IP.
            city_name: City name to display.
            latitude: Location latitude.
            longitude: Location longitude.
            show_city: Whether to prefix the display with city_name.
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

        # NWS requires a User-Agent identifying the application.
        # An email address is not strictly required, but is recommended
        # by the NWS API guidance.
        self.nws_user_agent = os.environ.get(
            "NWS_USER_AGENT",
            "BusyBarAppOfApps (weather app)",
        )

        self.http_headers = {
            "User-Agent": self.nws_user_agent,
            "Accept": "application/geo+json",
        }

        # Cache the discovered observation station.  We don't need to
        # rediscover it every five minutes.
        self.observation_station = None

        # Redis client.
        self.redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True,
        )

        self.redis_channel = f"busybar:app:{app_id}"

        # BusyBar device client.
        self.icon_uploader = IconUploader(
            device_ip,
            icon_folder=Path(__file__).parent / "weather" / "icons",
        )

        self.shutdown = False

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, sig, frame):
        """Handle shutdown signal."""
        logger.info("Shutdown signal received")
        self.shutdown = True

    def _discover_observation_station(self) -> str:
        """Find the NWS observation station nearest the configured location.

        Returns:
            NWS station identifier such as KIAD or KDCA.

        Raises:
            Exception on API/network error or if no station is available.
        """

        url = (
            "https://api.weather.gov/points/"
            f"{self.latitude},{self.longitude}"
        )

        logger.debug(
            "Finding NWS observation station for "
            f"{self.latitude},{self.longitude}"
        )

        resp = requests.get(
            url,
            headers=self.http_headers,
            timeout=10,
        )
        resp.raise_for_status()

        data = resp.json()

        observation_stations_url = (
            data.get("properties", {}).get("observationStations")
        )

        if not observation_stations_url:
            raise RuntimeError(
                "NWS points response did not provide observationStations"
            )

        resp = requests.get(
            observation_stations_url,
            headers=self.http_headers,
            timeout=10,
        )
        resp.raise_for_status()

        stations = resp.json().get("features", [])

        if not stations:
            raise RuntimeError(
                "NWS returned no observation stations for the location"
            )

        station_id = stations[0].get("properties", {}).get("stationIdentifier")

        if not station_id:
            raise RuntimeError(
                "NWS observation station had no stationIdentifier"
            )

        logger.info(
            f"Selected NWS observation station: {station_id}"
        )

        return station_id

    def _get_observation_station(self) -> str:
        """Return cached station ID, discovering it if necessary."""

        if self.observation_station:
            return self.observation_station

        self.observation_station = self._discover_observation_station()

        return self.observation_station

    def fetch_weather(self) -> tuple:
        """Fetch the latest NWS observed weather.

        Returns:
            (temperature_f, description, is_day, station_id) tuple.

            temperature_f:
                Current observed temperature in Fahrenheit.

            description:
                NWS human-readable observed condition such as
                "Thunderstorms and Rain".

            is_day:
                Whether the observation is during daytime.

            station_id:
                NWS station identifier.

        Raises:
            Exception on API error or invalid observation data.
        """

        station_id = self._get_observation_station()

        url = (
            f"https://api.weather.gov/stations/"
            f"{station_id}/observations/latest"
        )

        logger.debug(
            f"Fetching latest NWS observation from {station_id}"
        )

        try:
            resp = requests.get(
                url,
                headers=self.http_headers,
                timeout=10,
            )
            resp.raise_for_status()

            data = resp.json()
            properties = data.get("properties", {})

            # NWS temperature is returned in Celsius with a unitCode.
            temperature = properties.get("temperature", {})
            temp_c = temperature.get("value")

            if temp_c is None:
                raise RuntimeError(
                    f"NWS observation from {station_id} has no temperature"
                )

            temp_f = (float(temp_c) * 9.0 / 5.0) + 32.0

            description = (
                properties.get("textDescription")
                or "Unknown"
            )

            # NWS observations include an isDaytime boolean.
            # Fall back to the icon URL if it isn't present.
            is_day = properties.get("isDaytime")

            if is_day is None:
                is_day = True

                icon_url = properties.get("icon")

                if icon_url:
                    is_day = "/day/" in icon_url

            is_day = bool(is_day)

            logger.info(
                f"NWS observation: {temp_f:.1f}°F, "
                f"condition={description!r}, "
                f"is_day={is_day}, "
                f"station={station_id}"
            )

            return temp_f, description, is_day, station_id

        except requests.HTTPError as e:
            # A station can occasionally go offline or return a bad
            # observation.  Clear the cached station so the next attempt
            # can discover another nearby station.
            logger.warning(
                f"NWS observation request failed for {station_id}: {e}"
            )

            self.observation_station = None
            raise

        except Exception as e:
            logger.error(
                f"Failed to fetch NWS weather: {e}"
            )
            raise

    def get_icon_path(
        self,
        description: str,
        is_day: bool = True,
    ) -> str:
        """Get existing BusyBar icon filename for an NWS condition.

        Args:
            description: NWS observed condition.
            is_day: Whether the observation is during daytime.

        Returns:
            Existing icon filename.
        """

        icon = get_icon_for_condition(description)

        if not is_day:
            icon = NIGHT_ICON_MAP.get(icon, icon)

        return icon

    def render_elements(
        self,
        temp: float,
        description: str,
        icon_filename: str,
    ) -> list:
        """Render BusyBar display elements.

        Front display is 72x16.

        The condition is included because this app is intended to show
        actual current conditions, not just temperature.

        Example:
            Reston, VA
            82°F Thunderstorms and Rain

        The text scrolls automatically when it exceeds the available width.
        """

        # Keep the city element separate, as in the existing implementation.
        # This preserves the current display layout.
        condition_text = description.strip()

        if self.show_city:
            city_text = self.city_name
        else:
            city_text = ""

        return [
            {
                "id": "icon",
                "type": "image",
                "path": icon_filename,
                "x": 0,
                "y": 0,
                "display": "front",
            },
            {
                "id": "city",
                "type": "text",
                "text": city_text,
                "x": 18,
                "y": 0,
                "font": "small",
                "color": "#FFFFFFFF",
                "width": 54,
                "scroll_rate": 500,
                "scroll_start_delay": 0,
                "scroll_repeat_delay": 0,
                "display": "front",
            },
            {
                "id": "temp",
                "type": "text",
                "text": f"{temp:.0f}°F {condition_text}",
                "x": 18,
                "y": 6,
                "font": "normal",
                "color": "#FFFF00FF",
                "width": 54,
                "scroll_rate": 1200,
                "scroll_start_delay": 0,
                "scroll_repeat_delay": 0,
                "display": "front",
            },
        ]

    def publish_to_redis(self, elements: list) -> None:
        """Publish display message to Redis."""

        message = {
            "app_id": self.app_id,
            "priority": self.priority,
            "duration_seconds": self.duration_seconds,
            "timestamp": time.time(),
            "elements": elements,
        }

        msg_json = json.dumps(message)

        self.redis_client.publish(
            self.redis_channel,
            msg_json,
        )

        logger.info(
            f"Published to {self.redis_channel} "
            f"(priority={self.priority}, "
            f"duration={self.duration_seconds}s)"
        )

    def run(self) -> None:
        """Main loop: fetch observation, render, publish."""

        logger.info(
            f"Starting weather app: {self.city_name} "
            f"(lat={self.latitude}, lon={self.longitude})"
        )

        logger.info(
            f"Update interval: {self.interval_seconds}s, "
            f"priority: {self.priority}"
        )

        # Test Redis connection.
        try:
            self.redis_client.ping()
            logger.info("Connected to Redis")
        except redis.ConnectionError as e:
            logger.error(
                f"Failed to connect to Redis: {e}"
            )
            return

        try:
            while not self.shutdown:
                try:
                    # Fetch actual current NWS observation.
                    temp, description, is_day, station_id = (
                        self.fetch_weather()
                    )

                    # Select one of the existing BusyBar icons.
                    icon_filename = self.get_icon_path(
                        description,
                        is_day,
                    )

                    # Upload icon using the existing shared uploader.
                    icon_path = self.icon_uploader.upload(
                        icon_filename,
                        fallback="sun.png",
                    )

                    # Render display.
                    elements = self.render_elements(
                        temp,
                        description,
                        icon_path,
                    )

                    # Publish to Redis.
                    self.publish_to_redis(elements)

                except Exception as e:
                    logger.error(
                        f"Error in publish loop: {e}"
                    )

                # Wait for next update while checking shutdown frequently.
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
        description=(
            "BusyBar current-weather sub-app "
            "(NWS observations -> Redis)"
        )
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
        help=(
            "How often to fetch current weather, "
            "in seconds (default: 300 = 5 min)"
        ),
    )

    parser.add_argument(
        "--stale_buffer",
        type=int,
        default=10,
        help=(
            "Grace period (seconds) added on top of --interval "
            "before the display is considered stale."
        ),
    )

    parser.add_argument(
        "--show_city",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Prefix display text with the city name "
            "(default: on). Use --no-show_city for temperature "
            "and condition only."
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
        help="City name to display (default: Reston, VA)",
    )

    parser.add_argument(
        "--lat",
        type=float,
        default=38.935094,
        help="Latitude (default: Reston, VA)",
    )

    parser.add_argument(
        "--lon",
        type=float,
        default=-77.366724,
        help="Longitude (default: Reston, VA)",
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
