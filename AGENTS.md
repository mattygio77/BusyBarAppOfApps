# Busy App AGENTS.md

Context for an AI agent (or human) making changes or additions to this
repo. Read this before editing anything - several of the rules below exist
because an earlier version of this code got them wrong, and re-introducing
the same bug is easy to do by accident.

## What this repo is

A multi-app controller for a BusyBar LED device (72x16 RGB front display,
160x80 grayscale back display). Independent sub-app processes
(`apps/*_app.py`) publish "here's what I'd like to show" messages to Redis;
`controller/controller.py` is the only thing that actually talks to the
device, and decides whose message wins based on priority + a display-time
guarantee. Full walkthrough and how to run it: `README.md`. This file is
narrower - conventions, invariants, and gotchas for changing the code.

## Hard rules

1. **Don't generate icons in code.** Pull them from the internet - use
   [googlefonts/noto-emoji](https://github.com/googlefonts/noto-emoji) or
   official team logo CDNs (like ESPN's NHL CDN in `nhl_app.py`) as a
   resource. It's fine to post-process a sourced image (resize to 16x16,
   crop, or recolor by masking/re-tinting an extracted shape) - the
   requirement is that the underlying shape comes from a real downloaded
   asset, not hand-drawn polygons/lines/letters in PIL or similar.
2. **Upload icons through `apps/icon_uploader.py`'s `IconUploader`, not ad
   hoc `busylib` calls.** Existing apps (`weather_app.py`, `stock_app.py`,
   `nhl_app.py`) all use it; it owns the `BusyBar` client and caches what's
   already been uploaded this run. A new app needing icons should do the
   same rather than re-implementing upload logic. Note `apps/` has no
   `__init__.py` - it's not a package, and every app is invoked directly
   (`python3 apps/foo_app.py`), which puts `apps/` on `sys.path[0]`. That's
   what makes `from icon_uploader import IconUploader` resolve as a plain
   sibling import. Don't switch to a relative/package-style import unless
   the invocation style changes too.
3. **`display_draw` is additive (upsert-by-id), not a full-screen
   replace.** An element from a previous draw that isn't redeclared will
   keep showing unless cleared. `controller.py` handles this automatically
   by passing `clear_before_draw=True` whenever:
   - the active `(app_id, card_id)` slot changes, **or**
   - the active slot drops any previously drawn element `id`
     (`bool(current_element_ids - new_element_ids)`).
   Because the controller detects removed element IDs automatically, a
   sub-app switching layouts (e.g. `nhl_app.py` switching from its
   4-element scoreboard to its 1-element goal animation and back) should
   keep its normal `app_id` rather than inventing a fake `app_id` (like
   `nhl_anim`) just to force a screen clear.
4. **Every `busylib` field must be the correct native Python type.**
   `scroll_rate`, `priority`, `duration_seconds`, etc. must be `int`, not
   `str` - a wrong type has silently been ignored by the API in the past
   rather than erroring, which made it hard to notice.
5. **`duration_seconds` (and optional `ttl_seconds`) must be a positive
   `int`** (see `controller/message.py`'s `DisplayMessage.__post_init__`).
   If you're computing it from a sub-second or float interval, round up
   (`math.ceil(...)`) before adding any buffer - see `stock_app.py` and
   `nhl_app.py`.

## Display-ownership & multi-card queue mechanics (read before adding a new app)

`controller/display_queue.py` and `controller/message.py` implement this;
the summary:

- **Composite slot keys (`slot_key = (app_id, card_id)`):**
  - `DisplayMessage` accepts an optional `card_id` (defaulting to
    `"default"` if omitted) and an optional `ttl_seconds` (queue expiration
    window independent of on-screen dwell `duration_seconds`).
  - Queue slots are keyed by `(app_id, card_id)` (capped at
    `max_cards_per_app=10` per app). This allows a single app to queue
    multiple distinct cards (e.g. `("stocks", "VOO")`, `("stocks", "AAPL")`)
    alongside cards from separate apps (`("nhl", "WSH")`,
    `("weather", "default")`) without overwriting its other pending cards.
  - Publishing a message with an existing `(app_id, card_id)` updates that
    specific slot in-place.
- **Priority & guaranteed dwell time:**
  - The highest-priority, non-expired slot owns the display for its full
    `duration_seconds` (counted from when it *became* the owner, not when
    it was published).
  - **Only a strictly higher priority message can preempt the current owner
    early.** If the preempting message belongs to the *same* `app_id` (such
    as a priority-100 goal animation preempting that app's normal
    scoreboard), `DisplayQueue` retires the old slot from that app so it
    doesn't linger behind.
  - Among equal-priority candidates waiting in the queue, `_best_candidate()`
    interleaves fairly across apps (preferring the app that least recently
    owned the screen via `_app_last_turn_seq`) and breaks ties within the
    same app by earliest publish `timestamp` (FIFO).
- **Queue expiration (`ttl_seconds` vs. `duration_seconds`):**
  - Waiting messages that haven't yet become the display owner expire after
    `ttl_seconds` if provided, or `duration_seconds` otherwise. If an app
    publishes cards with a short on-screen `duration_seconds` (e.g. 4s)
    that may need to wait behind a 15s-30s owner, set `ttl_seconds` (e.g.
    `60`) so the card isn't dropped as stale before its turn arrives.
- **Coexisting with an always-on default app (`weather`):**
  - An app that republishes fresh content on the same `(app_id, card_id)`
    before its `duration_seconds` elapses renews its ownership and never
    naturally lets go. Weather does this deliberately (`duration_seconds =
    interval + stale_buffer`) at priority `50`.
  - **Gotcha:** Higher-priority apps (`stocks` at `60`, `nhl` at `70` when
    live) must use an **active/rest duty cycle**: publish during the
    `active_seconds` window (sizing `duration_seconds` to the remaining
    active window + buffer, not a fixed `active_seconds + 10` on every
    tick!), and **stop publishing completely during the `rest_seconds`
    window** so ownership expires on time and hands the screen back to
    lower-priority apps.
- **Content-aware redraw deduplication (preventing marquee scroll resets):**
  - Every `display_draw` call to the BusyBar resets hardware text scrolling
    back to pixel 0 (restarting `scroll_start_delay`).
  - To prevent scrolling text from stuttering or freezing, `controller.py`
    tracks `(slot_key, message.timestamp)` to update lease/queue state in
    `state.json`, **but only calls `_draw_to_device()` if `slot_changed` or
    `current_msg.elements != current_display_elements`**.
  - Sub-apps should also avoid republishing unchanged static/scrolling
    frames at 1 Hz (see `nhl_app.py`'s `_publish_if_needed()`, which only
    publishes when elements/priority change or when the active lease is
    within `stale_buffer_seconds` of expiring).
- **Idle content (`apps.json`'s `idle_rotation`):**
  - Synthesized by the controller at priority `0` when the queue is empty.
  - Never include a `"timeout"` field on idle elements (or persistent app
    elements), as firmware element timeouts erase the element on-device
    while the controller still considers the idle message active.

## Display element schema

Elements are plain dicts. Every element needs `id` and `type` (`"text"` or
`"image"`). For the full field reference (fonts: `tiny`/`small`/`normal`/
`condensed`/`bold`/`large`/`extra_large`/`global`; alignment options;
scroll fields), see `busylib`'s own
[`docs/guides/displays.md`](https://github.com/busy-app/busylib-py/blob/main/docs/guides/displays.md) -
don't guess at field names or valid values, check that doc.

Things that have caused real bugs here before:
- Omitting `align`, `scroll_start_delay`, or `scroll_repeat_delay` on text
  elements has caused clipping - set them explicitly rather than relying
  on defaults.
- `scroll_rate` is millisecond-scale (~400-1000 = normal reading speed); a
  small integer barely scrolls.
- Do not set `"timeout"` on text elements unless you specifically want the
  BusyBar firmware to blank that element before the message's
  `duration_seconds` finishes (this previously caused default idle text to
  disappear after 6 seconds while the screen stayed in idle mode).
- Front display is 72px wide x 16px tall:
  - Single left icon (`x=0..15`, 16x16) + right text column: place text at
    `x=18`, `width=54` (`18 + 54 = 72`).
  - Dual edge icons (`my_logo` at `x=0..15`, `opp_logo` at `x=56..71`, as
    in `nhl_app.py`): center text at `x=36` with `align="top_mid"` and
    `width=38` so the text box stays strictly inside the `17..55` gap
    without overlapping the right-hand icon.

## Known quirks worth knowing before you touch things

- `controller.py` defines `_clear_device()` as a standalone helper, while
  normal frame transitions clear via `clear_before_draw=clear_first` passed
  directly to `busylib`'s `display_draw()`.
- `main.py` does not spawn `apps/*_app.py` as subprocesses. Everything is
  started manually in separate terminals. If you're asked to fix this,
  it's a real gap, not an oversight to work around quietly - flag it.

## External APIs sub-apps depend on

- **Open-Meteo** (`weather_app.py`): free, no key, `current_weather`
  includes `is_day` (astronomical, per-location) - this is what drives
  the day/night icon swap. Reasonably stable/documented.
- **Yahoo Finance chart endpoint** (`stock_app.py`):
  `query1.finance.yahoo.com/v8/finance/chart/{ticker}`. Unofficial and
  reverse-engineered (same one `yfinance` sits on top of), no API key,
  requires a browser-like `User-Agent` header or requests get rejected.
  Publishes each ticker card with `"card_id": ticker` during market hours.
- **NHL Web API & ESPN Team Logos CDN** (`nhl_app.py`):
  - Scoreboard/clock data: `https://api-web.nhle.com/v1/score/now`.
  - Team logos: downloaded on demand as stock 500x500 PNGs from
    `https://a.espncdn.com/i/teamlogos/nhl/500/{slug}.png`, resized to
    16x16 RGBA (`Image.Resampling.LANCZOS`), and cached in
    `apps/nhl/icons/<abbrev>.png` (e.g. `wsh.png` for the Washington
    Capitals; `--my_logo` defaults to `None` so the stock logo is used
    unless explicitly overridden).
  - `ESPN_SLUG_MAP` translates the few NHL API 3-letter codes that differ
    on ESPN's CDN (`LAK -> la`, `NJD -> nj`, `SJS -> sj`, `TBL -> tb`,
    `UTA -> utah`).

## Adding a new sub-app - checklist

1. Create `apps/<name>_app.py` following the existing pattern: argparse
   CLI, a class with a `run()` loop, publish `DisplayMessage`-shaped JSON
   (including `card_id` if the app publishes multiple distinct cards, and
   `ttl_seconds` if cards have short dwell times) to Redis channel
   `busybar:app:<id>`.
2. If it needs icons, use `apps/icon_uploader.py`'s `IconUploader` (see
   Hard rule #2) and source assets per Hard rule #1.
3. Decide priority and duty cycle deliberately per the ownership mechanics
   above - if it's an interrupter above priority 50, make sure it stops
   publishing during its rest window and sizes `duration_seconds` to the
   remaining active window so it hands the display back cleanly.
4. Add an entry to `apps.json`'s `apps` list (`id`, `path`, `enabled`,
   plus interval/priority fields matching the existing entries' shape).
5. Sanity-check the pure logic standalone and run
   `/usr/local/bin/python3.12 test_controller.py` plus
   `/usr/local/bin/python3.12 -m py_compile apps/<name>_app.py`.
6. Update `README.md`'s app list and this file if you've introduced (or
   fixed) anything future changes should know about.
