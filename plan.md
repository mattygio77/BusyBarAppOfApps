# BusyBar App Extension Plan

This document outlines the design and implementation plan for adding several new sub-apps to the BusyBar controller.

## 1. Sports Apps: Capitals (NHL), DC United (MLS), West Ham (Premier League)

**Concept**: Display live scores, game times, or upcoming match info for these three teams. 
- **API Sources**: 
  - NHL: Unofficial NHL API (`https://api-web.nhle.com/v1/club-schedule/WSH/now` or `score/now`)
  - MLS / Premier League: ESPN hidden APIs (e.g., `site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard` for West Ham, `usa.1` for DC United) or similar open sports APIs.
- **Display Design**:
  - 16x16 team logo (resized/uploaded via `IconUploader`).
  - Text elements showing opponent abbreviation, score, and time remaining (e.g., "WSH 3 - 2 PIT (3rd)").
  - When no game is active, display the next upcoming game date/time (e.g., "WSH vs NYR Tmrw").
- **Priority Strategy**:
  - Priority `70` (higher than weather `50` and stocks `60`) **only while a game is actively being played**, using an active/rest duty cycle (like stocks) so it doesn't monopolize the screen during a 3-hour game.
  - Priority `40` (lower than weather) when displaying "upcoming game" info, so it only shows if weather/stocks aren't claiming the screen.

## 2. Custom ADSB Traffic (Aircraft Overhead)

**Concept**: Display info when aircraft are flying near the user's location.
- **API Sources**: Local PiAware/dump1090 JSON feed (`aircraft.json` over local network), or a service like ADSBExchange.
- **Display Design**:
  - Airplane icon (sourced from Noto Emoji, uploaded via `IconUploader`).
  - Flight number, altitude, and distance/heading (e.g., "UAL123 5k ft 2mi N").
- **Priority Strategy**:
  - Priority `80` (very high). It acts as an **interrupter**.
  - Logic: The app polls the local ADSB feed frequently. If an aircraft is within a tight radius (e.g., 3 miles, below 10,000 ft), it publishes a message with a short duration (e.g., 10 seconds). If no aircraft are nearby, it goes silent (publishes nothing), letting control fall back to weather/stocks.

## 3. Follow-On: Personal Weather Station (PWS)

**Concept**: Replace or supplement the Open-Meteo weather app with hyper-local data.
- **API Sources**: Ambient Weather API, Weather Underground API, or local intercept (e.g., RTL_433 or Ecowitt gateway).
- **Display Design**:
  - Thermometer/rain/wind icons depending on conditions.
  - Exact current temp, humidity, wind speed, and direction.
- **Priority Strategy**: 
  - Priority `50` (same as current weather app). Will likely replace the existing weather app as the always-on default screen, keeping the continuous renewal strategy.

## 4. Follow-On: DC Metro

**Concept**: Next train times for a specific WMATA station.
- **API Sources**: WMATA API (developer key required).
- **Display Design**:
  - Metro logo or colored dot icon based on line color (e.g., Silver/Orange/Blue).
  - Destination and minutes away (e.g., "SV Wiehle 5m", "OR Vienna 12m").
- **Priority Strategy**:
  - Priority `65` (higher than stocks) using an active/rest duty cycle (e.g., show for 20s, rest for 40s) during commuting hours.
  - Priority `45` (lower than weather) during off-hours so it doesn't interrupt.

## Implementation Steps

1. **Setup Shared Sports Utilities**: Create a base class or shared helper in `apps/` for fetching scores/schedules, handling team icons, and managing the active/rest duty cycle to avoid code duplication across the three teams.
2. **Implement `apps/capitals_app.py`**: Start with the NHL API as it's the most documented. Add to `apps.json`.
3. **Implement `apps/soccer_app.py`**: Handle both DC United and West Ham using the same ESPN API structure (just different league/team IDs). Add to `apps.json`.
4. **Implement `apps/adsb_app.py`**: Write the local polling loop for `aircraft.json`, filter by distance, and implement the high-priority interrupter logic. Add to `apps.json`.
