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
   [googlefonts/noto-emoji](https://github.com/googlefonts/noto-emoji) as a
   resource. It's fine to post-process a sourced image (resize to 16x16,
   crop, or recolor by masking/re-tinting an extracted shape) - the
   requirement is that the underlying shape comes from a real downloaded
   asset, not hand-drawn polygons/lines in PIL or similar.
2. **Upload icons through `apps/icon_uploader.py`'s `IconUploader`, not ad
   hoc `busylib` calls.** Both existing apps use it; it owns the `BusyBar`
   client and caches what's already been uploaded this run. A new app
   needing icons should do the same rather than re-implementing upload
   logic. Note `apps/` has no `__init__.py` - it's not a package, and
   every app is invoked directly (`python3 apps/foo_app.py`), which puts
   `apps/` on `sys.path[0]`. That's what makes `from icon_uploader import
   IconUploader` resolve as a plain sibling import. Don't switch to a
   relative/package-style import unless the invocation style changes too.
3. **`display_draw` is additive (upsert-by-id), not a full-screen
   replace.** An element from a previous owner that isn't redeclared will
   keep showing. The controller handles this globally via
   `clear_before_draw=True` on its one `display_draw` call - a sub-app
   itself never needs to clear anything, it just needs to make sure every
   element it cares about is included in the message it publishes (see
   the stocks app's segment culling for an example of "only include what
   should currently be visible").
4. **Every `busylib` field must be the correct native Python type.**
   `scroll_rate`, `priority`, `duration_seconds`, etc. must be `int`, not
   `str` - a wrong type has silently been ignored by the API in the past
   rather than erroring, which made it hard to notice.
5. **`duration_seconds` must be a positive `int`** (see
   `controller/message.py`'s `DisplayMessage.__post_init__`). If you're
   computing it from a sub-second tick interval, round up
   (`math.ceil(...)`) before adding any buffer - see `stock_app.py`.

## Display-ownership mechanics (read before adding a new app)

`controller/display_queue.py` implements this; the summary:

- The highest-priority, non-expired message owns the display.
- Once an app owns the display, **only a strictly higher priority message
  can preempt it early.** Equal-or-lower priority messages just wait, and
  are dropped as stale if they wait longer than their own
  `duration_seconds` without ever being shown.
- An app that republishes fresh content before its own `duration_seconds`
  elapses effectively renews its ownership and never naturally lets go.
  Weather does this deliberately (`duration_seconds = interval +
  stale_buffer`) so it behaves like an "always-on default" screen.
- **Gotcha:** two apps that are both designed to renew indefinitely at
  equal-or-different-but-static priority will result in ONE of them
  permanently owning the display and the other never showing at all - the
  lower/later one just keeps expiring as stale before ever winning. This
  bit us when adding the stocks app. If a new app needs to coexist with an
  existing always-on app, don't just pick a priority number - either:
  (a) give it a strictly higher priority AND make it deliberately stop
  publishing for a while on a duty cycle (active/rest), like
  `stock_app.py` does, so its own ownership lapses and hands control back,
  or
  (b) keep it strictly lower priority and accept that it only ever shows
  during gaps when nothing higher-priority is currently claiming the
  display (fine for genuinely occasional content, not for something
  meant to be seen regularly).
- Redraw identity is `(app_id, message.timestamp)`, not `app_id` alone
  (see `controller.py`'s main loop) - this is what lets an app that's
  already the owner push updated content (weather's temperature changing,
  stocks' scroll position advancing) without being ignored because its
  `app_id` didn't change. Always give a fresh message a new `timestamp`
  (the `DisplayMessage`/`publish_to_redis` pattern both existing apps use
  does this automatically via `time.time()`).
- Idle content (`apps.json`'s `idle_rotation`) is priority 0 and is
  synthesized by the controller itself, not published by any app - it's
  what shows when nothing else is claiming the display.

## Display element schema

Elements are plain dicts (not currently using `busylib`'s
`TextElement`/`ImageElement` classes, though those exist and could be
adopted). Every element needs `id` and `type` (`"text"` or `"image"`).
For the full field reference (fonts: `tiny`/`small`/`normal`/
`condensed`/`bold`/`large`/`extra_large`/`global`; alignment options;
scroll fields), see `busylib`'s own
[`docs/guides/displays.md`](https://github.com/busy-app/busylib-py/blob/main/docs/guides/displays.md) -
don't guess at field names or valid values, check that doc.

Things that have caused real bugs here before:
- Omitting `align`, `scroll_start_delay`, or `scroll_repeat_delay` on text
  elements has caused clipping - set them explicitly rather than relying
  on defaults.
- `scroll_rate` is millisecond-scale (~1000 = normal reading speed); a
  small integer barely scrolls.
- If you're manually animating an element's position frame-to-frame
  (like the stocks tape does, panning `x` every tick), set that element's
  own `scroll_rate: 0` (and the delay fields to `0`) so the device's
  built-in per-element scroll doesn't ALSO move it independently of your
  manual positioning.
- Front display is 72px wide x 16px tall. Leave yourself real margin -
  icons here are 16x16, text columns have generally used 40-54px widths.

## Known quirks worth knowing before you touch things

- `controller.py` defines `_clear_device()` but nothing currently calls
  it - clearing happens via `clear_before_draw=True` passed directly to
  `busylib`'s `display_draw()`. It's dead code, not a bug; don't assume
  it's in the active call path.
- `main.py` does not spawn `apps/*_app.py` as subprocesses. Everything is
  started manually in separate terminals. If you're asked to fix this,
  it's a real gap, not an oversight to work around quietly - flag it.
- Idle rotation only actually triggers a redraw when the display first
  goes idle, not on every rotation through multiple idle items (deferred,
  low-impact while only one idle item is configured).
- Text elements have been observed disappearing after ~10s while an
  accompanying icon element stays on screen. Root cause not yet
  confirmed - suspected but unverified `timeout` field interaction
  between text and image elements. Don't assume this is fixed unless
  you've specifically verified it.

## External APIs sub-apps depend on

- **Open-Meteo** (`weather_app.py`): free, no key, `current_weather`
  includes `is_day` (astronomical, per-location) - this is what drives
  the day/night icon swap. Reasonably stable/documented.
- **Yahoo Finance chart endpoint** (`stock_app.py`):
  `query1.finance.yahoo.com/v8/finance/chart/{ticker}`. Unofficial and
  reverse-engineered (same one `yfinance` sits on top of), no API key,
  and as of this writing doesn't need the crumb/cookie auth handshake
  the older `v7/finance/quote` endpoint requires - but that could change
  without notice since it's not a supported API. Requires a
  browser-like `User-Agent` header or requests get rejected. If stock
  data stops working, check this first.

## Adding a new sub-app - checklist

1. Create `apps/<name>_app.py` following the existing pattern: argparse
   CLI, a class with a `run()` loop, publish `DisplayMessage`-shaped JSON
   (see `controller/message.py`) to Redis channel `busybar:app:<id>`.
2. If it needs icons, use `apps/icon_uploader.py`'s `IconUploader` (see
   Hard rule #2) and source assets per Hard rule #1.
3. Decide priority deliberately per the ownership mechanics above - don't
   just pick a free-looking number. Document *why* in a comment, the way
   `stock_app.py`'s module docstring explains its priority-60-as-
   interrupter choice.
4. Add an entry to `apps.json`'s `apps` list (`id`, `path`, `enabled`,
   plus whatever interval/priority fields match the existing entries'
   shape).
5. Sanity-check the pure logic (position/timing math, formatting, edge
   cases) standalone before wiring it to Redis/busylib - both existing
   apps' scroll/rotation math were verified this way during development,
   independent of needing a live device or Redis instance.
6. `python3 -m py_compile apps/<name>_app.py` at minimum; ideally also
   import and construct the class against a stub `busylib` (see how
   `icon_uploader.py`'s integration was verified) since there's no BusyBar
   hardware available in most dev environments.
7. Update `README.md`'s app list and this file's "known quirks" section
   if you've introduced (or fixed) anything future changes should know
   about.
