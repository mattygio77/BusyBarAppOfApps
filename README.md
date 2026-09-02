# BusyBar App of Apps

A multi-app display controller for a [BusyBar](https://github.com/busy-app) LED
device. Independent "sub-apps" (weather, stock tickers, etc.) each decide what
*they'd like* to show and publish it to a shared message bus; a central
controller decides who actually gets the screen and draws it, based on
priority and how long each app has already had it.

Think of it like a tiny window manager, but for a 72x16 LED matrix instead of
a monitor.

## How it works

```
apps/weather_app.py  ─┐
apps/stock_app.py    ─┼─► Redis pub/sub ─► controller/controller.py ─► BusyBar device
apps/<your_app>.py   ─┘      (per-app          (picks a winner,        (72x16 RGB front,
                              channel)           draws it)               160x80 grayscale back)
```

- **Sub-apps** are independent, long-running Python processes. Each one
  decides on some interval "here's what I'd like to show," and publishes a
  small JSON message to its own Redis channel (`busybar:app:<id>`). A
  sub-app never talks to the BusyBar device directly for drawing - only the
  controller does that.
- **The controller** (`controller/controller.py`) subscribes to every
  enabled app's channel, keeps a priority queue of their latest messages,
  and roughly twice a second asks "who owns the display right now?" and
  draws that. If nobody's actively claiming it, it falls back to rotating
  idle content from `apps.json`.
- **Ownership** is priority-based with a guarantee: once an app becomes the
  display owner, it keeps the screen for its message's `duration_seconds`
  *unless* a strictly higher-priority message shows up. Equal or lower
  priority messages just wait their turn. An app that keeps republishing
  fresh content before its duration runs out effectively holds the screen
  indefinitely - that's how weather works as the "default" screen.
  See `AGENTS.md` for the full mechanics and the gotcha this creates when
  two apps both want to be an "always-on default" at once.

## Repo layout

```
main.py                      Entry point - starts the controller (does NOT
                              start Redis or any sub-apps; see "Running it")
apps.json                    App registry + device/Redis settings + idle content
state.json                   Runtime state written by the controller (who owns
                              the display, when, and what's queued)
AGENTS.md                    Context and ground rules for an AI agent (or human)
                              making changes to this repo - read this first if
                              you're about to add or modify anything

controller/
  controller.py               Main loop: Redis listener, ownership resolution,
                               drawing, idle rotation
  display_queue.py            Priority queue + ownership-guarantee logic
  message.py                  DisplayMessage schema + validation
  config.py                   Loads/saves apps.json and state.json

apps/
  weather_app.py               Publishes current weather, day/night-aware icon
  weather/icons/                sun/moon/partly/cloud/fog/rain/snow PNGs (16x16)
  stock_app.py                 Publishes a scrolling stock ticker tape
  stocks/icons/                 green/red trend-arrow PNGs (16x16)
  icon_uploader.py              Shared helper: uploads local icon PNGs to the
                                 device, used by both apps above

test_controller.py            Basic DisplayMessage + Redis pub/sub smoke test
requirements.txt              Python dependencies
```

## Requirements

- Python 3.9+
- [Redis](https://redis.io/) (`redis-server` on your `PATH` - the controller
  will try to launch it automatically if it isn't already running on the
  configured port)
- A BusyBar device reachable on your network (default `10.0.4.20`)
- Python packages in `requirements.txt`: `redis`, `requests`, `Pillow`,
  `busylib`

## Setup

```bash
pip install -r requirements.txt
```

Check `apps.json` and update `device_ip` if your BusyBar isn't at
`10.0.4.20`, or `redis_host`/`redis_port` if Redis lives somewhere other
than local default.

## Running it

There's no single command that starts everything yet (see "Known
limitations" below) - the controller and each sub-app are run as separate,
manually-started processes, each in its own terminal:

```bash
# Terminal 1: the controller (starts/connects to Redis, owns the device)
python3 main.py

# Terminal 2: weather (publishes to Redis; controller decides if/when it's shown)
python3 apps/weather_app.py

# Terminal 3: stocks
python3 apps/stock_app.py --tickers AAPL,MSFT,GOOGL,TSLA
```

Each sub-app also runs fine on its own without the controller running (it'll
just be publishing to a Redis channel nobody's listening to yet), which is
useful for testing a new app's output in isolation.

### Weather app

Fetches current conditions from [Open-Meteo](https://open-meteo.com/) (no
API key needed) and shows an icon + city + temperature, refreshing on an
interval. Icon swaps to a moon for clear/partly-clear conditions at night
(Open-Meteo's own sunrise/sunset-based `is_day` flag, not a fixed clock
cutoff).

```bash
python3 apps/weather_app.py \
  --city "Reston, VA" --lat 38.935094 --lon -77.366724 \
  --interval 300 --priority 50
```

Run `python3 apps/weather_app.py --help` for the full flag list (units,
`--no-show_city`, Redis/device overrides, etc.).

### Stocks app

Fetches the day's percent change for a configurable list of tickers from
Yahoo Finance's keyless chart endpoint and renders them as a continuously
scrolling ticker tape: arrow icon, ticker name, and green/red percent
change, looping seamlessly.

```bash
python3 apps/stock_app.py \
  --tickers AAPL,MSFT,GOOGL,TSLA \
  --interval 60 --scroll_speed 14 --priority 60
```

Because it's meant to *interrupt* rather than sit alongside weather as a
co-equal default, it runs an active/rest duty cycle (`--active_seconds` /
`--rest_seconds`, 25s each by default): it holds the display for a while,
then deliberately goes quiet so weather (or whatever else) gets a turn.
See `AGENTS.md` for why this is necessary rather than just picking a
priority number.

Run `python3 apps/stock_app.py --help` for the full flag list.

## Adding your own app

The short version: publish a JSON message shaped like `DisplayMessage`
(see `controller/message.py`) to `busybar:app:<your_id>` on an interval,
add an entry to `apps.json`, and think carefully about priority relative
to whatever else might be running. `AGENTS.md` has the full checklist,
the display-element schema, and the gotchas that have already bitten this
codebase once (so hopefully not twice).

## Known limitations / open issues

- `main.py` doesn't spawn sub-app processes - the controller and every app
  are started manually, in separate terminals/processes.
- Idle content rotation (`idle_rotation` in `apps.json`) only actually
  redraws when the display first becomes idle, not on each rotation
  through multiple idle items - low priority since only one idle item is
  currently configured.
- Text elements have been observed disappearing after ~10s while an
  accompanying icon stays on screen, on some display messages - under
  investigation, suspected `timeout` field interaction between text and
  image elements.

## Icon sourcing

Icons are pulled from [Noto Emoji](https://github.com/googlefonts/noto-emoji)
rather than generated in code (see `AGENTS.md`), resized to 16x16 to match
the front display's icon size, and in some cases recolored (e.g. the stock
arrows) by extracting the glyph's shape and re-tinting it - the shape still
comes from a real sourced asset, just re-colored to fit the app's semantics.
