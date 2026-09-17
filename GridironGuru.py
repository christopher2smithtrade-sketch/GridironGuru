#!/usr/bin/env python3
"""
============================================================
  GRIDIRON GURU — NFL Weekly Matchup Cheat Sheet
============================================================

  WHAT IT DOES:
  ─────────────────────────────────────────────────────────
  Answers one question: who is set up to EXPLODE this week?

  For every game on the slate it looks at:
    - How many yards does that defense allow to each position?
    - What is the pace of the game (fast/slow)?
    - Which offense is expected to score more?
    - What is the Vegas total / over-under?
    - Is the player healthy?

  Output: a ranked cheat sheet of players to watch,
  sorted by matchup quality. No salary cap. No roster.
  Just: here are the guys built to have a big week.

  SETUP:
    pip install requests
    Optional: set ODDS_API_KEY for live Vegas totals
              (free at https://the-odds-api.com/)

  RUNNING:
    python GridironGuru.py
============================================================
"""

import os
import re
import json
import time
import requests
import webbrowser

from datetime import datetime, date

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")   # handles the EDT/EST switch itself
except Exception:                            # pragma: no cover
    from datetime import timezone, timedelta
    EASTERN = timezone(timedelta(hours=-5))


def to_eastern(iso_utc):
    """
    ESPN publishes kickoffs in UTC. Formatting those directly and labelling them
    'ET' pushed every game 4-5 hours late and rolled night games onto the wrong
    day -- the Week 1 opener read as Thursday when it kicks Wednesday 8:20 PM.
    """
    try:
        return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(EASTERN)
    except Exception:
        return None


# ============================================================
#  CONFIG
# ============================================================

OUTPUT_FOLDER     = "reports"

# Every week's predictions are logged here, then scored against what actually
# happened. Without this the grades are unfalsifiable -- there is no way to
# know whether any of the weighting works.
PREDICTIONS_FOLDER = "predictions"

GRADE_COLORS = {"A+": "#00e676", "A": "#69f0ae", "B+": "#b9f6ca",
                "B": "#fff176", "C": "#ffb74d", "D": "#ff5252", "OUT": "#ff4444"}
# GitHub Actions has no browser; the workflow sets GITHUB_ACTIONS=true.
AUTO_OPEN_BROWSER = os.environ.get("GITHUB_ACTIONS", "") != "true"
DELAY             = 0.3

# Completed-season stats never change, so they are cached to disk.
# Delete this folder (or pass refresh=True) to force a re-pull.
CACHE_FOLDER = "cache"

# --- Odds API budget -------------------------------------------------------
# The free tier is 500 credits/month. The /events list is free, but each game's
# player props costs 3 credits (3 markets x 1 region), so a full 16-game slate
# is 48 credits -- only ~10 full refreshes a month. Without a guard, running
# this a few times a day would exhaust the month in under three days.
PROPS_CACHE_HOURS = 24    # refresh props at most once a day
ODDS_RESERVE      = 60    # never spend below this many credits
PROPS_MAX_GAMES   = 16    # cap games priced per refresh (16 = full slate)

PROP_MARKETS = {
    "player_pass_yds":      "Pass Yds",
    "player_rush_yds":      "Rush Yds",
    "player_reception_yds": "Rec Yds",
}

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER   = "christopher2smithtrade-sketch"
GITHUB_REPO   = "GridironGuru"
GITHUB_BRANCH = "main"

# Players shown per position before "Show All"
TOP_PER_POS = 5

# ── Team colors ──────────────────────────────────────────────
TEAM_COLORS = {
    "Arizona Cardinals":    "#97233F", "Atlanta Falcons":       "#A71930",
    "Baltimore Ravens":     "#241773", "Buffalo Bills":         "#00338D",
    "Carolina Panthers":    "#0085CA", "Chicago Bears":         "#0B162A",
    "Cincinnati Bengals":   "#FB4F14", "Cleveland Browns":      "#311D00",
    "Dallas Cowboys":       "#003594", "Denver Broncos":        "#FB4F14",
    "Detroit Lions":        "#0076B6", "Green Bay Packers":     "#203731",
    "Houston Texans":       "#03202F", "Indianapolis Colts":    "#002C5F",
    "Jacksonville Jaguars": "#006778", "Kansas City Chiefs":    "#E31837",
    "Las Vegas Raiders":    "#A5ACAF", "Los Angeles Chargers":  "#0073CF",
    "Los Angeles Rams":     "#003594", "Miami Dolphins":        "#008E97",
    "Minnesota Vikings":    "#4F2683", "New England Patriots":  "#002244",
    "New Orleans Saints":   "#D3BC8D", "New York Giants":       "#0B2265",
    "New York Jets":        "#125740", "Philadelphia Eagles":   "#004C54",
    "Pittsburgh Steelers":  "#FFB612", "San Francisco 49ers":   "#AA0000",
    "Seattle Seahawks":     "#002244", "Tampa Bay Buccaneers":  "#D50A0A",
    "Tennessee Titans":     "#0C2340", "Washington Commanders": "#5A1414",
}
DEFAULT_COLOR = "#2C3E50"


# ============================================================
#  SEASON / WEEK
# ============================================================

def get_current_week():
    """
    Current season + week. Before the season opens we look ahead to Week 1 --
    the schedule is published months early, so the real slate beats sample data.
    """
    # ESPN's scoreboard knows what week it is and rolls over the morning after
    # Monday night. Counting days from the opener does not -- on the Wednesday
    # of Week 2 it still said Week 1, which would have logged Week 2's
    # predictions into Week 1's file and scored them against the wrong games.
    try:
        r = requests.get(f"{ESPN}/scoreboard", timeout=10).json()
        wk = int(r.get("week", {}).get("number") or 0)
        yr = int(r.get("season", {}).get("year") or 0)
        stype = int(r.get("season", {}).get("type") or 2)
        if yr and wk and stype == 2:
            return yr, wk
    except Exception as e:
        print(f"  [!] ESPN week lookup failed ({e}) -- falling back to date math")

    today = date.today()
    season_start = date(2026, 9, 10)
    if today < season_start:
        return 2026, 1
    week = min(((today - season_start).days // 7) + 1, 18)
    return 2026, week


# ============================================================
#  DATA FETCHING
# ============================================================

def fetch(url, params=None, label=""):
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        time.sleep(DELAY)
        return r.json()
    except Exception as e:
        print(f"  [!] {label or url}: {e}")
        return {}


ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"


def get_games(season, week):
    data = fetch(f"{ESPN}/scoreboard",
                 params={"seasontype": 2, "week": week, "dates": season},
                 label="scoreboard")
    games = []
    for ev in data.get("events", []):
        comp  = ev.get("competitions", [{}])[0]
        teams = comp.get("competitors", [])
        if len(teams) < 2:
            continue
        home = next((t for t in teams if t.get("homeAway") == "home"), teams[0])
        away = next((t for t in teams if t.get("homeAway") == "away"), teams[1])
        games.append({
            "game_id":   ev.get("id"),
            "home_team": home["team"].get("displayName", ""),
            "away_team": away["team"].get("displayName", ""),
            "home_abbr": home["team"].get("abbreviation", ""),
            "away_abbr": away["team"].get("abbreviation", ""),
            "kickoff":   ev.get("date", ""),
            "venue":     comp.get("venue", {}).get("fullName", ""),
            "indoor":    comp.get("venue", {}).get("indoor", False),
            # Kept so an unlisted venue can still be geocoded by city
            # (the NFL plays international games at non-NFL stadiums).
            "city":      comp.get("venue", {}).get("address", {}).get("city", ""),
            "country":   comp.get("venue", {}).get("address", {}).get("country", ""),
            # Free DraftKings spread + total, so get_vegas() need not spend
            # Odds API credits just to learn the game total.
            "odds":      {
                "over_under": (comp.get("odds") or [{}])[0].get("overUnder"),
                "spread":     (comp.get("odds") or [{}])[0].get("spread"),
            },
            # Used to freeze the prediction log once results exist
            "completed": bool((comp.get("status") or {})
                              .get("type", {}).get("completed", False)),
            # A prediction must lock at KICKOFF, not at the final whistle. If
            # only completed games froze, a run during the 4:25 window would
            # overwrite the pre-game call with whatever the in-game data said.
            "started":   (comp.get("status") or {}).get("type", {})
                              .get("name", "") != "STATUS_SCHEDULED",
            # International / neutral-site games have a nominal "home" team but
            # no home crowd. The Rams were credited home field for a game in
            # Melbourne, which nudged their edge up on a night they had none.
            "neutral":   bool(comp.get("neutralSite", False)),
        })
    # Only fall back to samples if the real schedule genuinely isn't published yet
    return games or sample_games()


def sample_games():
    print("  Offseason -- using sample games for testing.")
    return [
        {"game_id":"1","home_team":"Kansas City Chiefs",  "away_team":"Buffalo Bills",
         "home_abbr":"KC",  "away_abbr":"BUF","kickoff":"2026-09-10T20:20:00Z","venue":"Arrowhead Stadium","indoor":False},
        {"game_id":"2","home_team":"Dallas Cowboys",      "away_team":"Philadelphia Eagles",
         "home_abbr":"DAL", "away_abbr":"PHI","kickoff":"2026-09-13T17:00:00Z","venue":"AT&T Stadium","indoor":True},
        {"game_id":"3","home_team":"San Francisco 49ers", "away_team":"Los Angeles Rams",
         "home_abbr":"SF",  "away_abbr":"LAR","kickoff":"2026-09-13T20:25:00Z","venue":"Levi's Stadium","indoor":False},
        {"game_id":"4","home_team":"Miami Dolphins",      "away_team":"New England Patriots",
         "home_abbr":"MIA", "away_abbr":"NE", "kickoff":"2026-09-13T17:00:00Z","venue":"Hard Rock Stadium","indoor":False},
        {"game_id":"5","home_team":"Green Bay Packers",   "away_team":"Chicago Bears",
         "home_abbr":"GB",  "away_abbr":"CHI","kickoff":"2026-09-13T13:00:00Z","venue":"Lambeau Field","indoor":False},
    ]


def get_injuries():
    """Fetch NFL injury report from ESPN."""
    data = fetch(f"{ESPN}/injuries", label="injuries")
    out  = {}
    for team in data.get("injuries", []):
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {}) or {}
            # This endpoint uses displayName -- there is no fullName field here,
            # and reading the wrong key silently yields zero injuries.
            name = ath.get("displayName") or ath.get("fullName") or ""
            status = inj.get("status", "Active")
            if not name or status in ("Active", ""):
                continue
            # Prefer the short structured description ("Ankle Sprain") over the
            # multi-sentence beat-writer comment, which is too long for a card.
            d = inj.get("details") or {}
            detail = " ".join(x for x in [d.get("type"), d.get("detail")]
                              if x and x != "Not Specified").strip()
            if not detail:
                detail = (inj.get("shortComment") or "")[:90]
            out[name] = {"status": status, "detail": detail}
    if not out:
        # No fabricated fallback: a fake injury report is worse than none.
        print("  [!] Injury pull returned nothing -- cards will show no injury badges")
    return out


def get_vegas(games):
    """
    Implied team totals from the game spread and over/under.

    ESPN's scoreboard carries DraftKings spread + overUnder for free, so that is
    the primary source -- it costs nothing and covers the full slate. The Odds
    API path below is only a fallback, because it is metered (2 credits a call)
    and it was quietly spending quota on every single run.
    """
    totals = {g["home_abbr"]: 24.0 for g in games}
    totals.update({g["away_abbr"]: 24.0 for g in games})
    game_totals = {g["home_abbr"]: 48.0 for g in games}

    got = 0
    for g in games:
        od = g.get("odds") or {}
        total, spread = od.get("over_under"), od.get("spread")
        if total is None or spread is None:
            continue
        # ESPN's spread is quoted from the home team's perspective
        home_adj = total / 2 - spread / 2
        totals[g["home_abbr"]] = round(home_adj, 1)
        totals[g["away_abbr"]] = round(total - home_adj, 1)
        game_totals[g["home_abbr"]] = total
        game_totals[g["away_abbr"]] = total
        got += 1

    # ESPN drops the line once a game is final, so completed games are not
    # "missing" data -- counting them as missing would trigger the metered
    # fallback on every run for the rest of the week.
    need = [g for g in games if not g.get("completed")]
    if got:
        print(f"  Vegas lines for {got}/{len(games)} games from ESPN (free)")
        if got >= len(need):
            return totals, game_totals

    if not ODDS_API_KEY:
        return totals, game_totals
    print("  Falling back to The Odds API for the remaining games (2 credits)")

    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/",
            params={"apiKey": ODDS_API_KEY, "regions": "us",
                    "markets": "totals,spreads", "oddsFormat": "american"},
            timeout=10,
        )
        r.raise_for_status()
        for ev in r.json():
            home_name = ev.get("home_team", "")
            spread = total = None
            for bm in ev.get("bookmakers", [])[:1]:
                for mkt in bm.get("markets", []):
                    if mkt["key"] == "totals":
                        total = float(mkt["outcomes"][0].get("point", 48))
                    if mkt["key"] == "spreads":
                        for o in mkt["outcomes"]:
                            if o["name"] == home_name:
                                spread = float(o.get("point", 0))
            if total:
                home_adj = total / 2 - (spread or 0) / 2
                away_adj = total - home_adj
                for g in games:
                    if g["home_team"] == home_name:
                        totals[g["home_abbr"]] = round(home_adj, 1)
                        totals[g["away_abbr"]] = round(away_adj, 1)
                        game_totals[g["home_abbr"]] = total
                        game_totals[g["away_abbr"]] = total
    except Exception as e:
        print(f"  [!] Vegas: {e}")

    return totals, game_totals


VENUE_COORDS = {
    "Arrowhead Stadium":         (39.0489, -94.4839),
    "AT&T Stadium":              (32.7473, -97.0945),
    "Levi's Stadium":            (37.4032, -121.9698),
    "Hard Rock Stadium":         (25.9580, -80.2389),
    "Lambeau Field":             (44.5013, -88.0622),
    "SoFi Stadium":              (33.9535, -118.3392),
    "Highmark Stadium":          (42.7738, -78.7870),
    "Empower Field":             (39.7439, -105.0201),
    "Soldier Field":             (41.8623, -87.6167),
    "MetLife Stadium":           (40.8135, -74.0745),
    "Lincoln Financial Field":   (39.9007, -75.1675),
    "M&T Bank Stadium":          (39.2780, -76.6227),
    "Gillette Stadium":          (42.0909, -71.2643),
    "NRG Stadium":               (29.6847, -95.4107),
    "Lucas Oil Stadium":         (39.7601, -86.1639),
    "Bank of America Stadium":   (35.2258, -80.8528),
    "Ford Field":                (42.3400, -83.0456),
    "Paul Brown Stadium":        (39.0955, -84.5160),
    "FirstEnergy Stadium":       (41.5061, -81.6995),
    "Nissan Stadium":            (36.1665, -86.7713),
    "Raymond James Stadium":     (27.9759, -82.5033),
    "TIAA Bank Field":           (30.3239, -81.6373),
    "Caesars Superdome":         (29.9511, -90.0812),
    "Lumen Field":               (47.5952, -122.3316),
    "State Farm Stadium":        (33.5276, -112.2626),
    "Allegiant Stadium":         (36.0909, -115.1833),
    "US Bank Stadium":           (44.9738, -93.2575),
    "FedExField":                (38.9077, -76.8644),
    "Acrisure Stadium":          (40.4468, -80.0158),
    "EverBank Stadium":          (30.3239, -81.6373),
    "Paycor Stadium":            (39.0955, -84.5160),
}

def geocode_city(city, country=""):
    """
    Resolve a city to (lat, lon) via Open-Meteo's free geocoder, so a venue
    missing from VENUE_COORDS still gets real weather. The NFL plays regular
    season games abroad (Melbourne, London, Munich, Sao Paulo...) at stadiums
    that will never be in a hardcoded NFL map. Results are cached.
    """
    if not city:
        return None
    key   = f"{city}|{country}".lower()
    cache = cache_load("geocode") or {}
    if key in cache:
        return tuple(cache[key]) if cache[key] else None
    coords = None
    try:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 5, "language": "en", "format": "json"},
            timeout=10,
        )
        results = r.json().get("results", []) or []
        match = None
        if country:
            c = country.lower()
            match = next((x for x in results
                          if c in (x.get("country", "") + x.get("country_code", "")).lower()), None)
            # ESPN says "USA"; the geocoder says "United States"
            if not match and c in ("usa", "us"):
                match = next((x for x in results if x.get("country_code") == "US"), None)
        match = match or (results[0] if results else None)
        if match:
            coords = (match["latitude"], match["longitude"])
            print(f"  Geocoded {city} -> {coords[0]:.2f},{coords[1]:.2f}")
        time.sleep(DELAY)
    except Exception as e:
        print(f"  [!] Geocode {city}: {e}")
    cache[key] = list(coords) if coords else None
    cache_save("geocode", cache)
    return coords


def get_weather(games):
    """
    Fetch game-day weather for outdoor venues using Open-Meteo (free, no key).
    Returns dict: game_id -> {temp_f, wind_mph, precip_mm, condition, impact}
    """
    weather = {}
    for g in games:
        if g.get("indoor"):
            weather[g["game_id"]] = {"temp_f": 72, "wind_mph": 0, "precip_mm": 0,
                                      "condition": "Dome", "impact": "None"}
            continue
        coords = VENUE_COORDS.get(g.get("venue", "")) or geocode_city(g.get("city", ""),
                                                                      g.get("country", ""))
        if not coords:
            weather[g["game_id"]] = {"temp_f": 65, "wind_mph": 8, "precip_mm": 0,
                                      "condition": "Unknown", "impact": "Unknown"}
            continue
        try:
            kickoff = g.get("kickoff", "")
            ko_et = to_eastern(kickoff) if kickoff else None
            game_date = ko_et.date().isoformat() if ko_et else date.today().isoformat()
            # Open-Meteo forecasts 16 days out. Beyond that say so rather than
            # substituting today's weather, which would just be a wrong forecast.
            from datetime import timedelta
            # 15, not 16: the API's window shifts during the day and a 16-day-out
            # request started returning 400 once the date rolled over.
            if game_date > (date.today() + timedelta(days=15)).isoformat():
                weather[g["game_id"]] = {
                    "temp_f": 65, "wind_mph": 0, "precip_mm": 0,
                    "condition": "Not yet forecast",
                    "impact": "Too far out -- check back within 16 days",
                }
                continue
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": coords[0], "longitude": coords[1],
                    "hourly": "temperature_2m,windspeed_10m,precipitation",
                    "temperature_unit": "fahrenheit",
                    "windspeed_unit": "mph",
                    "start_date": game_date, "end_date": game_date,
                    "timezone": "America/New_York",
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json().get("hourly", {})
            # Read the forecast at actual kickoff -- a 8:15pm game is much cooler
            # and usually calmer than the 1pm slot this used to assume.
            idx = 13
            try:
                idx = min(23, max(0, ko_et.hour))   # already Eastern
            except Exception:
                pass
            temp  = data.get("temperature_2m", [65]*24)[idx]
            wind  = data.get("windspeed_10m",  [8]*24)[idx]
            prec  = data.get("precipitation",  [0]*24)[idx]
            time.sleep(DELAY)

            if wind >= 20:     impact = "High Wind -- passing game hurt"
            elif wind >= 15:   impact = "Moderate Wind -- slight passing impact"
            elif prec >= 2.0:  impact = "Rain/Snow -- ball security risk"
            elif temp <= 25:   impact = "Extreme Cold -- scoring suppressed"
            else:              impact = "Good conditions"

            cond = "Rain" if prec >= 1 else ("Snow" if temp <= 32 and prec > 0 else "Clear")
            weather[g["game_id"]] = {
                "temp_f": round(temp), "wind_mph": round(wind, 1),
                "precip_mm": round(prec, 1), "condition": cond, "impact": impact,
            }
        except Exception as e:
            print(f"  [!] Weather {g['venue']}: {e}")
            weather[g["game_id"]] = {"temp_f": 65, "wind_mph": 8, "precip_mm": 0,
                                      "condition": "Unknown", "impact": "Unknown"}
    return weather


def get_player_props(games):
    """
    Fetch Vegas player props (passing/rushing/receiving yards) from The Odds API.
    Returns dict: player_name -> {line, over_odds, under_odds, stat}
    Falls back to sample data if no key.
    """
    if not ODDS_API_KEY:
        # No fabricated fallback -- invented betting lines are worse than none.
        print("  [!] ODDS_API_KEY not set -- no player props")
        return {}

    # Props move, but not minute to minute. Cache so repeated runs in a day
    # cost nothing -- the free tier only affords ~10 full refreshes a month.
    cached  = cache_load("props")
    age_h   = (time.time() - cached.get("fetched_at", 0)) / 3600 if cached else None
    if cached and age_h < PROPS_CACHE_HOURS:
        print(f"  Using cached props ({age_h:.1f}h old, next refresh in "
              f"{PROPS_CACHE_HOURS - age_h:.1f}h)")
        return cached["props"]

    props = {}
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events",
            params={"apiKey": ODDS_API_KEY}, timeout=10,
        )
        r.raise_for_status()
        events = r.json()

        # The events list is free AND reports the balance, so budget can be
        # checked at zero cost before committing to any paid calls.
        remaining = int(r.headers.get("x-requests-remaining", 0) or 0)
        need      = min(len(games), PROPS_MAX_GAMES) * 3
        print(f"  Odds API: {remaining} credits left, this refresh needs ~{need}")

        if remaining - need < ODDS_RESERVE:
            if cached:
                print(f"  [!] Below reserve ({ODDS_RESERVE}) -- keeping cached props "
                      f"({age_h:.1f}h old) instead of spending")
                return cached["props"]
            print(f"  [!] Below reserve ({ODDS_RESERVE}) -- skipping props this run")
            return {}

        # Match the book's events to the games actually on our slate, instead of
        # blindly taking the first N of a full-season event list.
        # ESPN and The Odds API both use full names ("Seattle Seahawks"), so match
        # on the nickname -- the last word -- which is unique across the league.
        def nick(full):
            return full.strip().split()[-1].lower() if full else "~"

        wanted = {}
        for g in games:
            for ev in events:
                book_teams = {nick(ev.get("home_team", "")), nick(ev.get("away_team", ""))}
                if {nick(g["home_team"]), nick(g["away_team"])} == book_teams:
                    wanted[ev["id"]] = g["game_id"]
                    break

        priced = 0
        for eid in list(wanted)[:PROPS_MAX_GAMES]:
            pr = requests.get(
                f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{eid}/odds",
                params={
                    "apiKey": ODDS_API_KEY, "regions": "us",
                    "markets": "player_pass_yds,player_rush_yds,player_reception_yds",
                    "oddsFormat": "american",
                },
                timeout=10,
            )
            time.sleep(0.5)
            # Re-check after every paid call: stop the moment we hit the reserve,
            # so an unexpected price change can't drain the month mid-loop.
            remaining = int(pr.headers.get("x-requests-remaining", remaining) or remaining)
            priced += 1
            if remaining <= ODDS_RESERVE:
                print(f"  [!] Hit reserve after {priced} games -- stopping "
                      f"({remaining} credits left)")
                break
            books = pr.json().get("bookmakers", [])
            # This is a DraftKings tool, so use DraftKings lines when offered
            book = next((b for b in books if b.get("key") == "draftkings"), None) or (books[0] if books else None)
            if not book:
                continue
            for mkt in book.get("markets", []):
                stat = PROP_MARKETS.get(mkt["key"])
                if not stat:
                    continue
                for outcome in mkt.get("outcomes", []):
                    if outcome.get("name") != "Over":
                        continue
                    name = outcome.get("description", "")
                    if not name:
                        continue
                    # Key on (name, stat): a QB has both a passing AND a rushing
                    # line, and keying on name alone lets one clobber the other.
                    props[f"{name}|{stat}"] = {
                        "line": outcome.get("point", 0),
                        "stat": stat,
                        "over": outcome.get("price", -110),
                        "book": book.get("key", ""),
                    }
        print(f"  Priced {priced} games -- {remaining} credits left this month")
        cache_save("props", {"fetched_at": time.time(), "props": props,
                             "remaining": remaining})
    except Exception as e:
        print(f"  [!] Player props: {e}")
        # A failed refresh should fall back to stale props, not to nothing
        if cached:
            print(f"  Falling back to cached props ({age_h:.1f}h old)")
            return cached["props"]
    return props


# Every ESPN prop type that carries a usable player line. Touchdown-scorer
# markets are deliberately absent: they are price-only, and ESPN never
# publishes prices, so they come back with no value attached.
# "Milestones" variants are skipped too -- they are tiered duplicates of these.
ESPN_PROP_TYPES = {
    "Total Passing Yards (incl. overtime)":      "Pass Yds",
    "Total Passing Touchdowns (incl. overtime)": "Pass TDs",
    "Total Rushing Yards (incl. overtime)":      "Rush Yds",
    "Total Receiving Yards (incl. overtime)":    "Rec Yds",
    "Total Receptions (incl. overtime)":         "Receptions",
}

# Order props appear on the card, by position
PROP_ORDER = {
    "QB": ["Pass Yds", "Pass TDs", "Rush Yds"],
    "RB": ["Rush Yds", "Rec Yds", "Receptions"],
    "WR": ["Rec Yds", "Receptions", "Rush Yds"],
    "TE": ["Rec Yds", "Receptions"],
}


def get_espn_props(games):
    """
    Player prop LINES from ESPN -- free, no key, no quota.

    ESPN republishes DraftKings numbers (casino id 100) and the lines match The
    Odds API exactly. The catch: ESPN publishes only the line, never the price,
    so there is no over/under juice here. Keyed by "espn_id|stat" because these
    entries identify players by athlete id rather than name.
    """
    props = {}
    for g in games:
        eid = g["game_id"]
        try:
            j = requests.get(
                f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
                f"events/{eid}/competitions/{eid}/odds/100/propBets",
                params={"limit": 200}, timeout=15,
            ).json()
            for it in j.get("items", []):
                stat = ESPN_PROP_TYPES.get(it.get("type", {}).get("name", ""))
                if not stat:
                    continue
                line = ((it.get("current") or {}).get("target") or {}).get("value")
                ref  = (it.get("athlete") or {}).get("$ref", "")
                pid  = ref.split("/athletes/")[-1].split("?")[0] if "/athletes/" in ref else ""
                if line and pid.isdigit():
                    props[f"{pid}|{stat}"] = {
                        "line": line, "stat": stat, "over": 0, "book": "DraftKings",
                    }
            time.sleep(DELAY)
        except Exception as e:
            print(f"  [!] ESPN props {g['away_abbr']}@{g['home_abbr']}: {e}")
    return props


def props_for(player, props):
    """
    Every prop line available for this player, ordered by what matters most for
    the position. Collecting them in one place is the point -- the board points
    at a player, and the actual bet gets placed on DraftKings.
    """
    pid  = str(player.get("espn_id", ""))
    name = player["name"]
    out  = []
    for stat in PROP_ORDER.get(player["position"], ["Rec Yds"]):
        p = props.get(f"{name}|{stat}") or props.get(f"{pid}|{stat}")
        if p and p.get("line"):
            out.append({"stat": stat, "line": p["line"], "over": p.get("over", 0)})
    return out


# ESPN team abbreviation -> ID
TEAM_IDS = {
    "ARI":"22","ATL":"1", "BAL":"33","BUF":"2", "CAR":"29","CHI":"3",
    "CIN":"4", "CLE":"5", "DAL":"6", "DEN":"7", "DET":"8", "GB":"9",
    "HOU":"34","IND":"11","JAX":"30","KC":"12", "LAC":"24","LAR":"14",
    "LV":"13", "MIA":"15","MIN":"16","NE":"17", "NO":"18", "NYG":"19",
    "NYJ":"20","PHI":"21","PIT":"23","SEA":"26","SF":"25", "TB":"27",
    "TEN":"10","WSH":"28",   # ESPN uses WSH, not WAS -- must match or lookups silently fail
}

STAT_SEASON = 2025   # most recently completed season (2025 finished Feb 2026)


# ============================================================
#  NFLVERSE  (snap counts + EPA)
# ============================================================
#
# ESPN has no snap counts and no EPA, so those come from nflverse via
# nflreadpy. Install with:  pip install nflreadpy
#
# nflverse uses two different team abbreviations than ESPN. Getting this
# wrong silently drops the Rams and Commanders -- the same failure mode
# that once corrupted Washington's defensive numbers.
NFLVERSE_TO_ESPN = {"LA": "LAR", "WAS": "WSH"}

# Set false to run without nflverse (cards fall back to the ESPN-only fields)
USE_NFLVERSE = True


def get_json(url, params=None, tries=3, timeout=15):
    """
    GET with retries. A single transient timeout used to silently drop a whole
    team from the pool (BUF and CLE vanished on one run), so anything the pool
    depends on goes through here.
    """
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}


def _norm_name(n):
    """Lowercase, strip suffixes and punctuation, for cross-source name matching."""
    n = (n or "").lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n)
    n = re.sub(r"[^a-z ]", "", n)
    return " ".join(n.split())


def get_nflverse_stats(season=STAT_SEASON, refresh=False):
    """
    Snap counts and EPA from nflverse.

    Returns {"snaps": {norm_name: {...}}, "snaps_by_last": {(last,team): {...}},
             "off": {espn_abbr: {...}}, "def": {espn_abbr: {...}}}

    EPA (expected points added per play) measures how much each play actually
    moved the needle, so it captures sacks, turnovers and efficiency that raw
    yards-allowed misses entirely. Play-by-play is ~49k rows, so results are
    cached to disk -- the pull takes a couple of minutes cold.
    """
    empty = {"snaps": {}, "snaps_by_last": {}, "off": {}, "def": {}}
    if not USE_NFLVERSE:
        return empty

    cache_key = f"nflverse_{season}"
    if not refresh:
        cached = cache_load(cache_key)
        if cached:
            # JSON can't hold tuple keys; rebuild the last-name index
            cached["snaps_by_last"] = {tuple(k.split("|")): v
                                       for k, v in cached.get("snaps_by_last", {}).items()}
            print(f"  Loaded nflverse ({len(cached['snaps'])} players, "
                  f"{len(cached['off'])} teams) from cache")
            return cached

    try:
        import nflreadpy as nfl
        import polars as pl
    except ImportError:
        print("  [!] nflreadpy not installed -- skipping snap counts / EPA "
              "(pip install nflreadpy)")
        return empty

    out = {"snaps": {}, "snaps_by_last": {}, "off": {}, "def": {}}

    # ---- Snap counts -------------------------------------------------
    try:
        print(f"  Pulling {season} snap counts from nflverse...")
        sc = nfl.load_snap_counts(seasons=[season]).filter(pl.col("game_type") == "REG")
        agg = (sc.group_by(["player", "team", "position"])
                 .agg(pl.col("offense_pct").mean().alias("snap"),
                      pl.col("st_pct").mean().alias("st"),
                      pl.len().alias("games")))
        for r in agg.iter_rows(named=True):
            if r["snap"] is None:
                continue
            team = NFLVERSE_TO_ESPN.get(r["team"], r["team"])
            rec = {"snap_pct": round(r["snap"] * 100, 1),
                   "st_pct":   round((r["st"] or 0) * 100, 1),
                   "games":    r["games"], "team": team}
            out["snaps"][_norm_name(r["player"])] = rec
            last = _norm_name(r["player"]).split()[-1] if r["player"] else ""
            if last:
                out["snaps_by_last"][(last, team)] = rec
        print(f"  Snap counts for {len(out['snaps'])} players")
    except Exception as e:
        print(f"  [!] nflverse snap counts: {e}")

    # ---- EPA ---------------------------------------------------------
    try:
        print(f"  Pulling {season} play-by-play for EPA (this takes a minute)...")
        pbp = nfl.load_pbp(seasons=[season]).filter(
            (pl.col("season_type") == "REG") & pl.col("play_type").is_in(["pass", "run"]))

        def side(group_col, pass_only=None):
            df = pbp
            if pass_only is not None:
                df = df.filter(pl.col("play_type") == ("pass" if pass_only else "run"))
            return (df.group_by(group_col)
                      .agg(pl.col("epa").mean().alias("epa"),
                           pl.col("success").mean().alias("succ"),
                           pl.len().alias("n"))
                      .drop_nulls())

        for row in side("posteam").iter_rows(named=True):
            t = NFLVERSE_TO_ESPN.get(row["posteam"], row["posteam"])
            out["off"][t] = {"epa_play": round(row["epa"], 4),
                             "success":  round((row["succ"] or 0) * 100, 1)}
        for row in side("defteam").iter_rows(named=True):
            t = NFLVERSE_TO_ESPN.get(row["defteam"], row["defteam"])
            out["def"][t] = {"epa_allowed": round(row["epa"], 4),
                             "success_allowed": round((row["succ"] or 0) * 100, 1)}
        # Split defensive EPA by play type -- a soft pass D is a different
        # matchup from a soft run D, and that distinction drives the grade.
        for is_pass, key in ((True, "pass_epa_allowed"), (False, "rush_epa_allowed")):
            for row in side("defteam", pass_only=is_pass).iter_rows(named=True):
                t = NFLVERSE_TO_ESPN.get(row["defteam"], row["defteam"])
                out["def"].setdefault(t, {})[key] = round(row["epa"], 4)
        for is_pass, key in ((True, "pass_epa"), (False, "rush_epa")):
            for row in side("posteam", pass_only=is_pass).iter_rows(named=True):
                t = NFLVERSE_TO_ESPN.get(row["posteam"], row["posteam"])
                out["off"].setdefault(t, {})[key] = round(row["epa"], 4)
        print(f"  EPA computed for {len(out['off'])} offenses, {len(out['def'])} defenses")

        # ---- Real receiving yards allowed BY POSITION --------------
        # Previously wr_ypg/te_ypg/rb_rec_ypg were pass_ypg times a fixed
        # constant (0.62 / 0.20 / 0.18). Because they were multiples of one
        # number, every team's WR rank was identical to its overall pass-defense
        # rank -- the WR and TE matchup carried no independent signal at all.
        # Real per-position numbers move teams an average of 10 rank places.
        try:
            players = nfl.load_players().select(["gsis_id", "position"])
            rec = (pbp.filter(pl.col("play_type") == "pass")
                      .select(["defteam", "game_id", "receiver_player_id", "receiving_yards"])
                      .join(players, left_on="receiver_player_id",
                            right_on="gsis_id", how="left")
                      .filter(pl.col("position").is_in(["WR", "TE", "RB"])))

            gp = (pbp.group_by("defteam")
                     .agg(pl.col("game_id").n_unique().alias("g")))
            games_by_team = {r["defteam"]: max(1, r["g"]) for r in gp.iter_rows(named=True)}

            totals = (rec.group_by(["defteam", "position"])
                         .agg(pl.col("receiving_yards").sum().alias("yds")))
            field = {"WR": "wr_ypg", "TE": "te_ypg", "RB": "rb_rec_ypg"}
            hits = 0
            for r in totals.iter_rows(named=True):
                key = field.get(r["position"])
                if not key:
                    continue
                t = NFLVERSE_TO_ESPN.get(r["defteam"], r["defteam"])
                g = games_by_team.get(r["defteam"], 17)
                out["def"].setdefault(t, {})[key] = round((r["yds"] or 0) / g, 1)
                hits += 1
            print(f"  Real per-position receiving yards allowed: {hits} team/position rows")
        except Exception as e:
            print(f"  [!] Per-position receiving yards: {e} -- keeping estimates")
    except Exception as e:
        print(f"  [!] nflverse EPA: {e}")

    saveable = dict(out)
    saveable["snaps_by_last"] = {f"{k[0]}|{k[1]}": v for k, v in out["snaps_by_last"].items()}
    cache_save(cache_key, saveable)
    return out


def lookup_snap(name, team, nv):
    """Snap % for a player, falling back to last-name+team for nicknames
    (ESPN says 'Hollywood Brown', nflverse says 'Marquise Brown')."""
    rec = nv["snaps"].get(_norm_name(name))
    if rec:
        return rec
    parts = _norm_name(name).split()
    if parts:
        return nv["snaps_by_last"].get((parts[-1], team))
    return None

# Which stat category feeds which position group.
LEADER_CATEGORIES = [
    ("passingYards",   "Pass Yds/gm", ("QB",)),
    ("rushingYards",   "Rush Yds/gm", ("RB",)),
    ("receivingYards", "Rec Yds/gm",  ("WR", "TE")),
]

# How many players to keep per team, per position.
#
# The pool is built TEAM BY TEAM rather than from league-wide leaderboards. A
# league top-24 is roughly a per-team average of less than one, so whole teams
# ended up with nobody: Seattle had zero running backs (Walker and Charbonnet
# split 2025 carries, so neither cracked the top 24), and eight teams had no
# quarterback at all. A matchup tool cannot advise on a game it has no players
# for, so every team now contributes its own leaders.
TEAM_POOL_LIMITS = {"QB": 2, "RB": 3, "WR": 4, "TE": 2}

# How far down each team's leader list to look before giving up
LEADER_SCAN_DEPTH = 8


def _cache_path(key):
    return os.path.join(CACHE_FOLDER, f"{key}.json")


def cache_load(key):
    """Return cached JSON for key, or None if not cached / unreadable."""
    try:
        with open(_cache_path(key), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cache_save(key, data):
    """Write data to the cache; failures are non-fatal."""
    try:
        os.makedirs(CACHE_FOLDER, exist_ok=True)
        with open(_cache_path(key), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"  [!] Cache write {key}: {e}")


UNAVAILABLE = ("Out", "Injured Reserve", "IR", "PUP", "Suspension", "Doubtful")


def build_pool_from_depth_charts(offense_stats, nv=None, season=STAT_SEASON,
                                 depth_season=None, refresh=False, injuries=None):
    """
    Build the pool from the CURRENT depth chart, not last year's leaderboard.

    Ranking players by prior-season production cannot surface a starter who is a
    rookie or who missed last year -- New England's RB1 (Rhamondre Stevenson) and
    Seattle's RB1 (Jadarian Price) were both simply absent, while 5%-snap
    backups occupied the card. The depth chart says who is actually starting
    right now; prior-season stats are then attached as the production baseline,
    and players with no history are kept and flagged rather than dropped.
    """
    depth_season = depth_season or (season + 1)
    try:
        import nflreadpy as nfl
        import polars as pl
    except ImportError:
        print("  [!] nflreadpy missing -- falling back to leaderboard pool")
        return None

    try:
        dc = nfl.load_depth_charts(seasons=[depth_season])
        stamp = dc.select(pl.col("dt").max()).item()
        cur = dc.filter(pl.col("dt") == stamp)
    except Exception as e:
        print(f"  [!] Depth charts unavailable ({e}) -- falling back to leaderboard pool")
        return None

    # Cache keyed on the chart timestamp so a new chart forces a rebuild
    injuries = injuries or {}
    out_names = sorted(n for n, v in injuries.items() if v.get("status") in UNAVAILABLE)
    # hashlib, not hash(): the builtin is salted per process, which would make
    # this key differ on every run and silently defeat the cache.
    import hashlib
    avail_sig = hashlib.md5("|".join(out_names).encode()).hexdigest()[:8]
    cache_key = f"pool_depth_{depth_season}_{str(stamp)[:10]}_{avail_sig}"
    if not refresh:
        cached = cache_load(cache_key)
        if cached:
            print(f"  Loaded {len(cached['players'])} players from cache "
                  f"(depth chart {str(stamp)[:10]})")
            return cached["players"], cached["recent_form"]

    print(f"  Building pool from {depth_season} depth charts ({str(stamp)[:16]})...")
    players, recent_form, seen = [], {}, set()

    rows = (cur.filter(pl.col("pos_abb").is_in(list(TEAM_POOL_LIMITS)))
               .sort(["team", "pos_abb", "pos_rank"]))

    label_for = {"QB": "Pass Yds/gm", "RB": "Rush Yds/gm",
                 "WR": "Rec Yds/gm", "TE": "Rec Yds/gm"}
    counts = {}

    for r in rows.iter_rows(named=True):
        pos  = r["pos_abb"]
        team = NFLVERSE_TO_ESPN.get(r["team"], r["team"])
        pid  = str(r["espn_id"] or "").split(".")[0]
        name = r["player_name"]
        if not pid.isdigit() or not name or pid in seen:
            continue
        key = (team, pos)
        # A player who is Out still gets a card (so the board SAYS he is out),
        # but he must not use up a roster slot -- otherwise a team with both
        # quarterbacks injured shows two OUT cards and never shows the man who
        # is actually starting. Atlanta week 1: Tua and Penix out, Cooper Rush
        # starting and absent from the board.
        available = injuries.get(name, {}).get("status") not in UNAVAILABLE
        if counts.get(key, 0) >= TEAM_POOL_LIMITS.get(pos, 3):
            continue
        seen.add(pid)
        if available:
            counts[key] = counts.get(key, 0) + 1

        stats = _athlete_season_stats(pid, season)
        games = stats.get("gamesPlayed") or 0
        total = (stats.get("passingYards", 0) if pos == "QB"
                 else stats.get("rushingYards", 0) if pos == "RB"
                 else stats.get("receivingYards", 0))
        avg = round(total / games, 1) if games else 0.0

        # Rushing production is kept for every position, because a QB's legs
        # are real output that a passing-yards-only projection cannot see.
        # Jalen Hurts ran for 421 yards and 8 scores in 2025; none of that
        # reached his projection until this was tracked.
        player = {"name": name, "position": pos, "team": team, "espn_id": pid,
                  "depth_rank": int(r["pos_rank"] or 9),
                  "rush_ypg": round(stats.get("rushingYards", 0) / games, 1) if games else 0.0,
                  "rush_tds": int(stats.get("rushingTouchdowns", 0) or 0),
                  "pass_tds": int(stats.get("passingTouchdowns", 0) or 0),
                  "rec_tds":  int(stats.get("receivingTouchdowns", 0) or 0)}
        player.update(_usage(pos, stats, team, offense_stats))
        snap = lookup_snap(name, team, nv) if nv else None
        if snap:
            player["snap_pct"] = snap["snap_pct"]
        players.append(player)

        recent_form[name] = {
            "stat_label":   label_for.get(pos, "Yds/gm"),
            "trend":        _form_trend(pos, avg) if games else "No 2025 data",
            "last3":        [round(avg)] * 3,
            "avg3":         avg,
            "season_total": round(total),
            "games":        int(games),
        }
        time.sleep(DELAY)

    gaps = [f"{t} {p}" for t in TEAM_IDS for p in ("QB", "RB", "WR", "TE")
            if counts.get((t, p), 0) == 0]
    rookies = sum(1 for v in recent_form.values() if not v["games"])
    print(f"  Pool: {len(players)} players from depth charts "
          f"({rookies} with no {season} stats)")
    if gaps:
        print(f"  [!] {len(gaps)} team/position gaps: {', '.join(gaps[:10])}")
    cache_save(cache_key, {"players": players, "recent_form": recent_form})
    # Older pool snapshots are dead weight (and now get committed to the repo)
    try:
        import glob
        for old in glob.glob(os.path.join(CACHE_FOLDER, "pool_depth_*.json")):
            if os.path.basename(old) != f"{cache_key}.json":
                os.remove(old)
    except Exception:
        pass
    return players, recent_form


def build_player_pool(offense_stats, nv=None, season=STAT_SEASON, refresh=False):
    """
    Build the player pool live from ESPN season leaders.

    Pulls the top producers in passing / rushing / receiving yards for the given
    season, resolves each athlete to name + position + CURRENT team (so offseason
    moves are reflected), and returns:
        players     -> list of player dicts ready for scoring
        recent_form -> name -> per-game production dict

    Season totals are used as the form baseline. Once the season is underway this
    becomes true last-3-games data; in week 1 the prior season is the best signal.
    """
    cache_key = f"players_{season}"
    if not refresh:
        cached = cache_load(cache_key)
        if cached:
            print(f"  Loaded {len(cached['players'])} players from cache")
            return cached["players"], cached["recent_form"]

    print(f"  Pulling {season} leaders for all 32 teams from ESPN...")
    players     = []
    recent_form = {}
    seen        = set()          # athlete ids already added
    counts      = {}             # (team, position) -> how many kept

    for abbr, tid in TEAM_IDS.items():
        try:
            data = get_json(
                f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
                f"seasons/{season}/types/2/teams/{tid}/leaders")
            cats = {c.get("name"): c for c in data.get("categories", [])}

            for cat_name, label, positions in LEADER_CATEGORIES:
                cat = cats.get(cat_name)
                if not cat:
                    continue
                for entry in cat.get("leaders", [])[:LEADER_SCAN_DEPTH]:
                    ref = entry.get("athlete", {}).get("$ref", "")
                    pid = ref.rstrip("/").split("/")[-1].split("?")[0]
                    total = entry.get("value", 0) or 0
                    if not pid.isdigit() or pid in seen or total <= 0:
                        continue

                    try:
                        a = get_json("https://site.api.espn.com/apis/common/v3/sports/"
                                     f"football/nfl/athletes/{pid}",
                                     tries=2, timeout=10).get("athlete", {})
                    except Exception:
                        continue

                    name = a.get("displayName")
                    pos  = (a.get("position") or {}).get("abbreviation", "")
                    # Use the player's CURRENT team, not the team whose leaderboard
                    # he appeared on, so offseason moves land on the right roster.
                    team = (a.get("team") or {}).get("abbreviation", "")
                    if not name or not team or pos not in positions:
                        continue
                    # ESPN keeps a retired player's last team, so without this a
                    # 44-year-old Philip Rivers lands on the Week 1 board off the
                    # three games he played in 2025.
                    if a.get("active") is False:
                        continue

                    key = (team, pos)
                    if counts.get(key, 0) >= TEAM_POOL_LIMITS.get(pos, 3):
                        continue

                    seen.add(pid)
                    counts[key] = counts.get(key, 0) + 1

                    stats = _athlete_season_stats(pid, season)
                    games = stats.get("gamesPlayed") or 17
                    avg   = round(total / games, 1)

                    player = {
                        "name":     name,
                        "position": pos,
                        "team":     team,
                        "espn_id":  pid,
                    }
                    player.update(_usage(pos, stats, team, offense_stats))
                    # Real snap share from nflverse -- ESPN has no snap data,
                    # and a 48% snap back reads very differently from an 85% one.
                    snap = lookup_snap(name, team, nv) if nv else None
                    if snap:
                        player["snap_pct"] = snap["snap_pct"]
                    players.append(player)

                    recent_form[name] = {
                        "stat_label":   label,
                        "trend":        _form_trend(pos, avg),
                        "last3":        [round(avg), round(avg), round(avg)],
                        "avg3":         avg,
                        "season_total": round(total),
                        "games":        int(games),
                    }
                    time.sleep(DELAY)
        except Exception as e:
            print(f"  [!] Team leaders {abbr}: {e}")

    gaps = [f"{t} {p}" for t in TEAM_IDS for p in ("QB", "RB", "WR", "TE")
            if counts.get((t, p), 0) == 0]
    print(f"  Built pool of {len(players)} players across 32 teams")
    if gaps:
        print(f"  [!] {len(gaps)} team/position gaps remain: {', '.join(gaps[:12])}"
              + (" ..." if len(gaps) > 12 else ""))
    cache_save(cache_key, {"players": players, "recent_form": recent_form})
    return players, recent_form


def _athlete_season_stats(pid, season):
    """
    Flatten an athlete's season stat lines into one {stat_name: value} dict.
    Categories overlap on gamesPlayed, so the max wins. Returns {} on failure.
    """
    out = {}
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/common/v3/sports/football/nfl/"
            f"athletes/{pid}/stats",
            params={"season": season, "seasontype": 2}, timeout=8,
        )
        for cat in r.json().get("categories", []):
            names = cat.get("names", [])
            for row in cat.get("statistics", []):
                if row.get("season", {}).get("year") != season:
                    continue
                for k, v in zip(names, row.get("stats", [])):
                    try:
                        val = float(str(v).replace(",", ""))
                    except (ValueError, TypeError):
                        continue
                    if k == "gamesPlayed":
                        out[k] = max(out.get(k, 0), val)
                    else:
                        out[k] = val
    except Exception:
        pass
    return out


def _usage(pos, stats, team, offense_stats):
    """
    Real usage from ESPN counting stats.

    ESPN's free API has no snap counts, so instead of a fake snap % we report
    the player's share of his team's opportunity plus his per-game volume:
        QB     -> share of team pass attempts, attempts/gm
        RB     -> share of team carries,       touches/gm (carries + receptions)
        WR/TE  -> share of team targets,       targets/gm
    """
    tm    = offense_stats.get(team, {})
    games = stats.get("gamesPlayed") or 17

    if pos == "QB":
        own, denom, vol_lbl = stats.get("passingAttempts", 0), tm.get("pass_att", 0), "att/gm"
        share_lbl = "of team pass att"
    elif pos == "RB":
        own, denom, vol_lbl = stats.get("rushingAttempts", 0), tm.get("rush_att", 0), "touch/gm"
        share_lbl = "carry share"
        own_vol = own + stats.get("receptions", 0)
    else:
        own, denom, vol_lbl = stats.get("receivingTargets", 0), tm.get("targets", 0), "tgt/gm"
        share_lbl = "target share"

    if pos != "RB":
        own_vol = own

    share = round(own / denom, 4) if denom else 0
    return {
        "usage_share":     min(share, 1.0),
        "usage_label":     share_lbl,
        "usage_vol":       round(own_vol / games, 1) if games else 0,
        "usage_vol_label": vol_lbl,
    }


def _form_trend(pos, avg):
    """Classify per-game production into Hot / Neutral / Cold."""
    if avg <= 0:
        return "Cold"
    if pos == "QB":
        return "Hot" if avg > 250 else "Cold" if avg < 190 else "Neutral"
    if pos == "RB":
        return "Hot" if avg > 75 else "Cold" if avg < 45 else "Neutral"
    return "Hot" if avg > 70 else "Cold" if avg < 40 else "Neutral"


def rank_defenses(defense):
    """Rank all 32 defenses 1 (toughest) to 32 (softest) on each stat.
    Called again after nflverse merges in real per-position numbers, otherwise
    the ranks shown on the cards would still describe the old estimates."""
    for stat_key, rank_key in (("pass_ypg", "rank_pass"), ("rush_ypg", "rank_rush"),
                               ("wr_ypg", "rank_wr"), ("te_ypg", "rank_te")):
        vals = sorted(((abbr, d.get(stat_key, 220)) for abbr, d in defense.items()),
                      key=lambda x: x[1])
        for rank, (abbr, _) in enumerate(vals, 1):
            defense[abbr][rank_key] = rank


def _team_defensive_stats(tid, season):
    """
    Sacks / interceptions / fumble recoveries / defensive TDs for one team.
    Returns ({stat: value}, games_played). ESPN's own `pointsAllowed` field is
    always 0, so points allowed is derived from opponent scores instead.
    """
    out, gp = {}, 17
    try:
        r = requests.get(
            f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
            f"seasons/{season}/types/2/teams/{tid}/statistics", timeout=10,
        )
        for cat in r.json().get("splits", {}).get("categories", []):
            if cat.get("name") not in ("defensive", "defensiveInterceptions", "general"):
                continue
            for st in cat.get("stats", []):
                try:
                    out[st["name"]] = float(str(st.get("value", 0)).replace(",", ""))
                except (ValueError, TypeError):
                    pass
        gp = int(out.get("teamGamesPlayed") or 17) or 17
        time.sleep(DELAY)
    except Exception as e:
        print(f"  [!] Defensive stats {tid}: {e}")
    return out, gp


def get_all_team_stats(season=STAT_SEASON, refresh=False):
    """
    Pull offense + defense stats for all 32 teams from ESPN boxscores.
    Returns (defense_stats, offense_stats) dicts keyed by team abbreviation.
    """
    cache_key = f"teams_{season}"
    if not refresh:
        cached = cache_load(cache_key)
        if cached:
            print(f"  Loaded team stats for {len(cached['offense'])} teams from cache")
            return cached["defense"], cached["offense"]

    print(f"  Fetching {season} team stats from ESPN (all 32 teams)...")
    defense = {}
    offense = {}

    for abbr, tid in TEAM_IDS.items():
        try:
            r = requests.get(
                f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}/types/2/teams/{tid}/statistics",
                timeout=8,
            )
            cats = r.json().get("splits", {}).get("categories", [])
            raw = {}
            for cat in cats:
                for s in cat.get("stats", []):
                    try:
                        raw[s["name"]] = float(str(s.get("value", 0)).replace(",",""))
                    except (ValueError, TypeError):
                        pass

            games = raw.get("teamGamesPlayed", 17) or 17

            pass_ypg  = round(raw.get("netPassingYardsPerGame", 220), 1)
            rush_ypg  = round(raw.get("rushingYardsPerGame",   110), 1)
            # Denominators for player usage share
            tm_targets  = raw.get("receivingTargets", 0)
            tm_rush_att = raw.get("rushingAttempts",  0)
            tm_pass_att = raw.get("passingAttempts",  0)
            plays_pg  = round(raw.get("totalOffensivePlays", 63*games) / games, 1)
            ppg       = round(raw.get("totalPointsPerGame",   22), 1)
            rz_pct    = round(raw.get("redZonePct", 0.55) * 100)

            pace = "Fast" if plays_pg >= 67 else "Slow" if plays_pg <= 60 else "Moderate"

            offense[abbr] = {
                "ppg":      ppg,
                "rz_pct":   rz_pct,
                "pass_ypg": pass_ypg,
                "rush_ypg": rush_ypg,
                "plays_pg": plays_pg,
                "pace":     pace,
                "targets":  tm_targets,
                "rush_att": tm_rush_att,
                "pass_att": tm_pass_att,
                "games":    int(games),
                # Giveaways drive DST scoring, so track what this offense coughs up
                "giveaways_pg": round((raw.get("interceptions", 12)
                                       + raw.get("fumblesLost", 9)) / games, 2),
                "sacks_allowed_pg": round(raw.get("sacks", 40) / games, 2),
            }
            time.sleep(DELAY)
        except Exception as e:
            print(f"  [!] Team stats {abbr}: {e}")

    # Build defense stats by pulling opponent stats from each team's schedule
    print("  Building defense allowed stats from schedule...")
    for abbr, tid in TEAM_IDS.items():
        dstat, gp = _team_defensive_stats(tid, season)
        try:
            r = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{tid}/schedule",
                params={"season": season, "seasontype": 2}, timeout=8,
            )
            events = r.json().get("events", [])
            pass_allowed = []
            rush_allowed = []
            plays_list   = []
            pts_allowed  = []

            for ev in events:
                eid = ev.get("id")
                if not eid:
                    continue
                # Opponent's final score = points this defense gave up. The
                # schedule already carries it, so this costs no extra request.
                try:
                    for comp in ev["competitions"][0].get("competitors", []):
                        if comp.get("team", {}).get("abbreviation") != abbr:
                            sc = comp.get("score")
                            sc = sc.get("value", sc.get("displayValue")) if isinstance(sc, dict) else sc
                            pts_allowed.append(float(sc))
                except Exception:
                    pass
                try:
                    bs = requests.get(
                        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary",
                        params={"event": eid}, timeout=8,
                    ).json()
                    teams_bs = bs.get("boxscore", {}).get("teams", [])
                    for t in teams_bs:
                        if t.get("team", {}).get("abbreviation", "") != abbr:
                            # This is the opponent — their yards = what our defense allowed
                            for s in t.get("statistics", []):
                                n = s.get("name", "")
                                v = s.get("displayValue", "0").replace(",","")
                                try:
                                    val = float(v)
                                except ValueError:
                                    continue
                                if n == "netPassingYards":
                                    pass_allowed.append(val)
                                elif n == "rushingYards":
                                    rush_allowed.append(val)
                                elif n == "totalOffensivePlays":
                                    plays_list.append(val)
                    time.sleep(0.15)
                except Exception:
                    pass

            if pass_allowed:
                p_avg = round(sum(pass_allowed) / len(pass_allowed), 1)
                r_avg = round(sum(rush_allowed) / len(rush_allowed), 1) if rush_allowed else 110.0
                pl_avg = round(sum(plays_list) / len(plays_list), 1) if plays_list else 63.0
                pace  = "Fast" if pl_avg >= 67 else "Slow" if pl_avg <= 60 else "Moderate"
                defense[abbr] = {
                    "pass_ypg":  p_avg,
                    "rush_ypg":  r_avg,
                    "wr_ypg":    round(p_avg * 0.62, 1),   # ~62% of pass yds go to WRs
                    "te_ypg":    round(p_avg * 0.20, 1),   # ~20% to TEs
                    "rb_rec_ypg":round(p_avg * 0.18, 1),   # ~18% to RBs in pass game
                    "plays_pg":  pl_avg,
                    "pace":      pace,
                    "pts_allowed_pg": round(sum(pts_allowed)/len(pts_allowed), 1) if pts_allowed else 22.0,
                    "sacks_pg":     round(dstat.get("sacks", 34) / gp, 2),
                    "takeaways_pg": round((dstat.get("interceptions", 12)
                                           + dstat.get("fumblesRecovered", 8)) / gp, 2),
                    "def_tds":      int(dstat.get("defensiveTouchdowns", 0)),
                }
        except Exception as e:
            print(f"  [!] Defense schedule {abbr}: {e}")

    rank_defenses(defense)

    print(f"  Defense stats built for {len(defense)} teams, offense for {len(offense)} teams")
    cache_save(cache_key, {"defense": defense, "offense": offense})
    return defense, offense


# ============================================================
#  MATCHUP SCORING ENGINE
# ============================================================

# Yards allowed thresholds per position — what counts as "soft" vs "tough"
# Fallback scale only. These are replaced at runtime by calibrate_thresholds()
# using the real league distribution -- hardcoded cutoffs drift badly year to
# year and silently pin most of the league at 0.0 when they do.
SOFT = {"QB": 260, "RB": 130, "WR": 175, "TE": 60}
TOUGH = {"QB": 210, "RB": 100, "WR": 140, "TE": 45}


EPA_BAND = {}   # pos -> (best_for_defense_epa, worst) -- filled at runtime


def merge_nflverse(defense_stats, offense_stats, nv):
    """Fold nflverse EPA onto the ESPN team stats, keyed by ESPN abbreviation."""
    for t, v in nv.get("off", {}).items():
        if t in offense_stats:
            offense_stats[t].update(v)
    for t, v in nv.get("def", {}).items():
        if t in defense_stats:
            defense_stats[t].update(v)
    # Real per-position yards just replaced the estimates, so the ranks that
    # go on the cards have to be rebuilt from the new values.
    rank_defenses(defense_stats)


def calibrate_epa(defense_stats):
    """
    Percentile-anchor the EPA-allowed scale per position, same approach used for
    yards. EPA allowed is negative-is-good, so the band runs from the 10th
    percentile (stingiest) to the 90th (most generous).
    """
    EPA_BAND.clear()
    for pos, key in (("QB", "pass_epa_allowed"), ("WR", "pass_epa_allowed"),
                     ("TE", "pass_epa_allowed"), ("RB", "rush_epa_allowed")):
        vals = sorted(d[key] for d in defense_stats.values() if key in d)
        if len(vals) < 8:
            continue
        pick = lambda q: vals[min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))]
        lo, hi = pick(0.10), pick(0.90)
        if hi - lo > 0.001:
            EPA_BAND[pos] = (lo, hi)
    if EPA_BAND:
        print("  EPA scale calibrated: " + ", ".join(
            f"{p} {lo:+.3f}/{hi:+.3f}" for p, (lo, hi) in EPA_BAND.items()))


def epa_matchup_score(pos, d):
    """0-10 from EPA allowed (higher = softer defense). None if unavailable."""
    band = EPA_BAND.get(pos)
    key  = "rush_epa_allowed" if pos == "RB" else "pass_epa_allowed"
    if not band or key not in d:
        return None
    lo, hi = band
    return round(min(10, max(0, (d[key] - lo) / (hi - lo) * 10)), 1)


def calibrate_thresholds(defense_stats):
    """
    Rebuild the matchup scale from this season's actual yards-allowed spread.

    The 0-10 matchup score is 35% of the composite -- the single largest weight --
    so if the endpoints don't match reality the score degenerates. Anchoring to
    the 10th/90th percentile keeps the scale spread across the real league every
    season, instead of dumping half the teams onto 0.0.
    """
    if not defense_stats:
        return
    for pos, key in YPG_KEY.items():
        vals = sorted(d[key] for d in defense_stats.values() if key in d)
        if len(vals) < 8:
            continue
        p = lambda q: vals[min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))]
        tough, soft = p(0.10), p(0.90)
        if soft - tough < 1:          # degenerate spread; keep the fallback
            continue
        TOUGH[pos], SOFT[pos] = round(tough, 1), round(soft, 1)
    print("  Matchup scale calibrated: " +
          ", ".join(f"{p} {TOUGH[p]}-{SOFT[p]}" for p in ("QB", "RB", "WR", "TE")))
YPG_KEY = {"QB": "pass_ypg", "WR": "wr_ypg", "TE": "te_ypg", "RB": "rush_ypg"}
RANK_KEY = {"QB": "rank_pass", "WR": "rank_wr", "TE": "rank_te", "RB": "rank_rush"}


def grade_for(composite):
    """Shared grade ladder -- keeps DEF and skill positions on one scale."""
    if   composite >= 7.5: return "A+", "#00e676", "Elite Spot"
    elif composite >= 6.5: return "A",  "#69f0ae", "Strong Play"
    elif composite >= 5.5: return "B+", "#b9f6ca", "Solid Play"
    elif composite >= 4.5: return "B",  "#fff176", "Decent Option"
    elif composite >= 3.5: return "C",  "#ffb74d", "Risky"
    else:                  return "D",  "#ff5252", "Fade"


def defense_grade(team, game, defense_stats, offense_stats, vegas_totals,
                  game_totals, weather):
    """
    Grade a team DEFENSE the way a DST is actually evaluated: mostly by who it
    is playing. A good unit against a good offense is a worse spot than an
    average unit against a turnover-prone one, so opponent weakness carries the
    most weight -- same logic as the skill-position matchup score.
    """
    d   = defense_stats.get(team, {})
    opp = game["away_abbr"] if game["home_abbr"] == team else game["home_abbr"]
    o   = offense_stats.get(opp, {})
    is_home = game["home_abbr"] == team

    # --- Opponent weakness (the biggest lever) ---
    opp_ppg   = o.get("ppg", 22.0)
    # 30 ppg -> 0, 14 ppg -> 10
    opp_score = max(0, min(10, (30 - opp_ppg) / 16 * 10))
    # Blend in the opponent's offensive EPA/play. PPG is noisy (a defensive TD
    # or a short field inflates it); EPA measures the offense itself.
    opp_epa = o.get("epa_play")
    if opp_epa is not None:
        # +0.15 EPA/play is elite offense -> 0 for the D; -0.15 is dreadful -> 10
        epa_side  = max(0, min(10, (0.15 - opp_epa) / 0.30 * 10))
        opp_score = opp_score * 0.5 + epa_side * 0.5

    # --- Turnovers: what the opponent gives up x what this defense takes ---
    give     = o.get("giveaways_pg", 1.3)
    take     = d.get("takeaways_pg", 1.2)
    to_score = max(0, min(10, ((give / 1.3) * 0.55 + (take / 1.3) * 0.45) * 5))

    # --- Pass rush vs an offense that allows sacks ---
    sacks    = d.get("sacks_pg", 2.2)
    allowed  = o.get("sacks_allowed_pg", 2.4)
    rush_sc  = max(0, min(10, ((sacks / 2.4) * 0.6 + (allowed / 2.4) * 0.4) * 5))

    # --- This unit's own quality ---
    pa       = d.get("pts_allowed_pg", 22.0)
    own_sc   = max(0, min(10, (30 - pa) / 16 * 10))

    # --- Vegas: a low implied total for the opponent is the cleanest signal ---
    g_total  = game_totals.get(game["game_id"], 44.0)
    spread   = vegas_totals.get(game["game_id"], 0)
    opp_impl = g_total / 2 - (spread / 2 if is_home else -spread / 2)
    vegas_sc = max(0, min(10, (28 - opp_impl) / 14 * 10))

    # --- Weather: wind and cold suppress offenses, which helps a defense ---
    wx    = weather.get(game["game_id"], {})
    wind  = wx.get("wind_mph", 0)
    bonus = 0.4 if (wind >= 15 and not game.get("indoor")) else 0.0

    composite = round(min(10, (
        opp_score * 0.30 +
        vegas_sc  * 0.24 +
        to_score  * 0.18 +
        own_sc    * 0.16 +
        rush_sc   * 0.12
    ) + bonus + (0.2 if (is_home and not game.get("neutral")) else 0)), 1)

    grade, color, label = grade_for(composite)

    notes = [f"{opp} offense: {opp_ppg:.1f} PPG"
             + (f", {opp_epa:+.3f} EPA/play" if opp_epa is not None else "")
             + f", {give:.2f} giveaways/gm",
             f"{team} D: {pa:.1f} pts allowed/gm, {sacks:.2f} sacks/gm, {take:.2f} takeaways/gm",
             f"{opp} implied {opp_impl:.1f} pts"]
    if bonus:
        notes.append(f"Wind {wind} mph -- passing suppressed")
    if d.get("def_tds"):
        notes.append(f"{d['def_tds']} defensive TDs in {STAT_SEASON}")

    return {
        "name":       f"{team} Defense",
        "position":   "DEF",
        "team":       team,
        "opp":        opp,
        "is_home":    is_home,
        "game":       game,
        "composite":  composite,
        "grade":      grade,
        "grade_color": color,
        "label":      label,
        "scout":      " | ".join(notes),
        "opp_score":  round(opp_score, 1),
        "vegas_sc":   round(vegas_sc, 1),
        "to_score":   round(to_score, 1),
        "own_sc":     round(own_sc, 1),
        "rush_sc":    round(rush_sc, 1),
        "opp_ppg":    opp_ppg,
        "opp_epa":    opp_epa,
        "opp_impl":   round(opp_impl, 1),
        "pts_allowed": pa,
        "sacks_pg":   sacks,
        "takeaways_pg": take,
        "giveaways":  give,
        "def_tds":    d.get("def_tds", 0),
        "wind_mph":   wind,
        "indoor":     game.get("indoor", False),
        "neutral":    game.get("neutral", False),
    }


def calibrate_defense_scores(scored):
    """
    Stretch DEF composites across the grade ladder.

    The DEF score blends five sub-scores, and averaging pulls everything toward
    the middle -- raw output spanned only 3.1-6.4, so no defense could ever earn
    an A no matter how good the spot was. This maps the slate's 10th/90th
    percentile onto 3.0/8.0, the same percentile-anchoring used for the skill
    matchup scale, so the spread reflects the real range each week.
    """
    defs = [p for p in scored if p["position"] == "DEF"]
    if len(defs) < 8:
        return
    vals = sorted(p["composite"] for p in defs)
    pick = lambda q: vals[min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))]
    lo, hi = pick(0.10), pick(0.90)
    if hi - lo < 0.5:
        return
    for p in defs:
        stretched = 3.0 + (p["composite"] - lo) / (hi - lo) * 5.0
        p["composite"] = round(max(0.0, min(10.0, stretched)), 1)
        p["grade"], p["grade_color"], p["label"] = grade_for(p["composite"])
    print(f"  DEF scale calibrated: raw {lo:.1f}-{hi:.1f} -> 3.0-8.0")


# A "full" role at each position -- this much of the team's work scores 10/10.
# RB 55% of carries, WR 28% of targets, TE 22% of targets, all from 2025 starters.
ROLE_FULL   = {"RB": 0.55, "WR": 0.28, "TE": 0.22}
# Depth-chart fallback when there is no usage history (rookies, new arrivals)
ROLE_BY_DEPTH = {"RB": {1: 6.0, 2: 3.5, 3: 1.5}, "WR": {1: 6.0, 2: 4.5, 3: 3.0, 4: 1.5},
                 "TE": {1: 6.0, 2: 2.0}}
# Fitted on Week 1 2026. Rank correlation of grade vs actual yards, and the
# average yards of the top-5 graded players per position:
#   weight 0.00 -> corr 0.137, top-5 34.7 yds   (the grade was nearly noise)
#   weight 0.15 -> corr 0.354, top-5 50.5
#   weight 0.20 -> corr 0.422, top-5 54.5       <- chosen: top picks peak here
#   weight 0.30 -> corr 0.522, top-5 48.4       (becomes a volume list)
ROLE_WEIGHT = 0.20


def role_score(player):
    """
    0-10: how much of his team's work this player actually gets.

    The composite was ~80% team situation, so a 9%-carry backup in a shootout
    graded like the lead back beside him (Emari Demercado, #8 RB league-wide),
    and a 0%-usage WR3 sat #4 among receivers. Volume is the strongest single
    predictor of production and it was carrying no weight at all. Measured
    usage share beats depth-chart rank -- Houston's listed RB2 (Marks, 41%)
    out-touches its listed RB1 (Montgomery, 33%).
    """
    pos = player["position"]
    if pos == "QB":
        return 10.0 if player.get("qb_starter") is not False else 0.0
    full = ROLE_FULL.get(pos, 0.3)
    usage = player.get("usage_share") or 0.0
    snap  = player.get("snap_pct")
    if usage > 0:
        u_sc = min(10.0, usage / full * 10.0)
        if snap:                                   # blend in real snap share
            return round(0.65 * u_sc + 0.35 * min(10.0, snap / 85.0 * 10.0), 1)
        return round(u_sc, 1)
    # No history at all: lean on where the depth chart puts him
    return ROLE_BY_DEPTH.get(pos, {}).get(player.get("depth_rank") or 9, 1.0)


def matchup_grade(player, game, defense_stats, offense_stats, vegas_totals, game_totals,
                  injuries, weather, props, recent_form):
    """
    Score a player's matchup this week. Returns a full report dict.
    """
    pos      = player["position"]
    team     = player["team"]
    is_home  = game["home_abbr"] == team
    opp      = game["away_abbr"] if is_home else game["home_abbr"]
    indoor   = game.get("indoor", False)

    d      = defense_stats.get(opp, {})
    off    = offense_stats.get(team, {})
    team_total = vegas_totals.get(team, 24.0)
    g_total    = game_totals.get(team, 48.0)
    # Offensive pace = how fast the player's own team plays
    off_pace     = off.get("pace", "Moderate")
    off_plays_pg = off.get("plays_pg", 63)
    # Defensive pace = how fast the opponent plays (affects total possessions)
    opp_pace     = d.get("pace", "Moderate")
    opp_plays_pg = d.get("plays_pg", 63)
    # Combined pace score uses the average plays per game
    avg_plays_pg = round((off_plays_pg + opp_plays_pg) / 2, 1)

    # ── Yards allowed to this position ──────────────────────
    ypg_key  = YPG_KEY.get(pos, "pass_ypg")
    rank_key = RANK_KEY.get(pos, "rank_pass")
    ypg_allowed = d.get(ypg_key, 220)
    def_rank    = d.get(rank_key, 16)    # 1=toughest, 32=softest

    # ── Matchup score (0-10): higher = softer defense ───────
    soft_threshold  = SOFT.get(pos, 220)
    tough_threshold = TOUGH.get(pos, 180)
    matchup_raw = (ypg_allowed - tough_threshold) / (soft_threshold - tough_threshold)
    yards_score = round(min(10, max(0, matchup_raw * 10)), 1)

    # Blend in EPA allowed. Yards alone can't see sacks, turnovers or efficiency
    # -- a defense can bleed yards while still winning downs. EPA does see that,
    # so it gets 40% of the matchup signal when nflverse data is available.
    epa_score = epa_matchup_score(pos, d)
    if epa_score is None:
        matchup_score = yards_score
    else:
        matchup_score = round(yards_score * 0.60 + epa_score * 0.40, 1)

    # ── Scoring environment (0-10) ───────────────────────────
    # High team total + fast pace = more opportunity
    env_score = round(min(10, (team_total / 35.0) * 10), 1)

    # ── Game pace score (0-10) — based on combined plays/gm ──
    pace_score = round(min(10, (avg_plays_pg / 72.0) * 10), 1)

    # ── Offense quality (0-10) ───────────────────────────────
    ppg = off.get("ppg", 22)
    off_score = round(min(10, (ppg / 35.0) * 10), 1)

    # ── Recent form ──────────────────────────────────────────
    form        = recent_form.get(player["name"], {})
    form_avg    = form.get("avg3", 0)
    form_trend  = form.get("trend", "Neutral")
    form_last3  = form.get("last3", [])
    form_total  = form.get("season_total", 0)
    form_games  = form.get("games", 0)
    form_label  = form.get("stat_label", "Yds")
    # Ceilings for raw yards by position
    form_ceiling = {"QB": 350, "RB": 130, "WR": 130, "TE": 90}.get(pos, 130)
    if form_games:
        recent_score = round(min(10, (form_avg / form_ceiling) * 10), 1)
    else:
        # A rookie starter has no prior production, which is not evidence of
        # being bad. Scoring him 0 on form would bury the actual RB1 beneath
        # his own backups, so unknown gets a neutral mark instead of a zero.
        recent_score = 5.0

    # ── Usage (share of team opportunity + per-game volume) ──
    usage_share     = player.get("usage_share", 0)
    usage_label     = player.get("usage_label", "usage")
    usage_vol       = player.get("usage_vol", 0)
    usage_vol_label = player.get("usage_vol_label", "")

    # ── Weather ──────────────────────────────────────────────
    wx         = weather.get(game["game_id"], {})
    wind_mph   = wx.get("wind_mph", 0)
    temp_f     = wx.get("temp_f", 65)
    precip_mm  = wx.get("precip_mm", 0)
    wx_impact  = wx.get("impact", "Good conditions")
    wx_cond    = wx.get("condition", "Clear")

    # ── Wind penalty for passing positions ───────────────────
    wind_penalty = 0
    if not indoor and pos in ("QB", "WR", "TE"):
        if wind_mph >= 20:   wind_penalty = 1.2
        elif wind_mph >= 15: wind_penalty = 0.5

    # ── Vegas player prop ─────────────────────────────────────
    player_props = props_for(player, props)

    # ── Home field ───────────────────────────────────────────
    # A neutral site (London, Melbourne, Munich...) has no home crowd, so the
    # nominal home team gets the same neutral mark as the visitor.
    if game.get("neutral"):
        home_score = 5.5
    else:
        home_score = 6.5 if is_home else 4.5

    # ── Weighted composite ───────────────────────────────────
    # Role/usage gets ROLE_WEIGHT; the original six factors share the rest in
    # their old proportions, so their relative importance is unchanged.
    rs   = role_score(player)
    rest = 1.0 - ROLE_WEIGHT
    composite = round(
        rs            * ROLE_WEIGHT +
        matchup_score * 0.35 * rest +
        env_score     * 0.20 * rest +
        recent_score  * 0.20 * rest +
        pace_score    * 0.12 * rest +
        off_score     * 0.08 * rest +
        home_score    * 0.05 * rest,
        2
    )
    composite = max(0, round(composite - wind_penalty, 2))

    # ── Injury ───────────────────────────────────────────────
    inj       = injuries.get(player["name"], {})
    inj_status = inj.get("status", "Active")
    inj_detail = inj.get("detail", "")
    penalties = {"Questionable": 0.4, "Doubtful": 1.2, "Out": 99, "IR": 99, "PUP": 99}
    composite  = max(0, round(composite - penalties.get(inj_status, 0), 2))

    # ── Grade ────────────────────────────────────────────────
    if inj_status in ("Out", "IR", "PUP"):
        grade, label, color = "OUT", "OUT", "#ff4444"
    else:
        grade, color, label = grade_for(composite)

    # ── Scout notes ──────────────────────────────────────────
    notes = []
    if def_rank >= 25:
        notes.append(f"{opp} D ranked #{def_rank} in {STAT_SEASON} -- allowed {ypg_allowed} YPG to {pos}s")
    elif def_rank <= 8:
        notes.append(f"Tough: {opp} D ranked #{def_rank} vs {pos} in {STAT_SEASON}")
    if team_total >= 27:
        notes.append(f"High-scoring game (total {g_total:.0f}, {team} implied {team_total:.1f})")
    elif team_total <= 18:
        notes.append(f"Low-scoring environment (total {g_total:.0f})")
    notes.append(f"{STAT_SEASON} pace -- {team} offense {off_pace} ({off_plays_pg} plays/gm), {opp} defense {opp_pace} ({opp_plays_pg} plays/gm)")
    if form_trend == "Hot":
        notes.append(f"Productive -- {form_avg:.1f} {form_label} in {STAT_SEASON}")
    elif form_trend == "Cold":
        notes.append(f"Low volume -- {form_avg:.1f} {form_label} in {STAT_SEASON}")
    if not indoor and wind_mph >= 15:
        notes.append(f"Wind {wind_mph} mph -- passing game affected")
    elif not indoor:
        notes.append(f"Outdoor: {temp_f}F, wind {wind_mph} mph")
    else:
        notes.append("Indoor -- no weather impact")

    return {
        **player,
        "game":         game,
        "opp":          opp,
        "is_home":      is_home,
        "composite":    composite,
        "grade":        grade,
        "label":        label,
        "grade_color":  color,
        "matchup_score": matchup_score,
        "role_score":    rs,
        "yards_score":   yards_score,
        "epa_score":     epa_score,
        "epa_allowed":   d.get("rush_epa_allowed" if pos == "RB" else "pass_epa_allowed"),
        "env_score":    env_score,
        "pace_score":   pace_score,
        "off_score":    off_score,
        "ypg_allowed":  ypg_allowed,
        "def_rank":     def_rank,
        "team_total":   team_total,
        "game_total":   g_total,
        "off_pace":     off_pace,
        "off_plays_pg": off_plays_pg,
        "opp_pace":     opp_pace,
        "opp_plays_pg": opp_plays_pg,
        "avg_plays_pg": avg_plays_pg,
        "indoor":       indoor,
        "inj_status":   inj_status,
        "inj_detail":   inj_detail,
        "scout":        " | ".join(notes),
        "off_ppg":      ppg,
        "rz_pct":       off.get("rz_pct", 60),
        "form_avg":     form_avg,
        "form_trend":   form_trend,
        "form_last3":   form_last3,
        "form_total":   form_total,
        "form_games":   form_games,
        "depth_rank":   player.get("depth_rank"),
        "qb_starter":   player.get("qb_starter"),
        "neutral":      game.get("neutral", False),
        "rush_ypg":     player.get("rush_ypg", 0.0),
        "rush_tds":     player.get("rush_tds", 0),
        "pass_tds":     player.get("pass_tds", 0),
        "rec_tds":      player.get("rec_tds", 0),
        "form_label":   form_label,
        "form_score":   recent_score,
        "usage_share":     usage_share,
        "usage_label":     usage_label,
        "usage_vol":       usage_vol,
        "usage_vol_label": usage_vol_label,
        "wind_mph":     wind_mph,
        "temp_f":       temp_f,
        "precip_mm":    precip_mm,
        "wx_impact":    wx_impact,
        "wx_cond":      wx_cond,
        "props":        player_props,
    }


# ============================================================
#  HTML DASHBOARD
# ============================================================

LEAGUE_AVG_PLAYS   = 63.0    # plays per game

# How much of the situational adjustment to keep. 1.0 = the original full
# swing, 0.0 = ignore situation entirely and project the season baseline.
# Fitted on Week 1 2026 (284 out-of-sample players). MAE by shrink:
#   1.0 -> 31.2   0.75 -> 30.2   0.5 -> 29.4   0.4 -> 29.2   0.0 -> 28.7
# Gains flatten below 0.4, and going to 0 would claim matchups do not affect
# production at all -- too strong a conclusion from one week. Revisit after
# several weeks; a residual tier bias (~+10% A+ / -15% C at shrink 0) is mean
# regression in the BASELINE, not the multipliers, and needs a separate fix.
PROJ_SHRINK = 0.3

# Baseline regression strength: w = games / (games + REGRESS_K), i.e. K is
# "games worth of prior". Fitted JOINTLY with PROJ_SHRINK on Week 1 2026:
#   shrink .4, K 0  -> MAE 29.2, A+ +23%, C -24%   (what was live)
#   shrink .3, K 4  -> MAE 28.9, A+ +14%, C -17%   (chosen: better on both)
#   shrink .3, K 12 -> MAE 29.7, A+  +7%, C -13%   (best calibration, worse MAE)
# Regression trades average error for calibration at the extremes; K=4 is the
# point that improves both. POS_BASELINE_MEAN is computed at runtime from the
# pool (starters with real history) so it tracks the actual league.
REGRESS_K = 4.0
POS_BASELINE_MEAN = {}


def mark_qb_starters(pool, injuries):
    """
    Flag each team's one effective starting QB -- the lowest depth rank who is
    not ruled out. Quarterback is winner-take-all: a QB2 does not "rotate in",
    he takes zero snaps unless the starter goes down. Week 1 projected fourteen
    backups at 75-257 yards (Flacco 257, Winston 241); all fourteen produced 0.
    """
    by_team = {}
    for p in pool:
        if p["position"] != "QB":
            continue
        if (injuries.get(p["name"], {}).get("status") in UNAVAILABLE):
            p["qb_starter"] = False
            continue
        by_team.setdefault(p["team"], []).append(p)
    for team, qbs in by_team.items():
        qbs.sort(key=lambda x: x.get("depth_rank") or 9)
        for i, q in enumerate(qbs):
            q["qb_starter"] = (i == 0)


def set_positional_means(pool, recent_form):
    """Per-position mean of the prior-season per-game baseline, starters only
    (depth 1-2), so a backup's tiny number does not drag the anchor down."""
    POS_BASELINE_MEAN.clear()
    buckets = {}
    for p in pool:
        if (p.get("depth_rank") or 9) > 2:
            continue
        f = recent_form.get(p["name"], {})
        if not f.get("games"):
            continue
        base = f.get("avg3", 0) or 0
        if p["position"] == "QB":
            base += p.get("rush_ypg", 0.0)
        if base > 0:
            buckets.setdefault(p["position"], []).append(base)
    for pos, vals in buckets.items():
        POS_BASELINE_MEAN[pos] = round(sum(vals) / len(vals), 1)
    print("  Positional baselines: " + ", ".join(f"{k} {v}" for k, v in sorted(POS_BASELINE_MEAN.items())))
LEAGUE_AVG_IMPLIED = 22.5    # implied team points

# Fallback per-game production for a starter with no prior-season stats, by
# position and depth-chart rank. A rookie RB1 has no history, but projecting
# him at zero would be worse than projecting him at a typical starter's line.
ROOKIE_BASELINE = {
    "QB": {1: 215, 2: 60},
    "RB": {1: 62,  2: 30, 3: 12},
    "WR": {1: 58,  2: 44, 3: 28, 4: 16},
    "TE": {1: 38,  2: 14},
}

# Measured touchdowns per game for an involved player at each position
# (2025 regular season: QB 10+ attempts, RB 5+ carries, WR/TE 2+ targets).
# These replace guessed "share of team TDs" constants that ran the skill
# positions 2-3x too hot -- receivers were being quoted 56-82% chances against
# a real rate of 23.9%.
TD_BASE = {"QB": 1.62, "RB": 0.51, "WR": 0.27, "TE": 0.27}

# Typical usage share at each position, used to scale a player up or down
# from that positional average
TD_USAGE_NORM = {"QB": 1.0, "RB": 0.40, "WR": 0.18, "TE": 0.18}


def project_player(p):
    """
    Project this week's production from the season baseline, adjusted for how
    fast the game should run, how good the matchup is, and how many points the
    team is expected to score.

        projected = baseline x pace x matchup x scoring environment

    Returns projected yards, a touchdown chance, and the inputs behind them.
    """
    pos   = p["position"]
    # A backup QB projects to nothing. He keeps his card (the matchup grade is
    # still informative if he is pressed into duty) but gets no yardage call
    # and stays out of the leaders and the scorecard.
    if pos == "QB" and p.get("qb_starter") is False:
        return None
    base  = p.get("form_avg") or 0.0
    est   = False
    label = (p.get("form_label") or "Yds").replace("/gm", "")

    # A quarterback's rushing yards are production too. Ranking QBs on passing
    # alone undersells a runner badly -- Hurts averaged 201.5 passing but added
    # 26.3 on the ground, so a pass-only number reads him as a lesser producer
    # than pocket passers he is actually competitive with.
    # Applied to every QB, not just the runners, so the column stays one
    # comparable number. A pocket passer's rushing is simply near zero.
    if pos == "QB":
        base += p.get("rush_ypg", 0.0)
        label = "Total Yds"

    gp = p.get("form_games") or 0
    if not gp:
        rank = p.get("depth_rank") or 1
        base = ROOKIE_BASELINE.get(pos, {}).get(rank, 0)
        est  = True
    else:
        # Regress last season's average toward the positional mean, weighted by
        # how many games it rests on: w = g / (g + K). A 17-game baseline keeps
        # ~80% of itself; a 3-game one keeps ~40%. Two reasons this exists:
        #   1. Mean regression is real -- even with situational factors switched
        #      off, Week 1 A+ calls ran +10% over and C calls -15% under.
        #   2. Thin samples lie. Cooper Rush's 70-yard call came from a handful
        #      of 2025 appearances; he threw for 143.
        pos_mean = POS_BASELINE_MEAN.get(pos)
        if pos_mean:
            w    = gp / (gp + REGRESS_K)
            base = w * base + (1 - w) * pos_mean
    if not base:
        return None

    plays     = p.get("avg_plays_pg") or LEAGUE_AVG_PLAYS
    pace_mult = max(0.85, min(1.15, plays / LEAGUE_AVG_PLAYS))
    # A 10/10 matchup is worth about +25%, a 0/10 about -25%
    match_mult = 0.75 + (p.get("matchup_score", 5.0) / 10.0) * 0.50
    implied    = p.get("team_total") or LEAGUE_AVG_IMPLIED
    env_mult   = max(0.80, min(1.25, implied / LEAGUE_AVG_IMPLIED))

    # The three situational factors compounded to a 0.5x-1.8x range, and every
    # sample (2025 backtest, Sunday early slate, full Week 1 at n=285) showed the
    # same thing: A+ spots over-called by ~46%, C spots under-called by ~28%.
    # The situation matters, just less than the model assumed -- so the combined
    # adjustment is pulled toward 1.0 by PROJ_SHRINK, fitted on Week 1 actuals.
    raw_mult = pace_mult * match_mult * env_mult
    mult     = 1.0 + (raw_mult - 1.0) * PROJ_SHRINK
    proj     = base * mult
    if p.get("inj_status") in ("Out", "IR", "PUP"):
        return None

    # Touchdown chance, anchored to measured rates rather than assumed shares.
    #
    # Starts from this position's real TDs-per-game, scales it by how much of
    # the team's work the player gets, then blends in his OWN scoring rate from
    # last season -- regressed toward the positional mean so one big year does
    # not run away with it. His own rate already includes rushing scores, which
    # is how a running quarterback keeps his edge without being double counted.
    norm   = TD_USAGE_NORM.get(pos, 0.18)
    usage  = p.get("usage_share") or norm
    umult  = 1.0 if pos == "QB" else max(0.25, min(2.0, usage / norm))
    pos_rate = TD_BASE.get(pos, 0.3) * umult

    gp = p.get("form_games") or 0
    own_tds = (p.get("pass_tds", 0) or 0) + (p.get("rush_tds", 0) or 0) \
        + (p.get("rec_tds", 0) or 0)
    rate = (0.6 * (own_tds / gp) + 0.4 * pos_rate) if gp else pos_rate

    exp_td = max(0.0, rate * env_mult)
    td_pct = round((1 - pow(2.71828, -exp_td)) * 100)

    return {
        "yards":     round(proj),
        "estimated": est,
        "td_pct":    min(td_pct, 95),
        "label":     label,
        "plays":     round(plays, 1),
        "implied":   round(implied, 1),
        "base":      round(base),
    }


# How much each position group counts toward the overall lean. The quarterback
# swings a game more than a tight end does, so the weights say so.
PREVIEW_WEIGHTS = {"QB": 1.6, "RB": 1.0, "WR": 1.3, "TE": 0.7, "DEF": 1.3}

# Game lean: how much the Vegas spread counts versus our position comparison,
# and how tight a game must be before we decline to pick. Both set from the
# Week 1 2026 result (Vegas 12-4, position lean 4-4 with 8 toss-ups).
LEAN_VEGAS_WEIGHT = 0.6
LEAN_TOSSUP       = 0.15

# Plain-English words for each unit, so the write-up never says "composite"
UNIT_WORDS = {"QB": "quarterback", "RB": "running game", "WR": "receivers",
              "TE": "tight end", "DEF": "defense"}


def _unit_score(scored, team, pos):
    """
    A team's strength at one position: the starter matters most, so this takes
    the best card, nudged by the second option (depth still counts a little).
    """
    grp = sorted([p for p in scored if p["team"] == team and p["position"] == pos],
                 key=lambda x: x["composite"], reverse=True)
    if not grp:
        return None
    if len(grp) == 1 or pos in ("QB", "DEF"):
        return grp[0]["composite"]
    return round(grp[0]["composite"] * 0.7 + grp[1]["composite"] * 0.3, 2)


def game_preview(game, scored, game_totals):
    """
    A novice-readable read on who has the better side of a matchup.

    Compares each position group head to head, then says which way the whole
    game leans and whether that agrees with the betting line. Deliberately
    avoids every piece of jargon on the player cards -- no EPA, no composite,
    no percentiles -- because this is the part someone reads first.
    """
    home, away = game["home_abbr"], game["away_abbr"]
    rows, h_pts, a_pts = [], 0.0, 0.0

    for pos in ("QB", "RB", "WR", "TE", "DEF"):
        hs, as_ = _unit_score(scored, home, pos), _unit_score(scored, away, pos)
        if hs is None or as_ is None:
            continue
        diff = hs - as_
        if abs(diff) < 0.7:
            verdict, winner = "Even", None
        else:
            winner  = home if diff > 0 else away
            verdict = "Big edge" if abs(diff) >= 1.5 else "Edge"
        w = PREVIEW_WEIGHTS[pos]
        h_pts += hs * w
        a_pts += as_ * w
        rows.append({"pos": pos, "home": hs, "away": as_,
                     "verdict": verdict, "winner": winner, "diff": round(diff, 1)})

    if not rows:
        return None

    total_w = sum(PREVIEW_WEIGHTS[r["pos"]] for r in rows)
    h_avg, a_avg = h_pts / total_w, a_pts / total_w
    model_margin = h_avg - a_avg          # >0 favours home

    # Week 1: the position-only lean went 4-4 and called 8 of 16 games a
    # toss-up, while the Vegas favourite went 12-4. The spread is a better
    # signal than our position comparison, so it now carries most of the
    # weight in the lean. ESPN quotes the spread from the home side: -3.5
    # means home favoured by 3.5. Three points is treated as one "unit" of
    # edge, roughly matching a 1.0 gap in our 0-10 position scores.
    od     = game.get("odds") or {}
    spread = od.get("spread")
    if spread is not None:
        vegas_margin = -spread / 3.0
        margin = LEAN_VEGAS_WEIGHT * vegas_margin + (1 - LEAN_VEGAS_WEIGHT) * model_margin
    else:
        vegas_margin = None
        margin = model_margin

    fav = home if margin > 0 else away
    dog = away if margin > 0 else home

    # Threshold lowered from 0.3: refusing to pick half the slate is not a
    # useful read. A toss-up is now reserved for genuinely tight games.
    if abs(margin) < LEAN_TOSSUP:
        strength, headline = "toss-up", f"{home} vs {away} looks close to even."
    elif abs(margin) < 0.8:
        strength = "slight lean"
        headline = f"{fav} has the slight edge over {dog}."
    elif abs(margin) < 1.5:
        strength = "clear lean"
        headline = f"{fav} looks like the better side against {dog}."
    else:
        strength = "strong lean"
        headline = f"{fav} has the clear advantage over {dog}."

    # Where the favorite wins, and where the underdog can punch back
    fav_wins = [UNIT_WORDS[r["pos"]] for r in rows if r["winner"] == fav]
    dog_wins = [UNIT_WORDS[r["pos"]] for r in rows if r["winner"] == dog]

    def listify(xs):
        if len(xs) == 1:
            return xs[0]
        if len(xs) == 2:
            return f"{xs[0]} and {xs[1]}"
        return ", ".join(xs[:-1]) + f", and {xs[-1]}"

    parts = []
    if fav_wins:
        parts.append(f"{fav} has the better {listify(fav_wins)}.")
    if dog_wins:
        parts.append(f"{dog} answers at {listify(dog_wins)}.")
    if not fav_wins and not dog_wins:
        parts.append("Neither side has a meaningful edge at any position.")

    # Say plainly where the read comes from. When our positions and Vegas
    # disagree, the boys should see both -- not a blended number pretending
    # to be one opinion.
    vegas_note = ""
    if spread is not None and spread != 0:
        v_fav = home if spread < 0 else away
        line  = abs(spread)
        m_fav = home if model_margin > 0 else away
        if abs(model_margin) < 0.3:
            vegas_note = (f"Our position-by-position numbers see this as even; "
                          f"Vegas favors {v_fav} by {line:g}, and that carries the lean.")
        elif m_fav == v_fav:
            vegas_note = f"Our numbers and Vegas agree: {v_fav} by {line:g}."
        else:
            vegas_note = (f"Split read: our positions favor {m_fav}, but Vegas has "
                          f"{v_fav} by {line:g}. The lean follows Vegas.")

    if game.get("neutral"):
        parts.append(f"Played at a neutral site, so {home} gets no home-field edge.")

    total = game_totals.get(game["game_id"])
    if total:
        if total >= 48:
            parts.append("This one is expected to be high scoring, "
                         "so both offenses should get chances.")
        elif total <= 41:
            parts.append("This is expected to be a low-scoring game, "
                         "which usually means fewer chances for everyone.")

    return {"home": home, "away": away, "rows": rows, "headline": headline,
            "strength": strength, "favorite": fav, "margin": round(margin, 2),
            "model_margin": round(model_margin, 2),
            "vegas_margin": round(vegas_margin, 2) if vegas_margin is not None else None,
            "spread": spread,
            "body": " ".join(parts), "vegas": vegas_note,
            "kickoff": game.get("kickoff", ""), "game_id": game["game_id"]}


# ============================================================
#  RESULTS TRACKING
# ============================================================

def _repo_get(path, headers):
    """Fetch a file from the GitHub repo. Returns (bytes, sha) or (None, None)."""
    import base64
    try:
        r = requests.get(f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{path}",
                         headers=headers, timeout=15)
        if r.status_code != 200:
            return None, None
        j = r.json()
        return base64.b64decode(j["content"]), j["sha"]
    except Exception:
        return None, None


def sync_predictions_down(season, week):
    """
    Pull this week's prediction log from the repo before doing anything.

    The tool now runs from two places -- the PC and GitHub Actions (phone). If
    each kept its own log they would drift apart and the scorecard would be
    unreliable. The repo copy is the single record; whichever machine runs
    starts from it and pushes back to it.
    """
    if not GITHUB_TOKEN:
        return
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github.v3+json"}
    fname = f"pred_{season}_w{week:02d}.json"
    data, _ = _repo_get(f"{PREDICTIONS_FOLDER}/{fname}", headers)
    if data is None:
        return
    os.makedirs(PREDICTIONS_FOLDER, exist_ok=True)
    local = os.path.join(PREDICTIONS_FOLDER, fname)
    try:
        remote = json.loads(data)
        if os.path.exists(local):
            with open(local, encoding="utf-8") as f:
                mine = json.load(f)
            if mine.get("logged_at", "") >= remote.get("logged_at", ""):
                return                       # local copy is already the newer one
        with open(local, "wb") as f:
            f.write(data)
        print(f"  Synced {fname} from repo (logged {remote.get('logged_at','?')[:16]})")
    except Exception as e:
        print(f"  [!] Prediction sync: {e}")


def sync_predictions_up(path):
    """Push the prediction log back to the repo so every machine sees it."""
    if not GITHUB_TOKEN or not os.path.exists(path):
        return
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github.v3+json"}
    with open(path, "rb") as f:
        content = f.read()
    rel = path.replace("\\", "/")
    ok = deploy_file(content, rel, headers, f"Predictions {os.path.basename(path)}")
    if not ok:
        print(f"  [!] Could not push {rel} to repo")


def log_predictions(season, week, scored, games):
    """
    Save this week's calls so they can be graded later.

    Rewritten on every run so the stored file is the LAST read before kickoff --
    except once a game has finished, at which point the file is frozen. Logging
    a "prediction" after the result is known would quietly make the accuracy
    numbers meaningless.
    """
    os.makedirs(PREDICTIONS_FOLDER, exist_ok=True)
    path = os.path.join(PREDICTIONS_FOLDER, f"pred_{season}_w{week:02d}.json")

    # Freezing is PER GAME, not per week. A week runs Thursday to Monday, so
    # locking everything the moment the opener ends would leave Sunday's calls
    # stuck on Wednesday's information -- no Friday injury report, no late line
    # moves. Each game locks only when that game has actually been played.
    done_teams = {t for g in games if g.get("started") or g.get("completed")
                  for t in (g["home_abbr"], g["away_abbr"])}

    prior = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for r in json.load(f).get("predictions", []):
                    prior[(r["name"], r["position"])] = r
        except Exception as e:
            print(f"  [!] Could not read prior predictions: {e}")

    rows, kept = [], 0
    for p in scored:
        key = (p["name"], p["position"])
        if p["team"] in done_teams:
            if key in prior:
                rows.append(prior[key])      # keep the pre-kickoff call
                kept += 1
                continue
            # Game already played and we never logged it -- record it, but flag
            # it so scoring ignores it rather than crediting hindsight.
            late = True
        else:
            late = False

        row = {"name": p["name"], "position": p["position"], "team": p["team"],
               "opp": p["opp"], "grade": p["grade"], "composite": p["composite"]}
        if late:
            row["late"] = True
        if p["position"] == "DEF":
            row["proj_pts_allowed"] = p.get("opp_impl")
        else:
            pr = project_player(p)
            if pr:
                row.update({"proj_yards": pr["yards"], "td_pct": pr["td_pct"],
                            "stat": pr["label"], "estimated": pr["estimated"]})
        rows.append(row)

    # Game leans, logged directly so they can be scored against the winner
    # rather than reconstructed later. Same per-game lock as the players.
    prior_leans = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                prior_leans = {l["game_id"]: l for l in json.load(f).get("leans", [])}
        except Exception:
            pass
    leans = []
    for g in games:
        if g["home_abbr"] in done_teams and g["game_id"] in prior_leans:
            leans.append(prior_leans[g["game_id"]])
            continue
        pv = game_preview(g, scored, {})
        if pv:
            leans.append({"game_id": g["game_id"], "home": g["home_abbr"],
                          "away": g["away_abbr"], "favorite": pv["favorite"],
                          "strength": pv["strength"], "margin": pv["margin"],
                          "model_margin": pv["model_margin"],
                          "vegas_margin": pv["vegas_margin"], "spread": pv["spread"]})

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"season": season, "week": week,
                   "logged_at": datetime.now().isoformat(timespec="seconds"),
                   "predictions": rows, "leans": leans}, f, indent=1)
    msg = f"  Logged {len(rows)} predictions + {len(leans)} game leans -> {path}"
    if kept:
        msg += f" ({kept} locked -- games started or final)"
    print(msg)
    return path


def _actuals(season, week):
    """Actual player production for one week, keyed by normalised name."""
    try:
        import nflreadpy as nfl
        import polars as pl
    except ImportError:
        return {}
    try:
        ps = nfl.load_player_stats(seasons=[season]).filter(
            (pl.col("week") == week) & (pl.col("season_type") == "REG"))
    except Exception as e:
        print(f"  [!] Actuals for {season} wk{week}: {e}")
        return {}

    out = {}
    for r in ps.iter_rows(named=True):
        pos = r.get("position") or ""
        yards = (r.get("passing_yards") or 0) + (r.get("rushing_yards") or 0) \
            if pos == "QB" else \
            (r.get("rushing_yards") or 0) if pos == "RB" else \
            (r.get("receiving_yards") or 0)
        tds = ((r.get("passing_tds") or 0) + (r.get("rushing_tds") or 0)
               + (r.get("receiving_tds") or 0))
        out[_norm_name(r.get("player_display_name") or "")] = {
            "yards": float(yards), "tds": int(tds), "position": pos}
    return out


def score_week(season, week):
    """
    Grade one logged week against what actually happened.

    Reports two things that matter and are easy to misread separately:
      * Do better grades actually produce more yards? (does the ranking work)
      * Is the projection close? (does the number work)
    """
    path = os.path.join(PREDICTIONS_FOLDER, f"pred_{season}_w{week:02d}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    actual = _actuals(season, week)
    if not actual:
        return None

    # Which teams have actually finished their game this week. Needed for two
    # things: deciding whether the week is still in progress, and knowing when
    # "no stat line" means "produced nothing" rather than "hasn't played yet".
    finished = _finished_teams(season, week)

    tiers, errs, matched, zeroed = {}, [], 0, 0
    td_hit = td_exp = 0.0
    for p in data["predictions"]:
        if p["position"] == "DEF" or "proj_yards" not in p:
            continue
        if p.get("late"):
            continue   # logged after the game -- not a real prediction
        a = actual.get(_norm_name(p["name"]))
        if a is None:
            if p["team"] not in finished:
                continue          # game not played yet -- nothing to score
            # Game is final and he has no stat line: he was on the depth chart,
            # not ruled out, and touched the ball zero times. That is a real
            # result of 0, not a missing one. Dropping these players quietly
            # inflated the low grades' "actual" -- the guys we called for 10
            # yards who did nothing were vanishing from the scorecard.
            a = {"yards": 0.0, "tds": 0}
            zeroed += 1
        matched += 1
        tier = p["grade"]
        t = tiers.setdefault(tier, {"n": 0, "actual": 0.0, "proj": 0.0})
        t["n"] += 1
        t["actual"] += a["yards"]
        t["proj"] += p["proj_yards"]
        errs.append(abs(a["yards"] - p["proj_yards"]))
        td_exp += p.get("td_pct", 0) / 100.0
        td_hit += 1 if a["tds"] > 0 else 0

    if not matched:
        return None
    for t in tiers.values():
        t["avg_actual"] = round(t["actual"] / t["n"], 1)
        t["avg_proj"] = round(t["proj"] / t["n"], 1)

    # "In progress" means games are still to be played -- NOT that some players
    # had no stat line. The old check compared matched players to logged
    # players, which can never reach 100% because backups with zero touches
    # never appear in the box score, so a fully finished week read as partial.
    logged = sum(1 for p in data["predictions"]
                 if p["position"] != "DEF" and "proj_yards" in p and not p.get("late"))
    all_teams = {p["team"] for p in data["predictions"]}
    games_left = len(all_teams - finished) // 2
    return {"season": season, "week": week, "matched": matched, "zeroed": zeroed,
            "mae": round(sum(errs) / len(errs), 1),
            "tiers": tiers, "partial": games_left > 0, "games_left": games_left,
            "logged": logged,
            "td_predicted": round(td_exp, 1), "td_actual": int(td_hit)}


def _finished_teams(season, week):
    """ESPN abbreviations of every team whose game that week is final."""
    try:
        import nflreadpy as nfl
        import polars as pl
        s = nfl.load_schedules(seasons=[season]).filter(
            (pl.col("week") == week) & pl.col("result").is_not_null())
        out = set()
        for r in s.iter_rows(named=True):
            for t in (r["home_team"], r["away_team"]):
                out.add(NFLVERSE_TO_ESPN.get(t, t))
        return out
    except Exception:
        return set()


def accuracy_history(season, upto_week):
    """Every scored week so far, newest first."""
    out = []
    for w in range(1, upto_week + 1):
        r = score_week(season, w)
        if r:
            out.append(r)
    return out


def render_html(week, season, games, players_by_pos, timestamp, previews=None, history=None):

    def inj_badge(p):
        s = p.get("inj_status", "Active")
        d = p.get("inj_detail", "").replace('"', "&quot;")
        if not s or s == "Active":
            return ""
        clr = {"Questionable":"#ffc107","Doubtful":"#ff9800","Out":"#ff4444","IR":"#ff4444","PUP":"#ff4444"}.get(s,"#aaa")
        tip = f' title="{d}"' if d else ""
        return f' <span style="background:#1a1a1a;color:{clr};border:1px solid {clr};border-radius:6px;padding:1px 7px;font-size:11px;font-weight:bold;cursor:default"{tip}>{s}</span>'

    def bar(score, color="#4caf50"):
        w = int(score * 10)
        return f'<div style="background:#21262d;border-radius:4px;height:8px;width:100px;display:inline-block;vertical-align:middle"><div style="background:{color};height:8px;border-radius:4px;width:{w}%"></div></div>'

    def pace_chip(pace):
        c = {"Fast":"#00bcd4","Moderate":"#ffc107","Slow":"#ff7043"}.get(pace,"#aaa")
        return f'<span style="background:#1a1a1a;color:{c};border:1px solid {c};border-radius:10px;padding:2px 10px;font-size:11px">{pace} Pace</span>'

    TREND_COLOR = {"Hot": "#00e676", "Cold": "#ff5252", "Neutral": "#ffc107", "Out": "#ff4444"}

    def player_card(p, rank, hidden=False):
        # Team logo rides in as a CSS variable so the watermark lives entirely in
        # a ::after layer -- it can't push layout or sit on top of the text.
        logo = f"https://a.espncdn.com/i/teamlogos/nfl/500/{p['team'].lower()}.png"
        css  = f"--logo:url('{logo}')"
        style = (f' style="display:none;{css}"' if hidden else f' style="{css}"')
        home_away = ("Neutral site" if p.get("neutral")
                     else "Home" if p.get("is_home") else "Away")
        team_color = TEAM_COLORS.get(
            p["game"]["home_team"] if p["is_home"] else p["game"]["away_team"],
            DEFAULT_COLOR
        )
        trend_clr  = TREND_COLOR.get(p.get("form_trend", "Neutral"), "#ffc107")
        epa_note   = (f" &bull; {p['epa_allowed']:+.3f} EPA/play allowed"
                      if p.get("epa_allowed") is not None else "")
        u_share    = p.get("usage_share", 0)
        u_pct      = f"{u_share*100:.0f}% {p.get('usage_label','usage')}" if u_share else ""
        u_vol      = f"{p['usage_vol']} {p.get('usage_vol_label','')}" if p.get("usage_vol") else ""
        u_snap     = f"{p['snap_pct']:.0f}% snaps" if p.get("snap_pct") else ""
        usage_line = " &bull; ".join(x for x in [u_pct, u_vol, u_snap] if x) or "—"
        # QBs run the whole offense, so share thresholds only mean something for skill players
        if p["position"] == "QB":
            usage_note = "Full-time starter" if u_share >= 0.75 else "Split/backup snaps"
        elif u_share >= 0.25:  usage_note = "High usage -- volume is there"
        elif u_share >= 0.15:  usage_note = "Moderate usage"
        elif u_share > 0:      usage_note = "Low usage -- boom/bust risk"
        else:                  usage_note = "Usage data not available"
        wx_color   = "#ff5252" if p.get("wind_mph", 0) >= 15 and not p.get("indoor") else "#8b949e"
        wx_line    = "Dome" if p.get("indoor") else f"{p.get('temp_f',65)}F  Wind {p.get('wind_mph',0)} mph  {p.get('wx_cond','Clear')}"
        pl = p.get("props", [])
        prop_main = " &nbsp;&bull;&nbsp; ".join(
            f"{x['stat']} <strong>{x['line']:g}</strong>" for x in pl) or "—"
        prop_sub  = ("Lines via DraftKings -- place on DK" if pl else "Not posted yet")
        # Season total is the honest summary; the "last 3" slots all hold the
        # same season average until real 2026 game logs exist.
        dr = p.get("depth_rank")
        backup = " &middot; backup, no projection" if (p["position"] == "QB"
                                                     and p.get("qb_starter") is False) else ""
        depth_tag = (f'<span style="color:#8b949e;font-size:11px;margin-left:6px">'
                     f'{p["position"]}{dr}{backup}</span>') if dr and dr <= 4 else ""
        st, gp = p.get("form_total"), p.get("form_games")
        last3_str  = (f"{st:,} total over {gp} games" if st and gp
                      else "No 2025 stats -- rookie or did not play")
        return f"""
        <div class="player-card"{style} data-hidden="{1 if hidden else 0}" data-team="{p['team']}">
          <div class="card-rank" style="background:{p['grade_color']}22;color:{p['grade_color']};border:1px solid {p['grade_color']}44">
            #{rank} &nbsp;<strong style="font-size:1.1rem">{p['grade']}</strong>
          </div>
          <div class="card-body">
            <div class="card-name">
              <span style="color:{team_color};font-size:1.1rem;font-weight:700">{p['name']}</span>
              {inj_badge(p)}
              {depth_tag}
              <span style="color:#8b949e;font-size:13px;margin-left:8px">{p['team']} ({home_away}) vs {p['opp']}</span>
            </div>
            <div class="card-label" style="color:{p['grade_color']}">{p['label']}</div>

            <div class="card-stats">
              <div class="stat-block">
                <div class="stat-label">Matchup</div>
                <div class="stat-val">{bar(p['matchup_score'], '#4caf50')} {p['matchup_score']:.1f}/10</div>
                <div class="stat-sub">{STAT_SEASON}: allowed {p['ypg_allowed']} YPG to {p['position']}s &bull; Rank #{p['def_rank']}{epa_note}</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Scoring Env</div>
                <div class="stat-val">{bar(p['env_score'], '#2196f3')} {p['env_score']:.1f}/10</div>
                <div class="stat-sub">Game total {p['game_total']:.0f} &bull; {p['team']} implied {p['team_total']:.1f} pts</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Game Pace</div>
                <div class="stat-val">{pace_chip(p['off_pace'])} {p['off_plays_pg']} plays/gm &nbsp;<span style="color:#484f58">({p['team']} offense)</span></div>
                <div class="stat-sub">Opp ({p['opp']}) defense pace: {pace_chip(p['opp_pace'])} {p['opp_plays_pg']} plays/gm &bull; {'Indoor' if p['indoor'] else 'Outdoor'}</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Offense</div>
                <div class="stat-val">{bar(p['off_score'], '#ff9800')} {p['off_ppg']:.1f} PPG</div>
                <div class="stat-sub">Red zone efficiency {p['rz_pct']}%</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Production ({STAT_SEASON} season avg)</div>
                <div class="stat-val">{bar(p['form_score'], trend_clr)} {p['form_avg']:.0f} avg {p['form_label']} &nbsp;<span style="color:{trend_clr};font-weight:bold">{p['form_trend']}</span></div>
                <div class="stat-sub">{p['form_label']}: {last3_str}</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Usage ({STAT_SEASON} season)</div>
                <div class="stat-val">{usage_line}</div>
                <div class="stat-sub">{usage_note}</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Weather</div>
                <div class="stat-val">{wx_line}</div>
                <div class="stat-sub" style="color:{wx_color}">{p.get('wx_impact','Good conditions')}</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">DraftKings Props</div>
                <div class="stat-val">{prop_main}</div>
                <div class="stat-sub">{prop_sub}</div>
              </div>
            </div>

            <div class="card-scout">{p['scout']}</div>
          </div>
          <div class="card-score" style="color:{p['grade_color']}">{p['composite']:.1f}</div>
        </div>"""

    def defense_card(p, rank, hidden=False):
        """A DEF card -- same shell as a player card, but the stats a DST is
        actually judged on: who it faces, turnovers, pass rush, points allowed."""
        logo  = f"https://a.espncdn.com/i/teamlogos/nfl/500/{p['team'].lower()}.png"
        css   = f"--logo:url('{logo}')"
        style = (f' style="display:none;{css}"' if hidden else f' style="{css}"')
        home_away = ("Neutral site" if p.get("neutral")
                     else "Home" if p["is_home"] else "Away")
        team_color = TEAM_COLORS.get(
            p["game"]["home_team"] if p["is_home"] else p["game"]["away_team"], DEFAULT_COLOR)
        wx_line = "Dome" if p.get("indoor") else f"{p.get('wind_mph',0)} mph wind"
        opp_epa_note = (f" &bull; {p['opp_epa']:+.3f} EPA/play"
                        if p.get("opp_epa") is not None else "")
        return f"""
        <div class="player-card"{style} data-hidden="{1 if hidden else 0}" data-team="{p['team']}">
          <div class="card-rank" style="background:{p['grade_color']}22;color:{p['grade_color']};border:1px solid {p['grade_color']}44">
            #{rank} &nbsp;<strong style="font-size:1.1rem">{p['grade']}</strong>
          </div>
          <div class="card-body">
            <div class="card-name">
              <span style="color:{team_color};font-size:1.1rem;font-weight:700">{p['name']}</span>
              <span style="color:#8b949e;font-size:13px;margin-left:8px">{p['team']} ({home_away}) vs {p['opp']}</span>
            </div>
            <div class="card-label" style="color:{p['grade_color']}">{p['label']}</div>

            <div class="card-stats">
              <div class="stat-block">
                <div class="stat-label">Opponent Offense</div>
                <div class="stat-val">{bar(p['opp_score'], '#4caf50')} {p['opp_score']:.1f}/10</div>
                <div class="stat-sub">{p['opp']} scores {p['opp_ppg']:.1f} PPG{opp_epa_note}</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Vegas Outlook</div>
                <div class="stat-val">{bar(p['vegas_sc'], '#2196f3')} {p['vegas_sc']:.1f}/10</div>
                <div class="stat-sub">{p['opp']} implied {p['opp_impl']} pts</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Turnover Edge</div>
                <div class="stat-val">{bar(p['to_score'], '#ffc107')} {p['to_score']:.1f}/10</div>
                <div class="stat-sub">{p['takeaways_pg']:.2f} takeaways/gm vs {p['giveaways']:.2f} giveaways/gm</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Pass Rush</div>
                <div class="stat-val">{bar(p['rush_sc'], '#ff9800')} {p['rush_sc']:.1f}/10</div>
                <div class="stat-sub">{p['sacks_pg']:.2f} sacks/gm</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Unit Quality</div>
                <div class="stat-val">{bar(p['own_sc'], '#9c27b0')} {p['own_sc']:.1f}/10</div>
                <div class="stat-sub">{p['pts_allowed']:.1f} pts allowed/gm</div>
              </div>
              <div class="stat-block">
                <div class="stat-label">Defensive TDs ({STAT_SEASON})</div>
                <div class="stat-val">{p['def_tds']}</div>
                <div class="stat-sub">{wx_line}</div>
              </div>
            </div>

            <div class="card-scout">{p['scout']}</div>
          </div>
          <div class="card-score" style="color:{p['grade_color']}">{p['composite']:.1f}</div>
        </div>"""

    def pos_section(pos, players, pid):
        card_fn = defense_card if pos == "DEF" else player_card
        top  = players[:TOP_PER_POS]
        rest = players[TOP_PER_POS:]
        cards = "".join(card_fn(p, i+1) for i, p in enumerate(top))
        cards += "".join(card_fn(p, i+TOP_PER_POS+1, hidden=True) for i, p in enumerate(rest))
        btn = ""
        if rest:
            # Total is kept in a data attribute -- the label loses the number
            # once it reads "Show Less", so it can't be the source of truth.
            noun = "defenses" if pos == "DEF" else "players"
            btn = (f'<button class="show-btn" data-total="{len(players)}" data-open="0" '
                   f'data-noun="{noun}" '
                   f'onclick="toggleCards(\'{pid}\',this)">Show All ({len(players)} {noun})</button>')
        pos_labels = {"QB":"Quarterbacks","RB":"Running Backs","WR":"Wide Receivers",
                      "TE":"Tight Ends","DEF":"Defense / Special Teams"}
        return f"""
        <div class="pos-section" id="{pid}">
          <h2 class="pos-header">{pos_labels.get(pos, pos)}</h2>
          <div class="cards">{cards}</div>
          {btn}
        </div>"""

    # Games slate header -- each chip filters the board down to that game's two teams
    games_html = """
        <div class="game-chip all-chip active" onclick="filterGame(this,'','')">
          <div class="game-teams"><strong>All Games</strong></div>
          <div class="game-time">Full board</div>
          <div class="game-meta">Tap any game to focus it</div>
        </div>"""
    for g in games:
        try:
            ko = to_eastern(g["kickoff"])
            ko_str = ko.strftime("%a %b %d  %I:%M %p ") + ko.tzname()
        except Exception:
            ko_str = g["kickoff"]
        roof = "Dome" if g.get("indoor") else "Outdoor"
        if g.get("neutral"):
            roof += " &bull; Neutral site"
        games_html += f"""
        <div class="game-chip" onclick="filterGame(this,'{g['away_abbr']}','{g['home_abbr']}')">
          <div class="game-teams"><strong>{g['away_abbr']}</strong> @ <strong>{g['home_abbr']}</strong></div>
          <div class="game-time">{ko_str}</div>
          <div class="game-meta">{g['venue']} &bull; {roof}</div>
        </div>"""

    # ---- Accuracy: how last week's calls actually did ----
    def accuracy_html_block(hist):
        if not hist:
            return ""
        cards = ""
        for r in sorted(hist, key=lambda x: -x["week"])[:4]:
            rows = ""
            for g in ("A+", "A", "B+", "B", "C", "D"):
                t = r["tiers"].get(g)
                if not t:
                    continue
                clr = GRADE_COLORS.get(g, "#8b949e")
                rows += (f'<div class="ac-row"><span class="ac-g" style="color:{clr}">{g}</span>'
                         f'<span class="ac-n">{t["n"]}</span>'
                         f'<span class="ac-v">{t["avg_proj"]:.0f}<small>called</small></span>'
                         f'<span class="ac-v">{t["avg_actual"]:.0f}<small>actual</small></span></div>')
            partial = (' <span class="ac-partial">in progress &mdash; '
                       f'{r.get("games_left", "?")} games still to play</span>'
                       ) if r.get("partial") else ""
            cards += (f'<div class="ac-card"><h3 class="pl-head">Week {r["week"]}{partial}</h3>'
                      f'<div class="ac-sum">{r["matched"]} players scored &bull; '
                      f'typical miss <b>{r["mae"]:.0f} yds</b> &bull; '
                      f'TDs called <b>{r["td_predicted"]:.0f}</b> vs <b>{r["td_actual"]}</b> actual</div>'
                      f'<div class="ac-rows">{rows}</div></div>')
        n = sum(r["matched"] for r in hist)
        return (f'<div id="accuracy-section" class="about closed">'
                f'<button class="about-bar" onclick="toggleResults()">'
                f'<span><strong>How we did</strong> &mdash; {n} past calls scored against '
                f'what actually happened</span>'
                f'<span class="about-caret" id="results-caret">&#9662;</span></button>'
                f'<div class="about-body">'
                f'<p class="section-note">Every call we made, checked against the real result. '
                f'If the grades work, the "actual" column should fall as the grade drops. '
                f'Weeks marked <em>in progress</em> only cover games already played.</p>'
                f'<div class="ac-grid">{cards}</div></div></div>')

    accuracy_html = accuracy_html_block(history)

    # ---- Matchup previews (plain-English game reads) ----
    def preview_card(pv):
        rows = ""
        for r in pv["rows"]:
            if r["winner"] is None:
                who, clr = "Even", "#8b949e"
            else:
                who = f"{r['winner']} {r['verdict'].lower()}"
                clr = "#00e676" if r["verdict"] == "Big edge" else "#69f0ae"
            rows += (f'<div class="pv-row"><span class="pv-pos">{UNIT_WORDS[r["pos"]].title()}</span>'
                     f'<span class="pv-bar"><b style="width:{r["away"]*10:.0f}%"></b></span>'
                     f'<span class="pv-num">{r["away"]:.1f}</span>'
                     f'<span class="pv-vs">{pv["away"]} v {pv["home"]}</span>'
                     f'<span class="pv-num">{r["home"]:.1f}</span>'
                     f'<span class="pv-bar"><b style="width:{r["home"]*10:.0f}%"></b></span>'
                     f'<span class="pv-edge" style="color:{clr}">{who}</span></div>')
        try:
            # Must go through to_eastern() like the slate chips do. This used
            # to format the raw UTC stamp and label it ET, so every preview
            # showed kickoff four hours late -- 1:00 games read as 5:00 PM.
            k  = to_eastern(pv["kickoff"])
            ko = k.strftime("%a %b %d %I:%M %p ") + k.tzname()
        except Exception:
            ko = ""
        vegas = f'<div class="pv-vegas">{pv["vegas"]}</div>' if pv["vegas"] else ""
        return f"""
        <div class="preview-card" data-teams="{pv['away']}|{pv['home']}">
          <div class="pv-head">
            <span class="pv-title">{pv['away']} @ {pv['home']}</span>
            <span class="pv-ko">{ko}</span>
          </div>
          <div class="pv-headline">{pv['headline']}</div>
          <div class="pv-body">{pv['body']}</div>
          {vegas}
          <div class="pv-rows">{rows}</div>
        </div>"""

    previews_html = "".join(preview_card(pv) for pv in (previews or []))

    pos_html = "".join(
        pos_section(pos, players_by_pos.get(pos, []), f"sec_{pos}")
        for pos in ["QB", "RB", "WR", "TE", "DEF"]
    )

    # ---- Projected leaders, top 5 per position ----------------------
    POS_TITLES = {"QB": "Quarterbacks", "RB": "Running Backs",
                  "WR": "Wide Receivers", "TE": "Tight Ends",
                  "DEF": "Defenses"}

    def proj_block(pos):
        rows_html = ""
        if pos == "DEF":
            # For a defense the projection that matters is points allowed,
            # so this list is ranked low-to-high.
            items = sorted(players_by_pos.get("DEF", []),
                           key=lambda x: x.get("opp_impl", 99))
            for i, d in enumerate(items):
                rows_html += (
                    f'<div class="pl-row" data-team="{d["team"]}" data-extra="{0 if i < 5 else 1}">'
                    f'<span class="pl-rank" style="color:{d["grade_color"]}">{i+1}</span>'
                    f'<span class="pl-name"><strong>{d["name"]}</strong>'
                    f'<small>vs {d["opp"]}</small></span>'
                    f'<span class="pl-proj">{d.get("opp_impl", 0):.0f}<small>pts allowed</small></span>'
                    f'<span class="pl-td">{d.get("sacks_pg", 0):.1f}<small>sacks/gm</small></span>'
                    f'<span class="pl-grade" style="color:{d["grade_color"]}">{d["grade"]}</span>'
                    f'</div>')
            return rows_html

        scored_list = []
        for pl in players_by_pos.get(pos, []):
            pr = project_player(pl)
            if pr:
                scored_list.append((pr["yards"], pl, pr))
        scored_list.sort(key=lambda t: -t[0])
        for i, (_, pl, pr) in enumerate(scored_list):
            est = '<span class="pl-est" title="No prior-season stats -- estimated from depth chart">est</span>' if pr["estimated"] else ""
            rows_html += (
                f'<div class="pl-row" data-team="{pl["team"]}" data-extra="{0 if i < 5 else 1}">'
                f'<span class="pl-rank" style="color:{pl["grade_color"]}">{i+1}</span>'
                f'<span class="pl-name"><strong>{pl["name"]}</strong>{est}'
                f'<small>{pl["team"]} vs {pl["opp"]}</small></span>'
                f'<span class="pl-proj">{pr["yards"]}<small>proj {pr["label"].lower()}</small></span>'
                f'<span class="pl-td">{pr["td_pct"]}%<small>TD chance</small></span>'
                f'<span class="pl-grade" style="color:{pl["grade_color"]}">{pl["grade"]}</span>'
                f'</div>')
        return rows_html

    proj_html = "".join(
        f'<div class="pl-card"><h3 class="pl-head">{POS_TITLES[pos]}</h3>'
        f'<div class="pl-rows">{proj_block(pos)}</div></div>'
        for pos in ("QB", "RB", "WR", "TE", "DEF")
    )

    pages_base = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Gridiron Guru">
<meta name="theme-color" content="#0d1117">
<title>Gridiron Guru -- NFL Week {week}</title>
<link rel="manifest" href="{pages_base}/manifest.json">
<link rel="apple-touch-icon" href="{pages_base}/icon.png">
<link rel="icon" type="image/png" href="{pages_base}/icon.png">
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#0d1117; color:#e6edf3; font-family:'Segoe UI',system-ui,sans-serif; line-height:1.5; }}

/* ── Splash screen ─────────────────────────────────────── */
#splash {{
  position:fixed; inset:0; z-index:9999;
  background:radial-gradient(ellipse at center, #0a1f0a 0%, #050f05 60%, #000 100%);
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  transition:opacity 0.8s ease;
}}
#splash.fade-out {{ opacity:0; pointer-events:none; }}
.splash-title {{
  font-size:2.8rem; font-weight:900; letter-spacing:6px;
  color:#3fb950; text-shadow:0 0 40px #3fb95088;
  margin-top:24px; animation:glow 2s ease-in-out infinite alternate;
}}
.splash-sub {{
  font-size:1rem; color:#8b949e; letter-spacing:3px;
  margin-top:8px; text-transform:uppercase;
}}
.crowd-text {{
  position:absolute; bottom:48px; font-size:13px;
  color:#3fb95055; letter-spacing:2px; animation:crowd 1.5s ease-in-out infinite alternate;
}}
@keyframes glow {{
  from {{ text-shadow:0 0 20px #3fb95066; }}
  to   {{ text-shadow:0 0 60px #3fb950cc, 0 0 100px #3fb95044; }}
}}
@keyframes crowd {{
  from {{ opacity:0.3; letter-spacing:2px; }}
  to   {{ opacity:0.9; letter-spacing:4px; }}
}}
@keyframes ballArc {{
  0%   {{ transform:translate(0,0) rotate(0deg);   opacity:0; }}
  10%  {{ opacity:1; }}
  60%  {{ transform:translate(120px,-90px) rotate(180deg); }}
  100% {{ transform:translate(200px,20px) rotate(300deg); opacity:0.2; }}
}}
@keyframes qbThrow {{
  0%,40% {{ transform:rotate(0deg); }}
  60%    {{ transform:rotate(-18deg); }}
  80%    {{ transform:rotate(8deg); }}
  100%   {{ transform:rotate(0deg); }}
}}
@keyframes fadeUp {{
  from {{ opacity:0; transform:translateY(20px); }}
  to   {{ opacity:1; transform:translateY(0); }}
}}
.splash-scene {{ position:relative; width:260px; height:200px; }}
.qb-figure   {{ position:absolute; bottom:10px; left:20px; animation:qbThrow 1.8s ease-in-out infinite; transform-origin:50% 80%; }}
.football    {{ position:absolute; bottom:90px; left:80px; animation:ballArc 1.8s ease-in-out infinite; }}
.crowd-wave  {{ position:absolute; bottom:0; left:0; right:0; }}

.header {{ background:linear-gradient(135deg,#0f2a0f,#0d1117 70%);
           padding:36px 24px; text-align:center; border-bottom:2px solid #238636; }}
.header h1 {{ font-size:2.6rem; color:#3fb950; letter-spacing:3px; font-weight:800; }}
.header p  {{ color:#8b949e; margin-top:6px; font-size:15px; }}

.container {{ max-width:1100px; margin:0 auto; padding:28px 16px; }}

h2.section-title {{ color:#3fb950; font-size:1.2rem; margin:36px 0 16px;
                    border-bottom:1px solid #21262d; padding-bottom:8px; }}

/* Games slate */
.games-row {{ display:flex; flex-wrap:wrap; gap:12px; margin-bottom:32px; }}
.game-chip {{ background:#161b22; border:1px solid #30363d; border-radius:10px;
              padding:14px 18px; min-width:200px;
              cursor:pointer; user-select:none;
              transition:border-color .15s, background .15s, transform .1s; }}
.game-chip:hover {{ border-color:#238636; background:#1c2128; }}
.game-chip:active {{ transform:scale(.98); }}
.game-chip.active {{ border-color:#00e676; background:#0f2417;
                     box-shadow:0 0 0 1px #00e67655; }}
.game-chip.active .game-teams {{ color:#00e676; }}
.all-chip {{ min-width:150px; }}
.game-teams {{ font-size:1rem; font-weight:700; color:#e6edf3; }}
.game-time  {{ font-size:12px; color:#8b949e; margin-top:3px; }}
.game-meta  {{ font-size:11px; color:#484f58; margin-top:2px; }}
.filter-banner {{ display:none; background:#0f2417; border:1px solid #00e67655;
                  border-radius:8px; padding:10px 16px; margin-bottom:24px;
                  color:#00e676; font-size:13px; font-weight:600; }}

/* "How this works" explainer. Open on a first visit, collapsed after that --
   the boys read it once, then it should get out of the way. */
.about {{ margin:0 0 26px; }}
.about-bar {{ width:100%; display:flex; justify-content:space-between; align-items:center;
              gap:12px; background:#161b22; border:1px solid #30363d; border-radius:12px;
              padding:13px 17px; color:#c9d1d9; font-size:13px; text-align:left;
              cursor:pointer; font-family:inherit; }}
.about-bar:hover {{ border-color:#3fb950; }}
.about-bar strong {{ color:#3fb950; }}
.about-caret {{ color:#8b949e; transition:transform .18s; }}
.about.closed .about-caret {{ transform:rotate(-90deg); }}
.about.closed .about-body {{ display:none; }}
.about-body {{ background:#161b22; border:1px solid #30363d; border-top:none;
               border-radius:0 0 12px 12px; margin-top:-12px; padding:20px 20px 16px;
               color:#c9d1d9; font-size:13px; line-height:1.6; }}
.about-body p {{ margin:0 0 12px; }}
.about-lede {{ color:#e6edf3; font-size:1.05rem; font-weight:700; }}
.about-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr));
               gap:18px; margin:16px 0; }}
.about-grid h4 {{ margin:0 0 8px; font-size:.78rem; letter-spacing:1px;
                  text-transform:uppercase; color:#3fb950; }}
.about-grid ul, .about-grid ol {{ margin:0; padding-left:18px; }}
.about-grid li {{ margin-bottom:5px; }}
.about-body b {{ color:#e6edf3; }}
.about-sub {{ color:#8b949e; font-size:12px; }}
.about-fine {{ color:#484f58; font-size:11px; border-top:1px solid #21262d;
               padding-top:10px; margin-top:14px !important; }}

/* Matchup previews -- the plain-English read on each game */
.section-note {{ color:#8b949e; font-size:12px; margin:-8px 0 14px; }}
.previews {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(430px,1fr));
             gap:14px; margin-bottom:32px; }}
.preview-card {{ background:#161b22; border:1px solid #30363d; border-radius:12px;
                 padding:16px 18px; }}
.pv-head {{ display:flex; justify-content:space-between; align-items:baseline;
            margin-bottom:8px; }}
.pv-title {{ font-size:1.05rem; font-weight:800; color:#e6edf3; }}
.pv-ko {{ font-size:11px; color:#8b949e; }}
.pv-headline {{ color:#00e676; font-weight:700; font-size:.95rem; margin-bottom:6px; }}
.pv-body {{ color:#c9d1d9; font-size:13px; line-height:1.5; }}
.pv-vegas {{ color:#8b949e; font-size:12px; margin-top:6px; font-style:italic; }}
.pv-rows {{ margin-top:12px; border-top:1px solid #21262d; padding-top:10px; }}
.pv-row {{ display:flex; align-items:center; gap:6px; font-size:11px;
           color:#8b949e; padding:3px 0; }}
.pv-pos {{ min-width:96px; color:#c9d1d9; font-weight:600; }}
.pv-num {{ min-width:26px; text-align:center; color:#e6edf3; }}
.pv-vs  {{ min-width:66px; text-align:center; color:#484f58; font-size:10px; }}
.pv-bar {{ flex:1; height:5px; background:#21262d; border-radius:3px; overflow:hidden; }}
.pv-bar b {{ display:block; height:100%; background:#3fb950; }}
.pv-edge {{ min-width:104px; text-align:right; font-weight:600; }}

/* Accuracy -- did the calls actually work */
.ac-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr));
            gap:14px; margin-bottom:32px; }}
.ac-card {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:14px 16px; }}
.ac-sum {{ color:#8b949e; font-size:12px; margin-bottom:10px; }}
.ac-sum b {{ color:#e6edf3; }}
.ac-partial {{ color:#ffb74d; font-size:10px; text-transform:none;
               letter-spacing:0; font-weight:600; }}
.ac-row {{ display:flex; align-items:center; gap:10px; padding:5px 8px;
           background:#0d1117; border-radius:7px; margin-bottom:3px; font-size:12px; }}
.ac-g {{ font-weight:800; min-width:26px; }}
.ac-n {{ color:#484f58; min-width:34px; font-size:11px; }}
.ac-v {{ flex:1; text-align:right; color:#e6edf3; font-weight:700; }}
.ac-v small {{ display:block; font-size:9px; font-weight:400; color:#8b949e; text-transform:uppercase; }}

/* Projected leaders -- top 5 per position */
.pl-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(400px,1fr));
            gap:14px; margin-bottom:32px; }}
.pl-card {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:14px 16px; }}
.pl-head {{ font-size:.8rem; letter-spacing:1px; text-transform:uppercase;
            color:#3fb950; margin:0 0 10px; }}
.pl-row {{ display:flex; align-items:center; gap:10px; padding:7px 8px;
           background:#0d1117; border-radius:8px; margin-bottom:4px; }}
.pl-rank {{ font-size:1rem; font-weight:800; min-width:18px; }}
/* Everything past the top 5 is rendered but hidden, so a game filter can pull
   that matchup's players up without the page needing a second data source.
   Hidden in CSS as well as JS so the first paint is correct. */
.pl-row[data-extra="1"] {{ display:none; }}
.pl-name {{ flex:1; font-size:13px; color:#e6edf3; min-width:0; }}
.pl-name small {{ display:block; color:#8b949e; font-size:10px; }}
.pl-est {{ font-size:9px; color:#ffb74d; border:1px solid #ffb74d55; border-radius:4px;
           padding:0 3px; margin-left:5px; vertical-align:middle; }}
.pl-proj {{ min-width:64px; text-align:right; font-size:1.05rem; font-weight:800; color:#e6edf3; }}
.pl-proj small {{ display:block; font-size:9px; font-weight:400; color:#8b949e; text-transform:uppercase; }}
.pl-td {{ min-width:54px; text-align:right; font-size:.95rem; font-weight:700; color:#69f0ae; }}
.pl-td small {{ display:block; font-size:9px; font-weight:400; color:#8b949e; text-transform:uppercase; }}
.pl-grade {{ min-width:30px; text-align:right; font-weight:800; font-size:.95rem; }}

/* Top 10 */
.top10 {{ background:#161b22; border:1px solid #30363d; border-radius:12px;
          padding:20px; margin-bottom:32px; }}
.top10-row {{ display:flex; align-items:center; gap:12px; padding:10px 12px;
              border-radius:8px; margin-bottom:4px; background:#0d1117; }}
.top10-row:hover {{ background:#1c2128; }}
.top10-rank  {{ font-size:1.2rem; font-weight:800; min-width:28px; }}
.top10-name  {{ flex:1; }}
.top10-grade {{ font-weight:700; min-width:36px; }}
.top10-score {{ font-weight:700; min-width:36px; }}
.top10-label {{ font-size:12px; flex:1.5; }}

/* Position sections */
.pos-header {{ display:inline-block; background:#238636; color:#fff;
               padding:5px 18px; border-radius:14px; font-size:0.9rem;
               letter-spacing:1px; margin-bottom:14px; margin-top:32px; }}

/* Player cards */
.cards {{ display:flex; flex-direction:column; gap:12px; }}
.player-card {{ display:flex; align-items:stretch; background:#161b22;
                border:1px solid #30363d; border-radius:12px; overflow:hidden;
                position:relative; }}
/* Team logo watermark. Kept in its own layer behind the content: pointer-events
   off so it never blocks clicks, and low opacity so text stays readable. */
.player-card::after {{
  content:""; position:absolute; top:50%; right:18px; transform:translateY(-50%);
  width:150px; height:150px; background-image:var(--logo);
  background-size:contain; background-repeat:no-repeat; background-position:center;
  opacity:.12; pointer-events:none; z-index:0;
}}
.player-card > * {{ position:relative; z-index:1; }}
.player-card:hover::after {{ opacity:.20; }}
.player-card:hover {{ border-color:#3fb950; }}
.card-rank {{ display:flex; flex-direction:column; align-items:center;
              justify-content:center; min-width:70px; padding:12px 8px;
              text-align:center; font-size:12px; font-weight:600; }}
.card-body {{ flex:1; padding:14px 16px; }}
.card-name  {{ margin-bottom:4px; }}
.card-label {{ font-size:12px; font-weight:600; letter-spacing:1px;
               text-transform:uppercase; margin-bottom:10px; }}
.card-stats {{ display:flex; flex-wrap:wrap; gap:16px; margin-bottom:10px; }}
.stat-block {{ min-width:200px; }}
.stat-label {{ font-size:11px; color:#8b949e; text-transform:uppercase;
               letter-spacing:0.5px; margin-bottom:3px; }}
.stat-val   {{ font-size:13px; margin-bottom:2px; }}
.stat-sub   {{ font-size:11px; color:#8b949e; }}
.card-scout {{ font-size:12px; color:#8b949e; border-top:1px solid #21262d;
               padding-top:8px; margin-top:4px; }}
.card-score {{ display:flex; align-items:center; justify-content:center;
               min-width:64px; font-size:2rem; font-weight:800; padding:12px; }}

/* Show all button */
.show-btn {{ margin-top:12px; padding:8px 22px; background:#21262d; color:#8b949e;
             border:1px solid #30363d; border-radius:8px; cursor:pointer; font-size:13px; }}
.show-btn:hover {{ background:#30363d; color:#e6edf3; }}

/* ---- Responsive -------------------------------------------------------
   The stat blocks had a 200px min-width, so on a phone only one fit per row
   and each card stacked into an 849px tower -- taller than the screen itself.
   On small screens they become an explicit grid instead, which lays the stats
   out horizontally and cuts card height by more than half. Landscape gets a
   third column since the extra width is there. */
@media (max-width:760px) {{
  .container   {{ padding-left:10px; padding-right:10px; }}
  .card-stats  {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr));
                  gap:9px 12px; }}
  .stat-block  {{ min-width:0; }}          /* the culprit -- grid sizes these now */
  .stat-val    {{ font-size:12px; word-break:break-word; }}
  .stat-label  {{ font-size:10px; }}
  .stat-sub    {{ font-size:10px; }}
  .card-rank   {{ min-width:46px; padding:8px 4px; font-size:11px; }}
  .card-score  {{ min-width:44px; font-size:1.4rem; padding:8px 4px; }}
  .card-body   {{ padding:12px 10px; }}
  .card-scout  {{ font-size:11px; }}
  .player-card::after {{ width:104px; height:104px; right:6px; }}
  .game-chip   {{ min-width:calc(50% - 6px); padding:10px 12px; }}
  .games-row   {{ gap:8px; }}
  /* Top-10 rows are a single flex line on desktop; let them wrap on a phone
     so the name is never crushed to a few characters. */
  .top10-row   {{ flex-wrap:wrap; gap:4px 10px; padding:9px 10px; }}
  .top10-name  {{ flex:1 0 100%; order:-1; }}
  .top10-label {{ flex:1 0 100%; font-size:11px; }}
  .top10       {{ padding:12px; }}
  .previews    {{ grid-template-columns:1fr; gap:10px; }}
  .pl-grid     {{ grid-template-columns:1fr; gap:10px; }}
  .ac-grid     {{ grid-template-columns:1fr; gap:10px; }}
  .about-grid  {{ grid-template-columns:1fr; gap:14px; }}
  .about-body  {{ padding:15px 14px 12px; font-size:12.5px; }}
  .about-bar   {{ font-size:12px; padding:11px 14px; }}
  .pl-card     {{ padding:12px 13px; }}
  .pl-proj     {{ min-width:56px; font-size:.95rem; }}
  .pl-td       {{ min-width:46px; font-size:.85rem; }}
  .preview-card {{ padding:13px 14px; }}
  .pv-bar      {{ display:none; }}
  .pv-pos      {{ min-width:78px; }}
  .pv-vs       {{ min-width:56px; }}
  .pv-edge     {{ flex:1; }}
  .legend      {{ gap:6px; }}
  .legend-item {{ font-size:11px; }}
}}

/* Landscape phones: more width available, so go three across */
@media (max-width:1000px) and (orientation:landscape) {{
  .card-stats {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr));
                 gap:9px 14px; }}
  .stat-block {{ min-width:0; }}
  .game-chip  {{ min-width:calc(33.333% - 8px); }}
}}

/* Very narrow phones -- keep two columns but tighten everything further */
@media (max-width:380px) {{
  .card-stats {{ gap:8px 8px; }}
  .card-rank  {{ min-width:40px; }}
  .card-score {{ min-width:38px; font-size:1.2rem; }}
  .game-chip  {{ min-width:100%; }}
}}

/* Legend */
.legend {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:32px; }}
.legend-item {{ background:#161b22; border:1px solid #30363d; border-radius:8px;
                padding:8px 14px; font-size:13px; }}

.footer {{ text-align:center; padding:32px; color:#484f58; font-size:12px; }}
</style>
</head>
<body>

<div id="splash">
  <div class="splash-scene">
    <!-- Crowd in background -->
    <svg class="crowd-wave" viewBox="0 0 260 60" xmlns="http://www.w3.org/2000/svg">
      <g fill="#1a3a1a" opacity="0.7">
        <rect x="0"  y="40" width="18" height="20" rx="3"/>
        <rect x="22" y="32" width="18" height="28" rx="3"/>
        <rect x="44" y="38" width="18" height="22" rx="3"/>
        <rect x="66" y="28" width="18" height="32" rx="3"/>
        <rect x="88" y="36" width="18" height="24" rx="3"/>
        <rect x="110" y="30" width="18" height="30" rx="3"/>
        <rect x="132" y="42" width="18" height="18" rx="3"/>
        <rect x="154" y="34" width="18" height="26" rx="3"/>
        <rect x="176" y="26" width="18" height="34" rx="3"/>
        <rect x="198" y="38" width="18" height="22" rx="3"/>
        <rect x="220" y="32" width="18" height="28" rx="3"/>
        <rect x="242" y="44" width="18" height="16" rx="3"/>
      </g>
      <!-- Arms raised -->
      <g stroke="#3fb950" stroke-width="2" opacity="0.5">
        <line x1="9"   y1="40" x2="2"  y2="28"/>
        <line x1="9"   y1="40" x2="16" y2="26"/>
        <line x1="31"  y1="32" x2="24" y2="20"/>
        <line x1="31"  y1="32" x2="38" y2="18"/>
        <line x1="75"  y1="28" x2="68" y2="16"/>
        <line x1="75"  y1="28" x2="82" y2="14"/>
        <line x1="119" y1="30" x2="112" y2="18"/>
        <line x1="119" y1="30" x2="126" y2="16"/>
        <line x1="185" y1="26" x2="178" y2="14"/>
        <line x1="185" y1="26" x2="192" y2="12"/>
      </g>
    </svg>

    <!-- QB figure -->
    <svg class="qb-figure" width="80" height="120" viewBox="0 0 80 120" xmlns="http://www.w3.org/2000/svg">
      <!-- Jersey -->
      <ellipse cx="40" cy="42" rx="18" ry="22" fill="#238636"/>
      <!-- Helmet -->
      <ellipse cx="40" cy="18" rx="14" ry="13" fill="#1a6e2e"/>
      <rect x="26" y="22" width="28" height="5" rx="2" fill="#0d1117"/>
      <!-- Facemask -->
      <path d="M26 24 Q24 30 28 32" stroke="#aaa" stroke-width="2" fill="none"/>
      <!-- Throwing arm raised -->
      <line x1="58" y1="30" x2="74" y2="10" stroke="#1a6e2e" stroke-width="8" stroke-linecap="round"/>
      <!-- Other arm -->
      <line x1="22" y1="38" x2="10" y2="52" stroke="#1a6e2e" stroke-width="8" stroke-linecap="round"/>
      <!-- Legs -->
      <line x1="34" y1="62" x2="28" y2="90" stroke="#0d2b1a" stroke-width="10" stroke-linecap="round"/>
      <line x1="46" y1="62" x2="52" y2="90" stroke="#0d2b1a" stroke-width="10" stroke-linecap="round"/>
      <!-- Number -->
      <text x="40" y="48" text-anchor="middle" fill="#fff" font-size="14" font-weight="bold">12</text>
    </svg>

    <!-- Football -->
    <svg class="football" width="28" height="18" viewBox="0 0 28 18" xmlns="http://www.w3.org/2000/svg">
      <ellipse cx="14" cy="9" rx="13" ry="8" fill="#8B4513"/>
      <line x1="1" y1="9" x2="27" y2="9" stroke="#fff" stroke-width="1.5"/>
      <line x1="14" y1="1" x2="14" y2="17" stroke="#fff" stroke-width="1"/>
      <line x1="9"  y1="2" x2="9"  y2="16" stroke="#fff" stroke-width="0.8"/>
      <line x1="19" y1="2" x2="19" y2="16" stroke="#fff" stroke-width="0.8"/>
    </svg>
  </div>

  <div class="splash-title">GRIDIRON GURU</div>
  <div class="splash-sub">NFL Matchup Intelligence</div>
  <div class="crowd-text">TOUCHDOWN &nbsp;&bull;&nbsp; THE CROWD GOES WILD</div>
</div>

<div class="header">
  <h1>GRIDIRON GURU</h1>
  <p>NFL Matchup Cheat Sheet &bull; Season {season} &bull; Week {week}</p>
  <p style="font-size:12px;margin-top:6px;color:#484f58">Generated {timestamp}</p>
</div>

<div class="container">

  <div class="about" id="about">
    <button class="about-bar" onclick="toggleAbout()">
      <span><strong>How this works</strong> &mdash; what the grades mean and where the numbers come from</span>
      <span class="about-caret" id="about-caret">&#9662;</span>
    </button>
    <div class="about-body" id="about-body">
      <p class="about-lede">Who's set up for a big week &mdash; and why.</p>

      <p>A weekly cheat sheet. We grade every starting quarterback, running back,
      receiver, tight end and defense on how good their <em>spot</em> is this week &mdash;
      not how good the player is. A star facing a shutdown defense in a slow,
      low-scoring game can be a worse play than a solid guy facing a leaky defense
      in a shootout. This finds those spots.</p>

      <div class="about-grid">
        <div>
          <h4>How to read a card</h4>
          <ul>
            <li><b>Grade A+ &rarr; D</b> &mdash; how favorable the matchup is. A+ = elite spot, D = avoid.</li>
            <li><b>Big number (0&ndash;10)</b> &mdash; the same thing as a score. Higher is better.</li>
            <li><b>Projected yards</b> &mdash; what we expect this week, adjusted for pace, matchup and expected points.</li>
            <li><b>TD chance</b> &mdash; odds he finds the end zone at least once.</li>
          </ul>
        </div>
        <div>
          <h4>The five things behind every grade</h4>
          <ol>
            <li><b>Matchup</b> &mdash; how generous that defense is to this position</li>
            <li><b>Scoring environment</b> &mdash; how many points Vegas expects</li>
            <li><b>Production</b> &mdash; what he actually did last season</li>
            <li><b>Pace</b> &mdash; how many plays the game should run <em>(more plays = more chances)</em></li>
            <li><b>Usage</b> &mdash; how much of his team's work he really gets</li>
          </ol>
          <p class="about-sub">Then adjusted for injuries, weather and wind.</p>
        </div>
      </div>

      <p><b>How to use it:</b> tap any game in the slate below to zero in on just those
      two teams. <b>&ldquo;Who Has the Edge&rdquo;</b> gives a plain-English read on every
      matchup &mdash; who's favored and where each side wins.</p>

      <p class="about-sub"><b>Which season:</b> every team and player stat on this board is the
      <b>{STAT_SEASON} season</b> &mdash; defensive ranks, pace, production, usage. One or two weeks of
      the new season is too little to trust, so last year stays the baseline until enough {season}
      games are in to blend them. Injuries, lines, props and weather are live this week.</p>

      <p class="about-sub"><b>Where the numbers come from:</b> ESPN (schedule, stats,
      injuries, DraftKings lines) &middot; nflverse (snap counts, efficiency) &middot;
      Open-Meteo (weather). All free, refreshed every time it runs.</p>

      <p class="about-fine">Matchup analysis, not betting advice. Lines are shown for
      reference &mdash; place anything you like on DraftKings.</p>
    </div>
  </div>

  <h2 class="section-title" id="slate-section">This Week's Slate</h2>
  <div class="games-row">{games_html}</div>
  <div id="filter-banner" class="filter-banner"></div>

  {accuracy_html}

  <div id="preview-section">
    <h2 class="section-title">Who Has the Edge -- Plain English</h2>
    <p class="section-note">Each game compared position by position. Higher number = better spot this week.</p>
    <div class="previews">{previews_html}</div>
  </div>

  <div id="top10-section">
    <h2 class="section-title">Projected Leaders -- Top 5 by Position</h2>
    <p class="section-note">Season baseline adjusted for game pace, matchup, and how many points the team is expected to score.</p>
    <div class="pl-grid">{proj_html}</div>
  </div>

  <h2 class="section-title">Full Matchup Breakdown by Position</h2>

  <div class="legend">
    <div class="legend-item"><strong style="color:#00e676">A+</strong> Elite Spot (7.5+) -- premier matchup, start with confidence</div>
    <div class="legend-item"><strong style="color:#69f0ae">A</strong> Strong Play (6.5+) -- very favorable conditions</div>
    <div class="legend-item"><strong style="color:#b9f6ca">B+</strong> Solid (5.5+) -- good matchup, above average</div>
    <div class="legend-item"><strong style="color:#fff176">B</strong> Decent (4.5+) -- neutral, situational</div>
    <div class="legend-item"><strong style="color:#ffb74d">C</strong> Risky (3.5+) -- tough spot, proceed with caution</div>
    <div class="legend-item"><strong style="color:#ff5252">D</strong> Fade -- avoid this matchup</div>
  </div>

  {pos_html}

</div>

<div class="footer">
  <p>Gridiron Guru &bull; Data: ESPN (free) &bull; Vegas: The Odds API</p>
  <p style="margin-top:6px">Matchup analysis only -- not betting advice</p>
</div>

<script>
// Splash screen — dismiss after 2.8s or on tap
(function() {{
  var splash = document.getElementById('splash');
  function dismiss() {{
    splash.classList.add('fade-out');
    setTimeout(function() {{ splash.style.display='none'; }}, 900);
  }}
  setTimeout(dismiss, 2800);
  splash.addEventListener('click', dismiss);
}})();

/* ---- Slate filter + expand/collapse -------------------------------
   Both features decide the same thing (is a card visible?), so they share
   one function. Filtering by game overrides the top-5 cap -- if you drill
   into a matchup you want every player from it, not just five.          */
var activeTeams = null;   // null = no game filter, else [away, home]

function cardVisible(card, sectionOpen) {{
  if (activeTeams) return activeTeams.indexOf(card.dataset.team) !== -1;
  return card.dataset.hidden === '0' || sectionOpen;
}}

function applyVisibility() {{
  document.querySelectorAll('.pos-section').forEach(function(sec) {{
    var btn  = sec.querySelector('.show-btn');
    var open = btn && btn.dataset.open === '1';
    var shown = 0;
    sec.querySelectorAll('.player-card').forEach(function(c) {{
      var vis = cardVisible(c, open);
      c.style.display = vis ? 'flex' : 'none';
      if (vis) shown++;
    }});
    // A game only involves two teams, so some positions may have nobody
    sec.style.display = shown ? 'block' : 'none';
    if (btn) btn.style.display = activeTeams ? 'none' : 'block';
  }});

  var pvShown = 0;
  document.querySelectorAll('.preview-card').forEach(function(c) {{
    var teams = (c.dataset.teams || '').split('|');
    // A preview belongs to a game, so show it only when BOTH its teams match
    var vis = !activeTeams ||
              (teams.indexOf(activeTeams[0]) !== -1 && teams.indexOf(activeTeams[1]) !== -1);
    c.style.display = vis ? 'block' : 'none';
    if (vis) pvShown++;
  }});
  var pvSec = document.getElementById('preview-section');
  if (pvSec) pvSec.style.display = pvShown ? 'block' : 'none';

  var t10shown = 0;
  // Unfiltered: the league's top 5. Filtered to one game: that game's top 5,
  // which is what you actually want when you drill into a matchup.
  document.querySelectorAll('.pl-card').forEach(function(card) {{
    var shown = 0;
    card.querySelectorAll('.pl-row').forEach(function(r) {{
      var match = !activeTeams || activeTeams.indexOf(r.dataset.team) !== -1;
      var vis;
      if (!activeTeams) {{ vis = r.dataset.extra === '0'; }}
      else {{ vis = match && shown < 5; }}
      if (vis) {{ shown++; t10shown++; }}
      r.style.display = vis ? 'flex' : 'none';
    }});
  }});
  document.querySelectorAll('.pl-card').forEach(function(c) {{
    var any = [...c.querySelectorAll('.pl-row')].some(function(r) {{ return r.style.display !== 'none'; }});
    c.style.display = any ? 'block' : 'none';
  }});
  document.querySelectorAll('.top10-row').forEach(function(r) {{
    var vis = !activeTeams || activeTeams.indexOf(r.dataset.team) !== -1;
    r.style.display = vis ? 'flex' : 'none';
    if (vis) t10shown++;
  }});
  var t10 = document.getElementById('top10-section');
  if (t10) t10.style.display = t10shown ? 'block' : 'none';
}}

function filterGame(chip, away, home) {{
  var isAll = !away && !home;
  // Tapping the active game again clears the filter
  if (!isAll && chip.classList.contains('active')) {{ isAll = true; chip = null; }}

  activeTeams = isAll ? null : [away, home];
  document.querySelectorAll('.game-chip').forEach(function(c) {{ c.classList.remove('active'); }});
  if (isAll) {{
    var allChip = document.querySelector('.all-chip');
    if (allChip) allChip.classList.add('active');
  }} else {{
    chip.classList.add('active');
  }}

  var banner = document.getElementById('filter-banner');
  if (banner) {{
    banner.style.display = isAll ? 'none' : 'block';
    if (!isAll) banner.textContent = 'Showing only ' + away + ' @ ' + home + ' -- tap again or "All Games" to clear';
  }}

  applyVisibility();
  var anchor = document.getElementById(isAll ? 'slate-section' : 'top10-section');
  if (anchor) anchor.scrollIntoView({{behavior:'smooth', block:'start'}});
}}

function toggleResults() {{
  var el = document.getElementById('accuracy-section');
  var closed = el.classList.toggle('closed');
  // Opt-in by default: results stay collapsed until someone asks for them
  try {{ localStorage.setItem('gg_results_closed', closed ? '1' : '0'); }} catch (e) {{}}
}}
(function () {{
  try {{
    var el = document.getElementById('accuracy-section');
    if (el && localStorage.getItem('gg_results_closed') === '0') el.classList.remove('closed');
  }} catch (e) {{}}
}})();

function toggleAbout() {{
  var el = document.getElementById('about');
  var closed = el.classList.toggle('closed');
  // Remember the choice, but never let a storage failure break the page
  try {{ localStorage.setItem('gg_about_closed', closed ? '1' : '0'); }} catch (e) {{}}
}}
(function () {{
  try {{
    if (localStorage.getItem('gg_about_closed') === '1') {{
      document.getElementById('about').classList.add('closed');
    }}
  }} catch (e) {{}}
}})();

function toggleCards(secId, btn) {{
  var open = btn.dataset.open === '1';
  btn.dataset.open = open ? '0' : '1';
  btn.textContent  = open ? 'Show All (' + btn.dataset.total + ' ' + (btn.dataset.noun||'players') + ')' : 'Show Less';
  applyVisibility();
}}
</script>

</body>
</html>"""


# ============================================================
#  ICON + MANIFEST GENERATION
# ============================================================

def create_football_png(size=180):
    """Generate a 180x180 PNG football icon — no external libs needed."""
    import struct, zlib
    width = height = size
    cx, cy   = width // 2, height // 2
    rx, ry   = int(width * 0.40), int(height * 0.25)

    BG     = (13,  17,  23,  255)   # #0d1117
    GREEN  = (15,  42,  15,  255)   # dark field green
    BROWN  = (139, 69,  19,  255)   # football brown
    WHITE  = (255, 255, 255, 255)

    rows = []
    for y in range(height):
        row = bytearray(b'\x00')    # PNG filter byte
        for x in range(width):
            dx, dy   = x - cx, y - cy
            in_ball  = (dx / rx) ** 2 + (dy / ry) ** 2 <= 1.0
            on_seam  = in_ball and abs(dy) <= max(1, size // 90)
            on_lace  = in_ball and abs(dx) <= max(1, size // 90)
            lace_w   = max(1, size // 120)
            on_sides = (in_ball and abs(dy) <= ry * 0.55 and
                        (abs(dx - rx * 0.35) <= lace_w or abs(dx + rx * 0.35) <= lace_w))
            if on_seam or on_lace or on_sides:
                r, g, b, a = WHITE
            elif in_ball:
                r, g, b, a = BROWN
            else:
                r, g, b, a = GREEN
            row += bytes([r, g, b, a])
        rows.append(bytes(row))

    raw        = b''.join(rows)
    compressed = zlib.compress(raw, 9)

    def chunk(tag, data):
        body = tag + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)

    png  = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
    png += chunk(b'IDAT', compressed)
    png += chunk(b'IEND', b'')
    return png


def create_manifest():
    """Web app manifest so Android Chrome installs it as a standalone app."""
    import json
    return json.dumps({
        "name":             "Gridiron Guru",
        "short_name":       "GridironGuru",
        "description":      "NFL Weekly Matchup Cheat Sheet",
        "start_url":        "./",
        "display":          "standalone",
        "background_color": "#0d1117",
        "theme_color":      "#3fb950",
        # "any" lets the installed app follow the phone's rotation. This was
        # "portrait", which hard-locks the standalone app upright -- landscape
        # is the better read for the wide stat cards, so don't set it back.
        "orientation":      "any",
        "icons": [
            {"src": "icon.png", "sizes": "180x180", "type": "image/png", "purpose": "any maskable"},
        ],
    }, indent=2)


# ============================================================
#  GITHUB DEPLOY
# ============================================================

def deploy_file(content_bytes, filename, headers, commit_msg):
    """Push a single file (bytes) to GitHub Pages."""
    import base64
    url  = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
    sha  = requests.get(url, headers=headers).json().get("sha")
    body = {
        "message": commit_msg,
        "content": base64.b64encode(content_bytes).decode(),
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=headers, json=body)
    return r.status_code in (200, 201)


def deploy(html):
    if not GITHUB_TOKEN:
        print("  [!] No GITHUB_TOKEN -- skipping deploy")
        return
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github.v3+json"}
    msg = f"Gridiron Guru update {datetime.now().strftime('%Y-%m-%d')}"

    # Deploy icon.png and manifest.json once (they rarely change)
    deploy_file(create_football_png(), "icon.png",      headers, msg)
    deploy_file(create_manifest().encode(), "manifest.json", headers, msg)

    # Deploy main HTML
    ok = deploy_file(html.encode(), "index.html", headers, msg)
    if ok:
        print(f"  [OK] Deployed --> https://{GITHUB_USER}.github.io/{GITHUB_REPO}/")
    else:
        print(f"  [!] Deploy failed")


# ============================================================
#  MAIN
# ============================================================

def run():
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    timestamp    = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    season, week = get_current_week()

    print(f"\n[GRIDIRON GURU] NFL {season} Week {week}  --  {timestamp}\n")

    print("  Fetching games...")
    games = get_games(season, week)
    print(f"  {len(games)} games on the slate\n")

    print("  Fetching Vegas totals...")
    vegas_totals, game_totals = get_vegas(games)

    print("  Fetching injury report...")
    injuries = get_injuries()
    flagged  = [n for n,v in injuries.items() if v["status"] != "Active"]
    print(f"  {len(flagged)} players with injury flags")

    print("  Fetching weather forecasts...")
    weather = get_weather(games)

    print("  Fetching player props...")
    # ESPN only: free, unlimited, and the same DraftKings lines. We gather the
    # numbers here; the actual bet gets placed on DK. The Odds API is no longer
    # called -- it cost quota purely to add the over/under price, which we don't
    # use. get_player_props() is kept if that juice is ever wanted back.
    props = get_espn_props(games)
    print(f"  {len(props)} prop lines from ESPN (free, no quota)")

    print(f"  Loading team stats (offense + defense) from ESPN {STAT_SEASON} season...")
    defense_stats, offense_stats = get_all_team_stats()
    nv = get_nflverse_stats()
    merge_nflverse(defense_stats, offense_stats, nv)
    calibrate_thresholds(defense_stats)
    calibrate_epa(defense_stats)
    print()

    print(f"  Building player pool from ESPN {STAT_SEASON} season leaders...")
    # Depth charts first -- they know who actually starts. Leaderboard pool is
    # the fallback if nflverse depth data is unavailable.
    built = build_pool_from_depth_charts(offense_stats, nv, injuries=injuries)
    if built:
        player_pool, recent_form = built
    else:
        player_pool, recent_form = build_player_pool(offense_stats, nv)
    set_positional_means(player_pool, recent_form)
    mark_qb_starters(player_pool, injuries)
    print()

    print("  Scoring matchups...")
    scored = []
    for p in player_pool:
        team = p["team"]
        game = next((g for g in games if g["home_abbr"] == team or g["away_abbr"] == team), None)
        if not game:
            continue
        result = matchup_grade(p, game, defense_stats, offense_stats, vegas_totals, game_totals,
                               injuries, weather, props, recent_form)
        scored.append(result)

    # One DEF entry per team on the slate
    for g in games:
        for team in (g["home_abbr"], g["away_abbr"]):
            if team in defense_stats:
                scored.append(defense_grade(team, g, defense_stats, offense_stats,
                                            vegas_totals, game_totals, weather))

    calibrate_defense_scores(scored)

    # Group and sort by position
    players_by_pos = {}
    for p in scored:
        players_by_pos.setdefault(p["position"], []).append(p)
    for pos in players_by_pos:
        # Backup QBs sort BELOW every starter regardless of grade. The grade is
        # mostly situational, so a QB2 in a soft matchup scores like his
        # starter -- Sam Howell sat #2 in the league behind Dak Prescott for a
        # game he will not play in. The grade stays (it is what the spot would
        # be worth if he is pressed into duty); the ranking no longer rewards it.
        players_by_pos[pos].sort(
            key=lambda x: (x["position"] == "QB" and x.get("qb_starter") is False,
                           -x["composite"]))

    print("  Rendering dashboard...")
    print("  Logging predictions for later scoring...")
    sync_predictions_down(season, week)
    pred_path = log_predictions(season, week, scored, games)
    sync_predictions_up(pred_path)
    # Include the CURRENT week too -- a week runs Thursday to Monday, so by
    # Sunday there are finished games worth showing rather than making everyone
    # wait until next week's report.
    history = accuracy_history(season, week)
    if history:
        print(f"  Scored {len(history)} prior week(s) against actual results")

    previews = [pv for pv in (game_preview(g, scored, game_totals) for g in games) if pv]
    html = render_html(week, season, games, players_by_pos, timestamp, previews, history)

    fname = f"nfl_week{week:02d}_{date.today().isoformat()}.html"
    fpath = os.path.join(OUTPUT_FOLDER, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [OK] Saved: {fpath}")

    deploy(html)

    # Summary
    # Skill players and defenses are scored on separate scales, so they are
    # summarised separately rather than pooled into one misleading ranking.
    print(f"\n  Top matchup plays this week:")
    skill = sorted((p for p in scored if p["position"] != "DEF"),
                   key=lambda x: x["composite"], reverse=True)
    for p in skill[:10]:
        home_away = "vs" if p["is_home"] else "@"
        print(f"    {p['position']:2s}  {p['name']:<22} {home_away} {p['opp']:4s}  {p['composite']:.1f}  {p['grade']}  {p['label']}")

    tops = sorted((p for p in scored if p["position"] == "DEF"),
                  key=lambda x: x["composite"], reverse=True)[:5]
    if tops:
        print(f"\n  Top defenses this week:")
        for p in tops:
            home_away = "vs" if p["is_home"] else "@"
            print(f"    DEF  {p['name']:<22} {home_away} {p['opp']:4s}  {p['composite']:.1f}  {p['grade']}  {p['label']}")

    print(f"\n[DONE] Week {week} cheat sheet ready.\n")

    if AUTO_OPEN_BROWSER:
        webbrowser.open(f"file:///{os.path.abspath(fpath)}")


if __name__ == "__main__":
    run()
