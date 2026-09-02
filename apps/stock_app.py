#!/usr/bin/env python3
"""Stocks ticker-tape sub-app for BusyBar controller.

Fetches the day's percent change for a configured list of stock tickers
and publishes a continuously scrolling ticker-tape display to Redis.

Each ticker renders as: [up/down arrow icon] [TICKER] / [+X.XX%] - the
ticker name in a larger font than the percent, and the percent (and arrow)
colored green for a gain or red for a loss. The whole tape scrolls
right-to-left across the front display and loops seamlessly once the last
configured ticker has scrolled past, wrapping back to the first.

A note on sharing the display with weather (or anything else that's also
designed to be an "always on" default): the controller's priority model
only lets a STRICTLY higher priority message interrupt the current owner,
and an app that keeps renewing itself (like weather does) never naturally
lets go. Two apps at equal or ascending-then-flat priority would mean
whichever grabs ownership first keeps it forever and the other never
shows up at all. So instead of trying to be a co-equal "default" like
weather, this app is a higher-priority *interrupter* that runs an active/
rest duty cycle: it actively holds the display (renewing every tick) for
--active_seconds, then deliberately stops publishing for --rest_seconds,
letting its own ownership guarantee lapse and handing control back to
whatever's next (weather, most likely) - then repeats. That's why its
default priority (60) is set above weather's (50).

Usage:
    python3 apps/stock_app.py --tickers AAPL,MSFT,GOOGL,TSLA --priority 60
"""

import argparse
import json
import logging
import math
import signal
import sys
import time
from pathlib import Path

import redis
import requests

from icon_uploader import IconUploader

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] stock_app: %(message)s",
)
logger = logging.getLogger(__name__)

# Icon filenames uploaded once at startup - unlike weather's per-condition
# icons, there are only ever two possible arrows here.
UP_ICON = "stock_up.png"
DOWN_ICON = "stock_down.png"

# Front display is 72x16 (see busylib's displays guide). Per-ticker
# "segment" layout in the scrolling tape, all in pixels. Segments are laid
# out left to right and the whole assembly is panned via each element's x
# coordinate - the device just doesn't render whatever falls outside 0-72,
# which is what lets a segment glide smoothly on and off screen.
ICON_SIZE = 16          # arrow icon is 16x16, drawn at the segment's x + 0
TEXT_X_OFFSET = 18      # 2px gap between icon and the text column
TEXT_COL_WIDTH = 50     # width of the ticker/percent text column
SEGMENT_GAP = 14        # blank space between one segment and the next
SEGMENT_WIDTH = TEXT_X_OFFSET + TEXT_COL_WIDTH + SEGMENT_GAP  # 82px/ticker

FRONT_WIDTH = 72        # from busylib's DisplayName.FRONT spec

TICKER_COLOR = "#FFFFFFFF"   # neutral white for the ticker name
UP_COLOR = "#00FF00FF"       # green percent text + arrow, for gains
DOWN_COLOR = "#FF0000FF"     # red percent text + arrow, for losses

# Yahoo Finance's chart endpoint has no official/documented status and no
# API key, but it's the same endpoint libraries like yfinance sit on top
# of, and (unlike the older v7 quote endpoint) doesn't require a
# crumb/cookie auth handshake. It does reject requests with default
# library User-Agents, so this pretends to be a browser.
YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


class StockApp:
    """Fetches daily stock movement and publishes a scrolling ticker tape."""

    def __init__(
        self,
        app_id: str,
        priority: int,
        tickers: list,
        interval_seconds: int = 60,
        scroll_speed: float = 14.0,
        tick_seconds: float = 0.4,
        stale_buffer_seconds: int = 3,
        active_seconds: float = 25.0,
        rest_seconds: float = 25.0,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        device_ip: str = "10.0.4.20",
    ):
        """Initialize the stocks app.

        Args:
            app_id: Unique identifier for this app
            priority: Display priority 0-100. Should be set STRICTLY
                higher than any other "always on" default app (weather
                defaults to 50, so this defaults to 60) - see the duty
                cycle note below and in the module docstring for why.
            tickers: List of ticker symbols to cycle through, e.g.
                ["AAPL", "MSFT", "GOOGL"]. Configuring which stocks show up
                is just this list - add or remove tickers here (or via
                --tickers on the command line).
            interval_seconds: How often to refetch prices from Yahoo
                Finance for every configured ticker
            scroll_speed: Ticker-tape scroll speed, in pixels/second
            tick_seconds: How often to advance the scroll animation and
                republish to Redis. The controller's main loop only checks
                for new content twice a second (see controller.py's
                time.sleep(0.5)), so pushing much faster than that just
                means intermediate frames get skipped rather than drawn -
                0.4s keeps this comfortably ahead of that without wasting
                Redis traffic on frames that would never make it to the
                device.
            stale_buffer_seconds: Grace period added on top of
                tick_seconds for each published message's duration_seconds -
                same purpose as in weather_app.py: keeps the tape as the
                display owner for as long as this app is actively
                publishing, and lets its ownership lapse quickly (rather
                than freezing on screen) once it stops.
            active_seconds: How long each active window lasts - the app
                renews itself every tick for this long, holding the
                display, before deliberately going quiet.
            rest_seconds: How long to stay quiet between active windows.
                During this time the app doesn't publish at all, so its
                ownership guarantee lapses (after stale_buffer_seconds or
                so) and whatever's next-highest-priority - normally
                weather - gets the display back. After resting, the app
                starts a fresh active window from the first ticker.
            redis_host: Redis server host
            redis_port: Redis server port
            device_ip: BusyBar device IP (for uploading the arrow icons)
        """
        self.app_id = app_id
        self.priority = priority
        self.tickers = [t.strip().upper() for t in tickers if t.strip()]
        if not self.tickers:
            raise ValueError("At least one ticker must be configured")

        self.interval_seconds = interval_seconds
        self.scroll_speed = scroll_speed
        self.tick_seconds = tick_seconds
        # duration_seconds must be a positive int (see DisplayMessage), so
        # round the tick up before adding the stale-buffer on top.
        self.duration_seconds = math.ceil(tick_seconds) + stale_buffer_seconds

        self.active_seconds = active_seconds
        self.rest_seconds = rest_seconds
        # Duty-cycle state: "active" (renewing/publishing, holding the
        # display) or "resting" (deliberately silent, letting go of it).
        self.cycle_state = "active"
        self.cycle_started_at = 0.0

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
            icon_folder=Path(__file__).parent / "stocks" / "icons",
        )

        # ticker -> (price, previous_close) from the most recent successful
        # fetch. A ticker that has never successfully fetched is left out
        # of the tape until it does; one that fails on a later refresh
        # keeps showing its last known value rather than dropping out.
        self.quotes = {}

        # Running scroll offset, in pixels. Advanced every tick and wrapped
        # modulo the current tape width (see run() and build_frame()).
        self.scroll_position = 0.0
        self.last_fetch = 0.0

        self.shutdown = False
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, sig, frame):
        """Handle shutdown signal."""
        logger.info("Shutdown signal received")
        self.shutdown = True

    def fetch_quote(self, ticker: str) -> tuple:
        """Fetch current price and previous close for one ticker.

        Uses Yahoo Finance's public, keyless chart endpoint (the v8
        /finance/chart/{ticker} endpoint) - the same one libraries like
        yfinance sit on top of. It's not an official, supported API, so
        treat failures here as routine rather than exceptional (see
        refresh_quotes()).

        Returns:
            (price, previous_close) tuple

        Raises:
            Exception on a request error, an unexpected response shape,
            or a missing price/previous-close field.
        """
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1d", "range": "1d"}

        resp = requests.get(
            url, params=params, headers=YAHOO_HEADERS, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("chart", {}).get("result")
        if not results:
            raise ValueError(f"No chart data returned for {ticker}")

        meta = results[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        previous_close = meta.get("previousClose", meta.get("chartPreviousClose"))

        if price is None or previous_close is None:
            raise ValueError(f"Missing price/previousClose for {ticker}")

        return price, previous_close

    def refresh_quotes(self) -> None:
        """Refetch every configured ticker, updating self.quotes.

        A single ticker failing doesn't take down the rest - it logs a
        warning and keeps showing that ticker's last known value (or
        leaves it out of the tape entirely if it has never succeeded).
        """
        for ticker in self.tickers:
            try:
                price, previous_close = self.fetch_quote(ticker)
                self.quotes[ticker] = (price, previous_close)
                logger.debug(
                    f"{ticker}: {price} (prev close {previous_close})"
                )
            except Exception as e:
                logger.warning(f"Failed to fetch quote for {ticker}: {e}")
            # Be polite to Yahoo's unofficial endpoint between requests.
            time.sleep(0.3)

    def _segment_elements(self, ticker: str, seg_x: int) -> list:
        """Build the icon + ticker + percent elements for one segment.

        seg_x is this segment's current on-screen x position and may be
        negative or beyond FRONT_WIDTH - the device simply won't render
        whatever falls outside the display, which is what lets a segment
        scroll smoothly on and off screen.
        """
        price, previous_close = self.quotes[ticker]
        pct_change = (price - previous_close) / previous_close * 100
        is_up = pct_change >= 0
        icon = UP_ICON if is_up else DOWN_ICON
        color = UP_COLOR if is_up else DOWN_COLOR

        return [
            {
                "id": f"{ticker}_icon",
                "type": "image",
                "path": icon,
                "x": seg_x,
                "y": 0,
                "display": "front",
            },
            {
                # Ticker name: bigger font ("normal"), neutral color -
                # only the percent below is green/red.
                "id": f"{ticker}_name",
                "type": "text",
                "text": ticker,
                "x": seg_x + TEXT_X_OFFSET,
                "y": 0,
                "font": "normal",
                "color": TICKER_COLOR,
                "width": TEXT_COL_WIDTH,
                "align": "mid_left",
                # We pan the whole segment ourselves via x each tick, so
                # the device's own per-element scroll must stay off -
                # otherwise the text would drift independently of the
                # icon it's supposed to stay lined up with.
                "scroll_rate": 0,
                "scroll_start_delay": 0,
                "scroll_repeat_delay": 0,
                "display": "front",
            },
            {
                # Percent change: smaller font ("small"), green/red.
                "id": f"{ticker}_pct",
                "type": "text",
                "text": f"{pct_change:+.2f}%",
                "x": seg_x + TEXT_X_OFFSET,
                "y": 6,
                "font": "small",
                "color": color,
                "width": TEXT_COL_WIDTH,
                "align": "mid_left",
                "scroll_rate": 0,
                "scroll_start_delay": 0,
                "scroll_repeat_delay": 0,
                "display": "front",
            },
        ]

    def _loading_message(self) -> list:
        """Fallback frame shown before the first successful fetch."""
        return [
            {
                "id": "stocks_loading",
                "type": "text",
                "text": "Loading stocks...",
                "x": 0,
                "y": 4,
                "font": "small",
                "color": "#FFFFFFFF",
                "width": FRONT_WIDTH,
                "scroll_rate": 0,
                "display": "front",
            }
        ]

    def build_frame(self) -> list:
        """Build the list of display elements visible at the current
        scroll position, wrapping seamlessly once the last ticker's
        segment has scrolled past.
        """
        available = [t for t in self.tickers if t in self.quotes]
        if not available:
            return self._loading_message()

        total_width = SEGMENT_WIDTH * len(available)

        elements = []
        for i, ticker in enumerate(available):
            base_x = i * SEGMENT_WIDTH
            seg_x = base_x - self.scroll_position
            # A segment that's scrolled past the left edge wraps back
            # around to the right, so the tape loops with no visible seam.
            if seg_x < -SEGMENT_WIDTH:
                seg_x += total_width

            if -SEGMENT_WIDTH <= seg_x <= FRONT_WIDTH:
                elements.extend(
                    self._segment_elements(ticker, int(round(seg_x)))
                )

        return elements or self._loading_message()

    def publish_to_redis(self, elements: list) -> None:
        """Publish a display message (one animation frame) to Redis."""
        message = {
            "app_id": self.app_id,
            "priority": self.priority,
            "duration_seconds": self.duration_seconds,
            "timestamp": time.time(),
            "elements": elements,
        }

        msg_json = json.dumps(message)
        self.redis_client.publish(self.redis_channel, msg_json)

        logger.debug(
            f"Published frame to {self.redis_channel} "
            f"({len(elements)} elements)"
        )

    def run(self) -> None:
        """Main loop: periodically refresh quotes, continuously animate
        and publish the scrolling ticker tape.
        """
        logger.info(
            f"Starting stocks app: {', '.join(self.tickers)} "
            f"(refresh every {self.interval_seconds}s, "
            f"scroll {self.scroll_speed}px/s)"
        )

        # Test Redis connection
        try:
            self.redis_client.ping()
            logger.info("Connected to Redis")
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            return

        self.icon_uploader.upload_all([UP_ICON, DOWN_ICON])

        # Fetch once synchronously before entering the loop so the tape
        # has real data on its very first frame instead of starting on
        # the loading fallback every time the app restarts.
        self.refresh_quotes()
        self.last_fetch = time.time()
        self.cycle_started_at = time.time()

        try:
            while not self.shutdown:
                now = time.time()

                if (now - self.last_fetch) >= self.interval_seconds:
                    self.refresh_quotes()
                    self.last_fetch = now

                cycle_elapsed = now - self.cycle_started_at

                if self.cycle_state == "active":
                    if cycle_elapsed >= self.active_seconds:
                        # Deliberately go quiet: stop publishing so this
                        # app's ownership guarantee lapses and the next-
                        # best candidate (weather, normally) takes over.
                        self.cycle_state = "resting"
                        self.cycle_started_at = now
                        logger.debug(
                            f"Active window done, resting for "
                            f"{self.rest_seconds}s"
                        )
                    else:
                        available = [
                            t for t in self.tickers if t in self.quotes
                        ]
                        if available:
                            total_width = SEGMENT_WIDTH * len(available)
                            self.scroll_position = (
                                self.scroll_position
                                + self.scroll_speed * self.tick_seconds
                            ) % total_width

                        elements = self.build_frame()
                        try:
                            self.publish_to_redis(elements)
                        except Exception as e:
                            logger.error(f"Error publishing frame: {e}")

                else:  # resting - stay silent, let ownership lapse
                    if cycle_elapsed >= self.rest_seconds:
                        self.cycle_state = "active"
                        self.cycle_started_at = now
                        self.scroll_position = 0.0
                        logger.debug("Rest complete, starting active window")

                # Wait for next tick (check shutdown frequently, every 100ms)
                for _ in range(max(1, int(self.tick_seconds * 10))):
                    if self.shutdown:
                        break
                    time.sleep(0.1)
        finally:
            self.icon_uploader.close()

        logger.info("Stocks app stopped")


def main():
    """Entry point for stocks app."""
    parser = argparse.ArgumentParser(
        description="BusyBar stocks ticker-tape sub-app (publishes to Redis)"
    )
    parser.add_argument(
        "--app_id",
        default="stocks",
        help="Unique app identifier (default: stocks)",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=60,
        help=(
            "Display priority 0-100 (default: 60). Keep this strictly "
            "above any other continuously-renewing 'default' app, e.g. "
            "weather (50) - see the module docstring for why."
        ),
    )
    parser.add_argument(
        "--tickers",
        default="VGT,AAPL,MSFT,GOOGL,AMZN,VOO",
        help=(
            "Comma-separated ticker symbols to cycle through "
            "(default: VGT,AAPL,MSFT,GOOGL,AMZN,VOO)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="How often to refetch prices, in seconds (default: 60)",
    )
    parser.add_argument(
        "--scroll_speed",
        type=float,
        default=14.0,
        help="Ticker-tape scroll speed in pixels/second (default: 14.0)",
    )
    parser.add_argument(
        "--tick_seconds",
        type=float,
        default=0.4,
        help=(
            "Animation/publish tick interval in seconds (default: 0.4). "
            "The controller only redraws about twice a second, so there's "
            "little benefit setting this much lower."
        ),
    )
    parser.add_argument(
        "--stale_buffer",
        type=int,
        default=3,
        help=(
            "Grace period (seconds) added on top of the tick interval "
            "before a published frame is considered stale (default: 3)."
        ),
    )
    parser.add_argument(
        "--active_seconds",
        type=float,
        default=25.0,
        help=(
            "How long each active window lasts before this app "
            "deliberately goes quiet and hands the display back "
            "(default: 25.0)"
        ),
    )
    parser.add_argument(
        "--rest_seconds",
        type=float,
        default=25.0,
        help=(
            "How long to stay quiet between active windows, giving other "
            "apps (e.g. weather) the display (default: 25.0)"
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

    args = parser.parse_args()

    app = StockApp(
        app_id=args.app_id,
        priority=args.priority,
        tickers=args.tickers.split(","),
        interval_seconds=args.interval,
        scroll_speed=args.scroll_speed,
        tick_seconds=args.tick_seconds,
        stale_buffer_seconds=args.stale_buffer,
        active_seconds=args.active_seconds,
        rest_seconds=args.rest_seconds,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        device_ip=args.device_ip,
    )

    app.run()


if __name__ == "__main__":
    main()
