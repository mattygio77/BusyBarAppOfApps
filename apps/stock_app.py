#!/usr/bin/env python3
"""Stocks sub-app for BusyBar controller.

Fetches the day's percent change for a configured list of stock tickers
and publishes a display card - one ticker at a time - to Redis, cycling
to the next ticker every --card_seconds, wrapping back to the first after
the last.

Each card is: [up/down arrow icon] [TICKER] / [+X.XX%] - the ticker name
in a larger font than the percent, and the percent (and arrow icon) colored
green for a gain or red for a loss.

Why one full card at a time rather than a continuously-panning tape (an
earlier version of this app tried that): busylib's ImageElement has no
scroll fields at all - only TextElement does - so an icon genuinely cannot
be scrolled by the device. And a TextElement's scroll_rate only reveals
more of THAT element's own text within its own fixed x/width box; it
doesn't move the box itself across the screen. There's no way to pan a
multi-element assembly (icon + two colored text pieces) across the display
using the device's native scroll - the only way to do that is to keep
republishing every element's x position from the app itself many times a
second, which is what caused this app to redraw far more often than it
should have.

So instead: publish one full card per ticker, once, and let it sit for the
whole --card_seconds (duration_seconds is set to cover exactly that, so
there's no need to republish until it's time to move to the next ticker -
this is what actually fixes the constant-redraw problem). scroll_rate is
still set on the ticker/percent text - correctly, for its real purpose -
so if a ticker or percent string is ever wider than its own text box, the
device scrolls *that piece* in place rather than clipping it, instead of
never scrolling anything the way the old design needed it to.

A note on sharing the display with weather (or anything else that's also
designed to be an "always on" default): the controller's priority model
only lets a STRICTLY higher priority message interrupt the current owner,
and an app that keeps renewing itself (like weather does) never naturally
lets go. So instead of trying to be a co-equal "default" like weather,
this app is a higher-priority *interrupter* that runs an active/rest duty
cycle: it actively holds the display, cycling through its tickers, for
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

# Icon filenames uploaded once at startup - there are only ever two
# possible arrows here.
UP_ICON = "stock_up.png"
DOWN_ICON = "stock_down.png"

# Front display is 72x16 (see busylib's displays guide / DisplayName.FRONT
# spec). Icon on the left, a two-row text column on the right - the exact
# same geometry weather_app.py already uses successfully.
ICON_SIZE = 16          # arrow icon is 16x16, drawn at x=0, y=0
TEXT_X_OFFSET = 18      # 2px gap between icon and the text column
TEXT_COL_WIDTH = 54     # 18 + 54 = 72, exactly fills the remaining width
FRONT_WIDTH = 72

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
    """Fetches daily stock movement and publishes one ticker card at a time."""

    def __init__(
        self,
        app_id: str,
        priority: int,
        tickers: list,
        interval_seconds: int = 60,
        card_seconds: float = 4.0,
        stale_buffer_seconds: int = 3,
        active_seconds: float = None,
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
                cycle note in the module docstring for why.
            tickers: List of ticker symbols to cycle through, e.g.
                ["AAPL", "MSFT", "GOOGL"]. Configuring which stocks show up
                is just this list - add or remove tickers here (or via
                --tickers on the command line).
            interval_seconds: How often to refetch prices from Yahoo
                Finance for every configured ticker
            card_seconds: How long each ticker's card stays on screen
                before advancing to the next (default: 4.0)
            stale_buffer_seconds: Grace period added on top of
                card_seconds for each published card's duration_seconds -
                same purpose as in weather_app.py: keeps a card as the
                display owner for its whole dwell time without needing to
                republish, and lets ownership lapse quickly (rather than
                freezing on screen) if this app stops updating.
            active_seconds: How long each active window lasts before this
                app deliberately goes quiet. If None (default), it's set
                to exactly one full lap through every configured ticker
                (card_seconds * number of tickers).
            rest_seconds: How long to stay quiet between active windows.
                During this time the app doesn't publish at all, so its
                ownership guarantee lapses and whatever's next-highest-
                priority - normally weather - gets the display back. After
                resting, the app starts a fresh active window from the
                first ticker.
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
        self.card_seconds = card_seconds
        # duration_seconds must be a positive int (see DisplayMessage), so
        # round the card length up before adding the stale-buffer on top.
        self.duration_seconds = math.ceil(card_seconds) + stale_buffer_seconds

        self.active_seconds = (
            active_seconds
            if active_seconds is not None
            else card_seconds * len(self.tickers)
        )
        self.rest_seconds = rest_seconds
        # Duty-cycle state: "active" (cycling through cards, holding the
        # display) or "resting" (deliberately silent, letting go of it).
        self.cycle_state = "active"
        self.cycle_started_at = 0.0

        # Which configured ticker (by index into the *available* list -
        # see _current_ticker()) is currently on screen, and when its card
        # was last (re)published.
        self.card_index = 0
        self.card_started_at = 0.0

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
        # of the rotation until it does; one that fails on a later refresh
        # keeps showing its last known value rather than dropping out.
        self.quotes = {}
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
        leaves it out of the rotation entirely if it has never succeeded).
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

    def _available_tickers(self) -> list:
        """Configured tickers that have at least one successful fetch."""
        return [t for t in self.tickers if t in self.quotes]

    def _card_elements(self, ticker: str) -> list:
        """Build the full-screen icon + ticker + percent card for one
        ticker.

        Row split (small font at y=0, normal font at y=6) matches
        weather_app.py's proven, non-clipping layout exactly - an earlier
        version of this app put the bigger ("normal") font in the top row
        with a vertically-centering "align", which clipped its top edge
        against the display's top boundary. "top_left" alignment plus
        reusing weather's exact y values avoids both problems: text is
        anchored at y and grows downward rather than being centered on it.
        """
        price, previous_close = self.quotes[ticker]
        pct_change = (price - previous_close) / previous_close * 100
        is_up = pct_change >= 0
        icon = UP_ICON if is_up else DOWN_ICON
        color = UP_COLOR if is_up else DOWN_COLOR

        return [
            {
                "id": "stock_icon",
                "type": "image",
                "path": icon,
                "x": 0,
                "y": 0,
                "display": "front",
            },
            {
                # Percent change: top row, smaller font, green/red.
                "id": "stock_pct",
                "type": "text",
                "text": f"{pct_change:+.2f}%",
                "x": TEXT_X_OFFSET,
                "y": 0,
                "font": "small",
                "color": color,
                "width": TEXT_COL_WIDTH,
                "align": "top_left",
                # Only matters if this string is ever wider than
                # TEXT_COL_WIDTH - the device scrolls it in place rather
                # than clipping it. Values match weather_app.py's city
                # text.
                "scroll_rate": 500,
                "scroll_start_delay": 1000,
                "scroll_repeat_delay": 1000,
                "display": "front",
            },
            {
                # Ticker name: bottom row, bigger font, neutral color -
                # only the percent above is green/red.
                "id": "stock_name",
                "type": "text",
                "text": ticker,
                "x": TEXT_X_OFFSET,
                "y": 6,
                "font": "normal",
                "color": TICKER_COLOR,
                "width": TEXT_COL_WIDTH,
                "align": "top_left",
                "scroll_rate": 500,
                "scroll_start_delay": 1000,
                "scroll_repeat_delay": 1000,
                "display": "front",
            },
        ]

    def _loading_message(self) -> list:
        """Fallback card shown before the first successful fetch."""
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
                "align": "top_left",
                "scroll_rate": 0,
                "display": "front",
            }
        ]

    def _current_card_elements(self) -> list:
        """Elements for whichever ticker card.card_index points at."""
        available = self._available_tickers()
        if not available:
            return self._loading_message()
        ticker = available[self.card_index % len(available)]
        return self._card_elements(ticker)

    def publish_current_card(self) -> None:
        """Publish (once) whichever ticker card is currently selected."""
        try:
            self.publish_to_redis(self._current_card_elements())
        except Exception as e:
            logger.error(f"Error publishing card: {e}")

    def advance_card(self) -> None:
        """Move to the next ticker, wrapping back to the first after the
        last, then publish it. A no-op (still publishes) if nothing has
        successfully fetched yet.
        """
        available = self._available_tickers()
        if available:
            self.card_index = (self.card_index + 1) % len(available)
        self.publish_current_card()

    def publish_to_redis(self, elements: list) -> None:
        """Publish a display message (one ticker card) to Redis."""
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
            f"Published card to {self.redis_channel} "
            f"({len(elements)} elements)"
        )

    def run(self) -> None:
        """Main loop: periodically refresh quotes, and advance/publish
        one ticker card at a time on a much slower cadence than before -
        once per card_seconds, not every animation tick.
        """
        logger.info(
            f"Starting stocks app: {', '.join(self.tickers)} "
            f"(refresh every {self.interval_seconds}s, "
            f"card shown for {self.card_seconds}s)"
        )

        # Test Redis connection
        try:
            self.redis_client.ping()
            logger.info("Connected to Redis")
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            return

        self.icon_uploader.upload_all([UP_ICON, DOWN_ICON])

        # Fetch once synchronously before entering the loop so the first
        # card has real data instead of starting on the loading fallback
        # every time the app restarts.
        self.refresh_quotes()
        self.last_fetch = time.time()
        self.cycle_started_at = time.time()
        self.card_started_at = time.time()
        self.publish_current_card()

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
                    elif (now - self.card_started_at) >= self.card_seconds:
                        self.advance_card()
                        self.card_started_at = now

                else:  # resting - stay silent, let ownership lapse
                    if cycle_elapsed >= self.rest_seconds:
                        self.cycle_state = "active"
                        self.cycle_started_at = now
                        self.card_index = 0
                        self.card_started_at = now
                        logger.debug("Rest complete, starting active window")
                        self.publish_current_card()

                # Check shutdown frequently (every 100ms) while waiting
                # for the next second-ish of wall-clock time to pass.
                for _ in range(10):
                    if self.shutdown:
                        break
                    time.sleep(0.1)
        finally:
            self.icon_uploader.close()

        logger.info("Stocks app stopped")


def main():
    """Entry point for stocks app."""
    parser = argparse.ArgumentParser(
        description="BusyBar stocks sub-app (publishes to Redis)"
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
        default="AAPL,MSFT,GOOGL,AMZN",
        help=(
            "Comma-separated ticker symbols to cycle through "
            "(default: AAPL,MSFT,GOOGL,AMZN)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="How often to refetch prices, in seconds (default: 60)",
    )
    parser.add_argument(
        "--card_seconds",
        type=float,
        default=4.0,
        help=(
            "How long each ticker's card stays on screen before advancing "
            "to the next (default: 4.0)"
        ),
    )
    parser.add_argument(
        "--stale_buffer",
        type=int,
        default=3,
        help=(
            "Grace period (seconds) added on top of card_seconds before a "
            "published card is considered stale (default: 3)."
        ),
    )
    parser.add_argument(
        "--active_seconds",
        type=float,
        default=None,
        help=(
            "How long each active window lasts before this app "
            "deliberately goes quiet and hands the display back. Defaults "
            "to one full lap through every configured ticker "
            "(card_seconds * number of tickers)."
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
        card_seconds=args.card_seconds,
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