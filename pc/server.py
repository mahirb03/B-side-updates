#!/usr/bin/env python3
"""
Desk dashboard server.

Serves index.html and a /api/state endpoint with clock-adjacent data:
weather, calendar, and whatever the mic last heard on the turntable.

Run:  python server.py
Then open http://localhost:8765
"""

import calendar
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def _trust_store():
    """A packaged .app has no CA bundle of its own, so every HTTPS call fails
    with CERTIFICATE_VERIFY_FAILED. Point Python at certifi's."""
    try:
        import certifi
    except ImportError:
        return
    where = certifi.where()
    if not os.path.exists(where):
        return
    os.environ.setdefault("SSL_CERT_FILE", where)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", where)
    try:
        ssl._create_default_https_context = lambda *a, **k: ssl.create_default_context(cafile=where)
    except Exception:
        pass


_trust_store()
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Inside B-Side.exe the launcher works out the real folder and passes it here;
# the exe itself may be sitting anywhere (the Desktop, say).
HERE = (os.environ.get("BSIDE_HOME")
        or (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__))))
def _user_dir():
    """Where a Mac app is allowed to keep its settings."""
    if sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/B-Side")
    else:
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "B-Side")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


LOG_PATH = os.path.join(_user_dir(), "b-side.log")


def log(*parts):
    """Print, and append to desk.log. pythonw has no console to print to."""
    line = datetime.now().strftime("%H:%M:%S ") + " ".join(str(p) for p in parts)
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

USER_DIR = _user_dir()
CONFIG_PATH = os.path.join(USER_DIR, "config.json")

# ----------------------------------------------------------------- config

DEFAULTS = {
    "service": "",                 # "spotify" or "apple", asked on first run
    "colour": "#2e4a3d",           # the plinth
    "place": "", "latitude": None, "longitude": None, "country": "",
    "auto_location": True,
    "football": {"enabled": True, "teams": [], "max_results": 4,
                 "max_upcoming": 5, "refresh_minutes": 30,
                 "request_gap_seconds": 2.5, "live_refresh_seconds": 30},
    "guest_queue": {"enabled": True, "cooldown_seconds": 20},
    "player_poll_seconds": 2,
    "port": 8765,
    # Public PKCE client id: safe to ship, it can't be used without the
    # person signing in themselves.
    "spotify_client_id": "96be4fd92bd84ee4b1888be48b7bce5c",
    "spotify_poll_seconds": 5,
    "sport": "soccer",
    "modules": {"sport": True, "history": True, "lyrics": False,
                "notes": True, "concerts": False, "aotd": True,
                "room": True, "playlists": True},
    "layout": {"left": ["clock", "weather", "room", "playlists", "qr"],
               "right": ["sport", "history", "aotd", "concerts"]},
    "update_repo": "mahirb03/B-side-updates",   # the public mirror
    "update_branch": "main",
    "setup_done": False,
    "always_on_top": False,
}


def _adopt_side_config():
    """A config.json sitting beside the exe - the old Windows build kept it
    there - is moved into the settings folder the first time we start, so
    upgrading never silently drops the Tuya keys and tokens."""
    if os.path.exists(CONFIG_PATH) or not getattr(sys, "frozen", False):
        return
    beside = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "config.json")
    if os.path.exists(beside):
        try:
            import shutil
            shutil.copyfile(beside, CONFIG_PATH)
        except Exception:
            pass


def load_config():
    _adopt_side_config()
    cfg_ = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg_.update(json.load(f))
    except Exception:
        pass                        # first run: defaults, then the setup page
    return cfg_


def save_config(patch):
    """Merge a change from the settings page and keep it on disk."""
    CFG.update(patch)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CFG, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log("settings: couldn't save:", e)
        return False
    return True

CFG = load_config()

# Secrets can live in the environment instead of config.json.
# The environment wins when both are set.
ENV_KEYS = {
    "audd_token": "DESK_AUDD_TOKEN",
}

def cfg(key, default=None):
    env_name = ENV_KEYS.get(key)
    if env_name:
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val
    v = CFG.get(key, default)
    if isinstance(v, str) and (v.startswith("PASTE_") or not v.strip()):
        return None
    return v

# Shared state the web page polls.
STATE = {
    "weather": None,
    "calendar": [],
    "now_playing": None,     # {"title","artist","album","at"}
    "listening": False,
    "listen_error": None,
    "football": {"live": [], "recent": [], "upcoming": [], "error": None},
    "listen_note": "",       # what the mic did on its last pass
    "identifying": False,    # a click-triggered lookup is in flight
    "spotify_playing": False,
    "spotify_needs_login": False,
    "guest_ready": False,
    "spotify_paused": None,
    "notes": None,
    "queue": [],
    "ac": None,
    "ac_error": None,
    "lamps": [],
    "st_needs_login": False,
}
_ART_CACHE = {}

# One identify at a time - the mic can't serve two callers.
IDENTIFYING = threading.Event()
_CAN_IDENTIFY = None   # worked out on first ask

# Set to cut the listener's wait short — e.g. the moment Spotify pauses.
WAKE_LISTEN = threading.Event()
WAKE_WEATHER = threading.Event()
WAKE_FOOTBALL = threading.Event()


def _layout_now():
    """The saved panel order, topped up with any panel added since it was
    written. Without this a config.json from an older build pins the list to
    whatever panels existed back then, and new ones never appear."""
    saved = CFG.get("layout") or {}
    known = set(DEFAULTS["layout"]["left"]) | set(DEFAULTS["layout"]["right"])
    out, seen = {}, set()
    for side in ("left", "right"):
        keep = []
        for name in (saved.get(side) or DEFAULTS["layout"][side]):
            if name in known and name not in seen:
                keep.append(name)
                seen.add(name)
        out[side] = keep
    for side in ("left", "right"):          # anything new lands on its home side
        for name in DEFAULTS["layout"][side]:
            if name not in seen:
                out[side].append(name)
                seen.add(name)
    return out


def public_settings():
    """What the settings page is allowed to see (no tokens)."""
    return {
        "service": CFG.get("service", ""),
        "colour": CFG.get("colour", "#2e4a3d"),
        "place": CFG.get("place", ""),
        "country": CFG.get("country", ""),
        "latitude": CFG.get("latitude"),
        "longitude": CFG.get("longitude"),
        "auto_location": bool(CFG.get("auto_location", True)),
        "teams": [{"name": t.get("name"), "id": t.get("id", "")}
                  for t in ((CFG.get("football") or {}).get("teams") or [])],
        "guest_queue": bool((CFG.get("guest_queue") or {}).get("enabled", True)),
        "sport": CFG.get("sport", "soccer"),
        "modules": dict(DEFAULTS["modules"], **(CFG.get("modules") or {})),
        "layout": _layout_now(),
        "setup_done": bool(CFG.get("setup_done")),
        "always_on_top": bool(CFG.get("always_on_top")),
        "update_repo": CFG.get("update_repo", ""),
        # the three picks: uri, name, cover - nothing secret
        "playlists": [{"uri": p.get("uri", ""), "name": p.get("name", ""),
                       "image": p.get("image", "")}
                      for p in (CFG.get("playlists") or [])[:3]],
    }
WAKE_SPOTIFY = threading.Event()   # set after a play/pause/volume press


def nap(seconds):
    """Sleep, but wake early if something wants the mic now."""
    if WAKE_LISTEN.wait(seconds):
        WAKE_LISTEN.clear()
LOCK = threading.Lock()

STATE_VERSION = 0
STATE_CHANGED = threading.Condition()


def set_state(**kw):
    """Bumps a version whenever something actually changes, so the dashboard
    can be handed the news instead of asking for it on a timer."""
    global STATE_VERSION
    with LOCK:
        moved = any(STATE.get(k) != v for k, v in kw.items())
        STATE.update(kw)
    if moved:
        with STATE_CHANGED:
            STATE_VERSION += 1
            STATE_CHANGED.notify_all()

def get_state():
    with LOCK:
        return json.loads(json.dumps(STATE))

def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "desk-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# ---------------------------------------------------------------- weather

WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}

def weather_loop():
    ensure_location()
    while True:
        try:
            lat, lon = cfg("latitude"), cfg("longitude")
            place = cfg("place") or ""
            if lat is None or lon is None:
                set_state(weather=None)
                if WAKE_WEATHER.wait(30):
                    WAKE_WEATHER.clear()
                continue
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code"
                "&daily=temperature_2m_max,temperature_2m_min,sunset"
                "&timezone=auto&forecast_days=1"
            )
            j = fetch_json(url)
            c, d = j["current"], j["daily"]
            sunset = ""
            try:
                sunset = datetime.fromisoformat(d["sunset"][0]).strftime("%-I:%M")
            except Exception:
                try:
                    sunset = datetime.fromisoformat(d["sunset"][0]).strftime("%I:%M").lstrip("0")
                except Exception:
                    pass
            set_state(weather={
                "place": place,
                # so the clock can show the time where they are, not where the Mac is
                "utc_offset": j.get("utc_offset_seconds"),
                "tz": j.get("timezone", ""),
                "temp": round(c["temperature_2m"]),
                "feels": round(c["apparent_temperature"]),
                "humidity": c["relative_humidity_2m"],
                "text": WMO.get(c["weather_code"], ""),
                "low": round(d["temperature_2m_min"][0]),
                "high": round(d["temperature_2m_max"][0]),
                "sunset": sunset,
            })
        except Exception as e:
            log("weather:", e)
        if WAKE_WEATHER.wait(600):      # settings changed: refresh at once
            WAKE_WEATHER.clear()

# --------------------------------------------------------------- football
# Follows specific teams rather than whole leagues. TheSportsDB's free key
# returns only a sliver of each league, but per-team lookups (next fixture,
# last result) work fully — and they cover every competition, internationals
# included, without having to know which league a match is in.

SDB = "https://www.thesportsdb.com/api/v1/json/3/"
TEAM_CACHE = os.path.join(USER_DIR, "teams_cache.json")
MATCH_CACHE = os.path.join(USER_DIR, "matches_cache.json")

# Where each competition streams, by country. Rights move around between
# seasons; anything not listed falls back to a "where to watch" search.
WATCH_BY_COUNTRY = {
    "IN": {
        "English Premier League": ("JioHotstar", "https://www.hotstar.com/in/sports/football"),
        "UEFA Champions League":  ("SonyLIV", "https://www.sonyliv.com/"),
        "Spanish La Liga":        ("FanCode", "https://www.fancode.com/football"),
    },
    "US": {
        "English Premier League": ("NBC / Peacock", "https://www.peacocktv.com/sports/premier-league"),
        "UEFA Champions League":  ("Paramount+", "https://www.paramountplus.com/sports/"),
        "Spanish La Liga":        ("ESPN+", "https://www.espn.com/watch/"),
        "Italian Serie A":        ("CBS / Paramount+", "https://www.paramountplus.com/sports/"),
        "German Bundesliga":      ("ESPN+", "https://www.espn.com/watch/"),
    },
    "GB": {
        "English Premier League": ("Sky / TNT", "https://www.skysports.com/football"),
        "UEFA Champions League":  ("TNT Sports", "https://www.tntsports.co.uk/football/"),
        "Spanish La Liga":        ("Premier Sports", "https://www.premiersports.com/"),
    },
    "CA": {
        "English Premier League": ("fuboTV", "https://ca.fubo.tv/"),
        "UEFA Champions League":  ("DAZN", "https://www.dazn.com/"),
    },
    "AU": {
        "English Premier League": ("Optus Sport", "https://sport.optus.com.au/"),
        "UEFA Champions League":  ("Stan Sport", "https://www.stan.com.au/sport"),
    },
    "AE": {
        "English Premier League": ("beIN SPORTS", "https://www.beinsports.com/en-mena/"),
        "UEFA Champions League":  ("beIN SPORTS", "https://www.beinsports.com/en-mena/"),
    },
    "SG": {
        "English Premier League": ("Hub / StarHub", "https://www.starhub.com/"),
    },
}


def country():
    return (cfg("country") or "IN").upper()


def watch_for(league):
    return WATCH_BY_COUNTRY.get(country(), {}).get(league)

SHORT = {
    "English Premier League": "PL",
    "UEFA Champions League": "UCL",
    "UEFA Europa League": "UEL",
    "Spanish La Liga": "LaLiga",
    "Italian Serie A": "Serie A",
    "German Bundesliga": "Bundesliga",
    "French Ligue 1": "Ligue 1",
    "UEFA Nations League": "Nations League",
    "FIFA World Cup": "World Cup",
    "International Friendlies": "Friendly",
}

LIVE_STATUSES = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT"}


def _fold(txt):
    """Lower-case and strip accents, so 'Atlético' matches 'Atletico'."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(txt or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def _load_team_cache():
    try:
        with open(TEAM_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_team_cache(d):
    try:
        with open(TEAM_CACHE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


_LEAGUES = {"at": 0, "rows": []}


def all_leagues():
    """Every soccer league TheSportsDB knows, fetched once."""
    if (_LEAGUES["rows"] and time.time() - _LEAGUES["at"] < 24 * 3600
            and _LEAGUES.get("sport") == cfg("sport", "soccer")):
        return _LEAGUES["rows"]
    try:
        j = fetch_json(SDB + "all_leagues.php", timeout=30)
    except Exception as e:
        log("football: league list failed -", e)
        return _LEAGUES["rows"]
    want_sport = (cfg("sport", "soccer") or "soccer").lower()
    rows = [{"id": l.get("idLeague"), "name": l.get("strLeague"),
             "alt": l.get("strLeagueAlternate") or ""}
            for l in (j.get("leagues") or [])
            if l.get("strLeague") and (want_sport == "any"
                                       or (l.get("strSport") or "").lower() == want_sport)]
    _LEAGUES.update(at=time.time(), rows=rows, sport=(cfg("sport", "soccer") or "soccer"))
    return rows


def search_football(q):
    """What the settings box offers while you type: teams and leagues."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"teams": [], "leagues": []}
    teams = []
    try:
        j = fetch_json(SDB + "searchteams.php?t=" + urllib.parse.quote(q), timeout=20)
        want_sport = (cfg("sport", "soccer") or "soccer").lower()
        for t in (j.get("teams") or [])[:20]:
            if want_sport != "any" and (t.get("strSport") or "").lower() != want_sport:
                continue
            if (t.get("strGender") or "Male").lower() not in ("male", ""):
                continue
            teams.append({"id": t.get("idTeam"), "name": t.get("strTeam"),
                          "badge": t.get("strTeamBadge") or t.get("strBadge") or "",
                          "note": " · ".join(x for x in (t.get("strLeague"),
                                                         t.get("strCountry")) if x)})
    except Exception as e:
        log("football: team search failed -", e)
    fold = _fold(q)
    leagues = [{"id": l["id"], "name": l["name"]}
               for l in all_leagues()
               if fold in _fold(l["name"]) or fold in _fold(l["alt"])][:8]
    return {"teams": teams[:8], "leagues": leagues}


def league_teams(league_id, limit=30):
    """Every club in a league, so 'follow the Premier League' works."""
    try:
        j = fetch_json(SDB + "lookup_all_teams.php?id=" + urllib.parse.quote(str(league_id)),
                       timeout=30)
    except Exception as e:
        log("football: league teams failed -", e)
        return []
    out = []
    for t in (j.get("teams") or []):
        if (t.get("strGender") or "Male").lower() not in ("male", ""):
            continue
        if t.get("idTeam") and t.get("strTeam"):
            out.append({"id": t["idTeam"], "name": t["strTeam"]})
    return out[:limit]


def resolve_team_id(name):
    """Find a team by exact name, in whichever sport is chosen."""
    try:
        j = fetch_json(SDB + "searchteams.php?t=" + urllib.parse.quote(name),
                       timeout=30)
    except Exception as e:
        log("football: search failed for", name, "-", e)
        return None
    want = _fold(name)
    want_sport = (cfg("sport", "soccer") or "soccer").lower()
    for t in (j.get("teams") or []):
        if want_sport != "any" and (t.get("strSport") or "").lower() != want_sport:
            continue
        if (t.get("strGender") or "Male").lower() not in ("male", ""):
            continue
        if _fold(t.get("strTeam")) == want:
            return t.get("idTeam")
    return None


def followed_teams(conf):
    """[(name, id)] with ids pinned in config, cached, or looked up once."""
    cache = _load_team_cache()
    changed = False
    out = []
    for entry in conf.get("teams") or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        name = entry.get("name", "").strip()
        tid = str(entry.get("id") or "").strip() or cache.get(name)
        if not tid and name:
            tid = resolve_team_id(name)
            time.sleep(2)
            if tid:
                cache[name] = tid
                changed = True
                log(f"football: found {name} -> {tid}")
            else:
                log(f"football: could not find '{name}' — add its id in config.json")
        if tid:
            out.append((name, str(tid)))
    if changed:
        _save_team_cache(cache)
    return out


def _ev_dt(ev):
    ts = ev.get("strTimestamp")
    if ts:
        try:
            return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        except Exception:
            pass
    d = ev.get("dateEvent") or ""
    t = (ev.get("strTime") or "00:00:00")[:8]
    try:
        return datetime.strptime(d + " " + t, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    except Exception:
        return None


def _youtube_search(q):
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q)


def _shape(ev, followed):
    dt = _ev_dt(ev)
    league = ev.get("strLeague", "") or ""
    home, away = ev.get("strHomeTeam", ""), ev.get("strAwayTeam", "")
    hs, as_ = ev.get("intHomeScore"), ev.get("intAwayScore")
    status = (ev.get("strStatus") or "").upper()
    now = datetime.now()

    live = status in LIVE_STATUSES or (
        dt is not None and dt <= now <= dt + timedelta(minutes=135)
        and status not in ("FT", "AET", "PEN", "AP", "POSTP", "CANC", "PST"))
    finished = status in ("FT", "AET", "PEN", "AP") or (
        hs not in (None, "") and dt is not None and now > dt + timedelta(minutes=135))

    watch = watch_for(league)
    link, link_label = "", ""
    if live and watch:
        link_label, link = f"Watch on {watch[0]}", watch[1]
    elif live:
        # No rights holder on file for this competition: point at a search
        # for where it's being shown in India rather than leave it dead.
        where = cfg("place") or country()
        link = ("https://www.google.com/search?q="
                + urllib.parse.quote(f"where to watch {home} vs {away} live {where}"))
        link_label = "Where to watch"
    elif finished:
        video = (ev.get("strVideo") or "").strip()
        if video:
            link, link_label = video, "Highlights"
        else:
            link = _youtube_search(f"{home} vs {away} highlights")
            link_label = "Find highlights"

    alt, alt_label = "", ""
    home_of = watch_for(league)
    if finished and home_of and country() == "IN" and league == "English Premier League":
        alt = ("https://www.hotstar.com/in/explore?search_query="
               + urllib.parse.quote(f"{home} vs {away} highlights"))
        alt_label = home_of[0]

    fold_f = {_fold(n) for n in followed}
    return {
        "home_id": str(ev.get("idHomeTeam") or ""),
        "away_id": str(ev.get("idAwayTeam") or ""),
        "home_badge": ev.get("strHomeTeamBadge") or "",
        "away_badge": ev.get("strAwayTeamBadge") or "",
        "status": status,
        "id": ev.get("idEvent", ""),
        "home": home,
        "away": away,
        "home_followed": _fold(home) in fold_f,
        "away_followed": _fold(away) in fold_f,
        "home_score": None if hs in (None, "") else int(hs),
        "away_score": None if as_ in (None, "") else int(as_),
        "league": league,
        "comp": SHORT.get(league, league),
        "when": dt.isoformat() if dt else None,
        "live": bool(live),
        "finished": bool(finished),
        "link": link,
        "link_label": link_label,
        "alt": alt,
        "alt_label": alt_label,
        "_sort": dt or datetime(1970, 1, 1),
    }


def team_info(tid):
    """Colour and short name for a team, looked up once and cached."""
    if not tid:
        return {}
    cache = _load_team_cache()
    info = cache.setdefault("_info", {})
    if tid in info:
        return info[tid]
    try:
        j = fetch_json(f"{SDB}lookupteam.php?id={tid}", timeout=30)
        t = (j.get("teams") or [{}])[0] or {}
    except Exception as e:
        log("football: team info", tid, e)
        return {}
    colours = [c for c in (t.get("strColour1"), t.get("strColour2"), t.get("strColour3"))
               if c and re.fullmatch(r"#[0-9A-Fa-f]{6}", c)]

    def punch(c):
        # prefer a colour with some saturation; white or black kits read badly
        r, g, b_ = (int(c[i:i + 2], 16) for i in (1, 3, 5))
        return max(r, g, b_) - min(r, g, b_)
    colour = max(colours, key=punch) if colours else ""
    info[tid] = {"colour": colour, "short": t.get("strTeamShort") or "",
                 "badge": t.get("strBadge") or ""}
    _save_team_cache(cache)
    return info[tid]


def football_loop():
    # Wait for the first run to name some teams, and pick up edits later.
    while not ((CFG.get("football") or {}).get("teams")):
        if WAKE_FOOTBALL.wait(5):
            WAKE_FOOTBALL.clear()
    conf = CFG.get("football") or {}

    refresh = int(conf.get("refresh_minutes", 30)) * 60
    live_every = int(conf.get("live_refresh_seconds", 60))
    n_results = int(conf.get("max_results", 4))
    n_next = int(conf.get("max_upcoming", 5))
    # The free API rate-limits hard; keep a gap between calls.
    gap = float(conf.get("request_gap_seconds", 2.5))


    def publish(rows_):
        allm = list(rows_.values())
        live = sorted([m for m in allm if m["live"]], key=lambda m: m["_sort"])
        recent = sorted([m for m in allm if m["finished"] and not m["live"]],
                        key=lambda m: m["_sort"], reverse=True)[:n_results]
        upcoming = sorted([m for m in allm if not m["finished"] and not m["live"]],
                          key=lambda m: m["_sort"])[:n_next]
        for m in live:
            for side in ("home", "away"):
                info = team_info(m.get(side + "_id"))
                m[side + "_colour"] = info.get("colour", "")
                m[side + "_short"] = info.get("short", "")
                if not m.get(side + "_badge"):
                    m[side + "_badge"] = info.get("badge", "")

        def clean(lst):
            return [{k: v for k, v in m.items() if k != "_sort"} for m in lst]
        set_state(football={"live": clean(live), "recent": clean(recent),
                            "upcoming": clean(upcoming), "error": None})
        return live

    teams = followed_teams(conf)
    log(f"football: following {len(teams)} teams")
    rows = {}
    last_full = 0

    # Show the last known matches straight away while the first sweep runs.
    try:
        with open(MATCH_CACHE, "r", encoding="utf-8") as f:
            for key, m in json.load(f).items():
                m["_sort"] = datetime.fromisoformat(m["when"]) if m.get("when") else datetime(1970, 1, 1)
                # A game that was live when saved is over if it kicked off long ago.
                if m.get("live") and m["_sort"] < datetime.now() - timedelta(minutes=150):
                    m["live"], m["finished"] = False, True
                rows[key] = m
        if rows:
            log(f"football: showing {len(rows)} cached matches while refreshing")
            publish(rows)
    except Exception:
        pass

    while True:
        conf = CFG.get("football") or {}         # settings can change under us
        if not conf.get("enabled", True):
            set_state(football=None)
            if WAKE_FOOTBALL.wait(30):
                WAKE_FOOTBALL.clear()
            continue
        wanted = [t.get("name") for t in (conf.get("teams") or [])]
        if [n for n, _ in teams] != wanted:
            teams = followed_teams(conf)
            rows, last_full = {}, 0
            log(f"football: now following {len(teams)} teams")
        names = [n for n, _ in teams]

        if time.time() - last_full >= refresh or not rows:
            # Full sweep: every team's last result and next fixture.
            fresh = {}
            for i, (name, tid) in enumerate(teams):
                for endpoint in ("eventslast.php", "eventsnext.php"):
                    try:
                        j = fetch_json(f"{SDB}{endpoint}?id={tid}", timeout=30)
                        for ev in (j.get("results") or j.get("events") or []):
                            if (ev.get("strSport") or "Soccer").lower() != "soccer":
                                continue
                            m = _shape(ev, names)
                            key = m["id"] or (m["home"], m["away"], m["when"])
                            if m["when"]:
                                fresh[key] = m
                    except Exception as e:
                        log(f"football: {name} {endpoint}: {e}")
                    time.sleep(gap)
                # Every few teams, show what's arrived so far (merged with
                # whatever was shown before, so nothing blinks out).
                if i % 3 == 2:
                    publish(dict(rows, **fresh))
            rows = fresh
            last_full = time.time()
            try:
                with open(MATCH_CACHE, "w", encoding="utf-8") as f:
                    json.dump({str(k): {kk: vv for kk, vv in m.items() if kk != "_sort"}
                               for k, m in rows.items()}, f)
            except Exception:
                pass
        else:
            # Between sweeps, only re-check games that are on (or should be).
            now = datetime.now()
            for key, m in list(rows.items()):
                kick = datetime.fromisoformat(m["when"]) if m.get("when") else None
                due = m["live"] or (kick and kick <= now and not m["finished"])
                if not due or not m["id"]:
                    continue
                try:
                    j = fetch_json(f"{SDB}lookupevent.php?id={m['id']}", timeout=30)
                    ev = (j.get("events") or [None])[0]
                    if ev:
                        rows[key] = _shape(ev, names)
                except Exception as e:
                    log("football: live refresh", m["id"], e)
                time.sleep(gap)

        live = publish(rows)
        if live:
            log("football: live -", "; ".join(
                f"{m['home']} {m['home_score']}-{m['away_score']} {m['away']}" for m in live))

        time.sleep(live_every if live else min(refresh, 300))


# --------------------------------------------------------------- spotify
# Reads what's playing on your account. Uses the Authorization Code flow
# with PKCE, so there's no client secret to keep anywhere.

SPOTIFY_SCOPES = ("user-read-currently-playing user-read-playback-state "
                  "user-modify-playback-state "
                  "user-read-recently-played user-top-read user-library-read "
                  "playlist-read-private playlist-read-collaborative")
STATS_SCOPES = ("user-read-recently-played", "user-top-read")
LIBRARY_SCOPE = "user-library-read"
QUEUE_SCOPE = "user-modify-playback-state"
PLAYLIST_SCOPE = "playlist-read-private"
TOKEN_PATH = os.path.join(USER_DIR, "spotify_token.json")
_pkce = {}


def spotify_redirect_uri():
    return f"http://127.0.0.1:{cfg('port', 8765)}/callback"


def _load_tokens():
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_tokens(d):
    try:
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception as e:
        log("spotify: could not save token:", e)


def spotify_login_url():
    """Build the consent URL and remember the PKCE verifier for the callback."""
    import base64
    import hashlib
    import secrets

    client_id = cfg("spotify_client_id")
    if not client_id:
        return None

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    _pkce["verifier"] = verifier

    q = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": spotify_redirect_uri(),
        "scope": SPOTIFY_SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    })
    return "https://accounts.spotify.com/authorize?" + q


def _token_request(fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def spotify_exchange_code(code):
    client_id = cfg("spotify_client_id")
    data = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": spotify_redirect_uri(),
        "client_id": client_id,
        "code_verifier": _pkce.get("verifier", ""),
    })
    if "refresh_token" not in data:
        raise RuntimeError(data.get("error_description") or str(data))
    data["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 60
    _save_tokens(data)
    return data


def spotify_access_token():
    tok = _load_tokens()
    if not tok.get("refresh_token"):
        return None
    if tok.get("access_token") and time.time() < tok.get("expires_at", 0):
        return tok["access_token"]

    fresh = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": cfg("spotify_client_id"),
    })
    if "access_token" not in fresh:
        log("spotify: refresh failed:", fresh)
        return None
    tok["access_token"] = fresh["access_token"]
    tok["expires_at"] = time.time() + int(fresh.get("expires_in", 3600)) - 60
    if fresh.get("refresh_token"):
        tok["refresh_token"] = fresh["refresh_token"]
    _save_tokens(tok)
    return tok["access_token"]


def spotify_now_playing():
    """Returns a track dict, or None when nothing is playing."""
    token = spotify_access_token()
    if not token:
        return None

    req = urllib.request.Request(
        "https://api.spotify.com/v1/me/player?additional_types=track",
        headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status == 204:          # nothing playing
                return None
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code in (204, 404):
            return None
        raise
    if not raw.strip():
        return None

    j = json.loads(raw)
    item = j.get("item") or {}
    if not item:
        return None

    album = item.get("album") or {}
    images = album.get("images") or []
    art = images[0].get("url", "") if images else ""
    artists = ", ".join(a.get("name", "") for a in (item.get("artists") or []))

    return {
        "title": item.get("name", ""),
        "artist": artists,
        "album": album.get("name", ""),
        "art": art,
        "ts": time.time(),
        "source": "spotify",
        "progress_ms": j.get("progress_ms") or 0,
        "duration_ms": item.get("duration_ms") or 0,
        "uri": item.get("uri", ""),
        "is_playing": bool(j.get("is_playing")),
        "volume": (j.get("device") or {}).get("volume_percent"),
        "supports_volume": bool((j.get("device") or {}).get("supports_volume", True)),
        "device": (j.get("device") or {}).get("name", ""),
    }


LAST_TRACK = os.path.join(USER_DIR, "last_track.json")

def remember_track(t):
    """Keep the last thing that played, so an idle dashboard can offer it back."""
    if not t or not t.get("title"):
        return
    keep = {k: t.get(k) for k in ("title", "artist", "album", "art", "uri",
                                  "source", "duration_ms")}
    keep["at"] = time.time()
    cur = get_state().get("last_track") or {}
    if cur.get("title") == keep["title"] and cur.get("artist") == keep["artist"]:
        keep["at"] = cur.get("at", keep["at"])
        if t.get("source") == cur.get("source"):
            return
    set_state(last_track=keep)
    record_play(keep)
    try:
        with open(LAST_TRACK, "w", encoding="utf-8") as f:
            json.dump(keep, f)
    except Exception:
        pass

def load_last_track():
    try:
        with open(LAST_TRACK, "r", encoding="utf-8") as f:
            set_state(last_track=json.load(f))
    except Exception:
        pass


_PL_CACHE = {"at": 0, "items": []}

def spotify_playlists(force=False):
    """Your playlists, for the settings picker. Kept ten minutes - the list
    barely changes, and the picker asks every time it opens."""
    if PLAYLIST_SCOPE not in (_load_tokens().get("scope") or ""):
        return {"ok": False, "why": "scope"}
    if not force and _PL_CACHE["items"] and time.time() - _PL_CACHE["at"] < 600:
        return {"ok": True, "items": _PL_CACHE["items"]}
    items, url = [], "https://api.spotify.com/v1/me/playlists?limit=50"
    try:
        while url and len(items) < 300:
            _, j = _spotify_call("GET", url)
            for p in (j.get("items") or []):
                if not p:
                    continue
                imgs = p.get("images") or []
                items.append({
                    "uri": p.get("uri", ""),
                    "name": p.get("name", ""),
                    "owner": ((p.get("owner") or {}).get("display_name") or ""),
                    "tracks": ((p.get("tracks") or {}).get("total") or 0),
                    # the smallest that's still crisp at 48px on a 2x screen
                    "image": (imgs[-1] if len(imgs) == 1 else (imgs[1] if len(imgs) > 1 else {})).get("url", "")
                             if imgs else "",
                })
            url = j.get("next")
    except urllib.error.HTTPError as e:
        return {"ok": False, "why": "reconnect" if e.code in (401, 403) else str(e.code)}
    except Exception as e:
        return {"ok": False, "why": str(e)}
    _PL_CACHE.update(at=time.time(), items=items)
    # Keep the saved picks' covers current - a playlist's mosaic changes
    # whenever its first few songs do.
    picks = CFG.get("playlists") or []
    fresh = {p["uri"]: p for p in items}
    if any(fresh.get(p.get("uri"), {}).get("image", p.get("image")) != p.get("image") for p in picks):
        save_config({"playlists": [dict(p, image=fresh.get(p.get("uri"), {}).get("image", p.get("image")))
                                   for p in picks]})
    return {"ok": True, "items": items}


def play_playlist(uri, shuffle=False):
    """Start a playlist, in order or shuffled. Spotify's own shuffle still
    opens on track one, so a shuffled start jumps to a random track too."""
    if not uri.startswith("spotify:playlist:"):
        return False, "That isn't a playlist."
    base = "https://api.spotify.com/v1/me/player"
    body = {"context_uri": uri}
    if shuffle:
        total = next((p["tracks"] for p in _PL_CACHE["items"] if p["uri"] == uri), 0)
        if not total:
            try:
                _, j = _spotify_call("GET", "https://api.spotify.com/v1/playlists/"
                                     + uri.split(":")[-1] + "?fields=tracks.total")
                total = (j.get("tracks") or {}).get("total") or 0
            except Exception:
                total = 0
        if total > 1:
            import random
            body["offset"] = {"position": random.randrange(total)}
    def set_shuffle():
        try:
            _spotify_call("PUT", f"{base}/shuffle?state={'true' if shuffle else 'false'}", data=b"")
        except Exception:
            pass                      # no device yet - tried again after play
    set_shuffle()
    try:
        _spotify_call("PUT", base + "/play", data=json.dumps(body).encode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "Open Spotify on a device first."
        if e.code == 403:
            return False, "Spotify won't allow that (it needs Premium)."
        if e.code == 401:
            return False, "Reconnect Spotify."
        return False, f"Spotify said no ({e.code})."
    set_shuffle()                     # now there's surely an active device
    WAKE_SPOTIFY.set()
    return True, ""


def spotify_resume(uri=""):
    """Start the remembered track again (or just carry on if it's paused)."""
    body = None
    if uri:
        body = json.dumps({"uris": [uri]} if uri.startswith("spotify:track:")
                          else {"context_uri": uri}).encode()
    try:
        _spotify_call("PUT", "https://api.spotify.com/v1/me/player/play", data=body or b"")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "Open Spotify on a device first."
        if e.code == 403:
            return False, "Spotify won't allow that (it needs Premium)."
        return False, f"Spotify said no ({e.code})."
    WAKE_SPOTIFY.set()
    return True, ""


def spotify_control(action, value=None):
    """Play, pause or set volume on whatever device Spotify is using."""
    base = "https://api.spotify.com/v1/me/player"
    if action == "pause":
        url = base + "/pause"
    elif action == "play":
        url = base + "/play"
    elif action == "skipto":
        # Jump ahead to a queued song by skipping; the rest of the queue stays.
        n = max(1, min(20, int(value) + 1))
        try:
            for _ in range(n):
                _spotify_call("POST", base + "/next", data=b"")
                time.sleep(0.25)
        except urllib.error.HTTPError as e:
            return False, "No active Spotify device." if e.code == 404 else f"Spotify said no ({e.code})."
        WAKE_SPOTIFY.set()
        return True, ""
    elif action == "volume":
        v = max(0, min(100, int(value)))
        log(f"spotify: volume set to {v} from the dashboard")
        url = f"{base}/volume?volume_percent={v}"
    else:
        return False, "Unknown control."
    try:
        _spotify_call("PUT", url, data=b"")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "No active Spotify device."
        if e.code == 403:
            try:
                reason = json.loads(e.read().decode()).get("error", {}).get("reason", "")
            except Exception:
                reason = ""
            if reason == "VOLUME_CONTROL_DISALLOW":
                return False, "This device doesn't allow volume control."
            return False, "Spotify won't allow that (it needs Premium)."
        if e.code == 401:
            return False, "Reconnect Spotify."
        return False, f"Spotify said no ({e.code})."
    WAKE_SPOTIFY.set()
    return True, ""


def spotify_devices():
    _, j = _spotify_call("GET", "https://api.spotify.com/v1/me/player/devices")
    return [{"id": d.get("id"), "name": d.get("name", ""), "type": d.get("type", ""),
             "active": bool(d.get("is_active"))}
            for d in (j.get("devices") or []) if d.get("id") and not d.get("is_restricted")]


def spotify_transfer(device_id):
    # Carry the current volume over, so switching to a device left at 90% isn't a jump scare.
    prev = None
    try:
        _, j = _spotify_call("GET", "https://api.spotify.com/v1/me/player")
        prev = (j.get("device") or {}).get("volume_percent")
    except Exception:
        pass
    try:
        _spotify_call("PUT", "https://api.spotify.com/v1/me/player",
                      data=json.dumps({"device_ids": [device_id], "play": True}).encode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "That device isn't available right now."
        return False, f"Spotify said no ({e.code})."
    if prev is not None:
        time.sleep(1.5)
        try:
            _spotify_call("PUT", f"https://api.spotify.com/v1/me/player/volume?volume_percent={prev}&device_id={device_id}", data=b"")
            log(f"spotify: moved playback, kept volume at {prev}")
        except Exception:
            pass
    WAKE_SPOTIFY.set()
    return True, ""


def spotify_queue(limit=3):
    """What's coming up next. Note Spotify returns the upcoming songs of the
    album or playlist too, not only ones added by hand."""
    token = spotify_access_token()
    if not token:
        return []
    req = urllib.request.Request("https://api.spotify.com/v1/me/player/queue",
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.read().decode("utf-8")
    j = json.loads(raw) if raw.strip() else {}
    out = []
    for t in (j.get("queue") or [])[:limit]:
        if not t or t.get("type") not in (None, "track"):
            continue
        imgs = (t.get("album") or {}).get("images") or []
        req_by = GUEST_REQUESTS.get(t.get("uri"))
        out.append({
            "title": t.get("name", ""),
            "uri": t.get("uri", ""),
            "artist": ", ".join(a.get("name", "") for a in t.get("artists") or []),
            "art": imgs[-1]["url"] if imgs else "",
            "requested_by": req_by["name"] if req_by else "",
        })
    return out


# ---------------------------------------------------------------- location

def locate():
    """Roughly where this Mac is, from its IP. Good enough for weather."""
    for url, pick in (
        ("http://ip-api.com/json/?fields=status,country,countryCode,city,lat,lon",
         lambda j: (j.get("city"), j.get("lat"), j.get("lon"), j.get("countryCode"))),
        ("https://ipapi.co/json/",
         lambda j: (j.get("city"), j.get("latitude"), j.get("longitude"), j.get("country_code"))),
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "b-side/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.loads(r.read().decode())
            city, lat, lon, cc = pick(j)
            if lat and lon:
                return {"place": city or "", "latitude": float(lat),
                        "longitude": float(lon), "country": (cc or "").upper()}
        except Exception:
            continue
    return None


def geo_search(q, limit=8):
    """Cities to choose from while typing: name, region and country."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    try:
        url = ("https://geocoding-api.open-meteo.com/v1/search?count=%d&name=%s"
               % (limit, urllib.parse.quote(q)))
        with urllib.request.urlopen(url, timeout=12) as r:
            res = (json.loads(r.read().decode()).get("results") or [])
    except Exception as e:
        log("location: search failed -", e)
        return []
    out = []
    for g in res:
        bits = [g.get("admin1"), g.get("country")]
        out.append({
            "place": g.get("name", ""),
            "label": ", ".join([g.get("name", "")] + [b for b in bits if b]),
            "latitude": g.get("latitude"), "longitude": g.get("longitude"),
            "country": (g.get("country_code") or "").upper(),
        })
    return out


def geocode(name):
    """Turn what someone typed into a place and coordinates."""
    try:
        url = ("https://geocoding-api.open-meteo.com/v1/search?count=1&name="
               + urllib.parse.quote(name))
        with urllib.request.urlopen(url, timeout=10) as r:
            res = (json.loads(r.read().decode()).get("results") or [])
        if res:
            g = res[0]
            return {"place": g.get("name", name), "latitude": g.get("latitude"),
                    "longitude": g.get("longitude"),
                    "country": (g.get("country_code") or "").upper()}
    except Exception:
        pass
    return None


def ensure_location():
    """First run, or 'auto' left on: work out where we are."""
    if cfg("latitude") and cfg("longitude") and not cfg("auto_location"):
        return
    if cfg("latitude") and cfg("longitude") and cfg("place"):
        return
    got = locate()
    if got:
        save_config(got)
        log(f"location: {got['place'] or '?'} ({got['country']})")


# ------------------------------------------------------------ local player
# On a Mac the Music and Spotify apps are scriptable, so now-playing and the
# controls need no account, no keys and no internet. The Spotify web API is
# only used for the guest queue.

import subprocess

APPS = {"apple": "Music", "spotify": "Spotify"}
_ART_LOOKUP = {}

# --------------------------------------------------------- youtube music
# There's no YouTube Music app from Google and no public API for playback,
# so it's read wherever it happens to be running. Three ways in, best first:
# the desktop wrapper's own local API, then the page itself in a browser,
# then macOS's Now Playing as a floor that needs no setup at all.

YTM_URL = "music.youtube.com"
YTM_APP = "YouTube Music"                 # the th-ch/youtube-music wrapper
YTM_API = "http://127.0.0.1:26538"
CHROMIUM = ("Google Chrome", "Brave Browser", "Microsoft Edge", "Arc",
            "Vivaldi", "Chromium", "Google Chrome Canary", "Opera")

# Kept to one line and single quotes so it survives being wrapped in an
# AppleScript string. Reads the player bar, and the <video> for the numbers.
YTM_READ_JS = (
    "(function(){var v=document.querySelector('video');"
    "var b=document.querySelector('ytmusic-player-bar');"
    "if(!v||!b)return '';"
    "var t=b.querySelector('.title'),y=b.querySelector('.byline'),"
    "i=b.querySelector('img.image');"
    "return JSON.stringify({title:t?t.textContent.trim():'',"
    "byline:y?y.textContent.trim():'',art:i?i.src:'',"
    "pos:Math.round(v.currentTime*1000)||0,"
    "dur:Math.round((v.duration||0)*1000)||0,"
    "playing:!v.paused,vol:Math.round((v.volume||0)*100)});})()")


def _ytm_js(action, value=None):
    """The page can be driven through the same <video> element it reports
    from, which stays in step with YouTube Music's own buttons."""
    if action in ("play", "pause", "playpause"):
        want = {"play": "play", "pause": "pause", "playpause": "toggle"}[action]
        return ("(function(){var v=document.querySelector('video');if(!v)return '';"
                f"var w='{want}';"
                "if(w==='play'||(w==='toggle'&&v.paused))v.play();else v.pause();"
                "return 'ok';})()")
    if action == "volume":
        v = max(0, min(100, int(value or 0))) / 100.0
        return ("(function(){var v=document.querySelector('video');if(!v)return '';"
                f"v.volume={v:.2f};return 'ok';}})()")
    if action in ("next", "previous"):
        cls = "next-button" if action == "next" else "previous-button"
        return ("(function(){var b=document.querySelector('ytmusic-player-bar');"
                f"var x=b&&b.querySelector('.{cls}');"
                "if(!x)return '';x.click();return 'ok';})()")
    return None


def _ytm_browser_run(js):
    """Run a snippet in whichever running browser has YouTube Music open."""
    esc = js.replace("\\", "\\\\").replace('"', '\\"')
    for app in CHROMIUM:
        if not _app_running(app):
            continue
        out = _osa(f'''tell application "{app}"
            repeat with w in windows
                repeat with t in tabs of w
                    if URL of t contains "{YTM_URL}" then
                        return execute javascript "{esc}" in t
                    end if
                end repeat
            end repeat
        end tell''', timeout=8)
        if out:
            return out
    if _app_running("Safari"):
        out = _osa(f'''tell application "Safari"
            repeat with w in windows
                repeat with t in tabs of w
                    if URL of t contains "{YTM_URL}" then
                        return do JavaScript "{esc}" in t
                    end if
                end repeat
            end repeat
        end tell''', timeout=8)
        if out:
            return out
    return ""


def _ytm_api(path, method="GET", body=None):
    """The desktop wrapper's API server plugin, when it's switched on."""
    req = urllib.request.Request(YTM_API + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def _ytm_from_api():
    j = _ytm_api("/api/v1/song")
    if not j or not j.get("title"):
        return None
    return {"title": j.get("title", ""), "artist": j.get("artist", ""),
            "album": j.get("album", "") or "", "art": j.get("imageSrc", "") or "",
            "progress_ms": int(j.get("elapsedSeconds") or 0) * 1000,
            "duration_ms": int(j.get("songDuration") or 0) * 1000,
            "is_playing": not j.get("isPaused", False),
            "uri": j.get("url", ""), "volume": 50, "supports_volume": True}


def _ytm_from_browser():
    raw = _ytm_browser_run(YTM_READ_JS)
    if not raw:
        return None
    try:
        j = json.loads(raw)
    except Exception:
        return None
    if not j.get("title"):
        return None
    bits = [p.strip() for p in (j.get("byline") or "").split("•")]
    # The player bar only needs a thumbnail, so its art comes through at about
    # 60px. Same image, bigger crop.
    art = re.sub(r"=w\d+-h\d+", "=w544-h544", j.get("art", "") or "")
    return {"title": j["title"], "artist": bits[0] if bits else "",
            "album": bits[1] if len(bits) > 1 else "",
            "art": art,
            "progress_ms": int(j.get("pos") or 0),
            "duration_ms": int(j.get("dur") or 0),
            "is_playing": bool(j.get("playing")),
            "uri": "", "volume": int(j.get("vol") or 50),
            "supports_volume": True}


# JXA, because the plain C entry point stopped answering third parties in
# macOS 15.4. No artwork comes back either way, so iTunes fills that in.
YTM_NOWPLAYING_JXA = '''
ObjC.import("Foundation");
var b = $.NSBundle.bundleWithPath("/System/Library/PrivateFrameworks/MediaRemote.framework");
if (!b || !b.load) { "" } else {
  b.load;
  var req = $.NSClassFromString("MRNowPlayingRequest");
  if (!req) { "" } else {
    var info = req.localNowPlayingItem.nowPlayingInfo;
    if (!info) { "" } else {
      var g = function(k){ var v = info.objectForKey(k); return v ? String(v.js || v) : ""; };
      JSON.stringify({
        title: g("kMRMediaRemoteNowPlayingInfoTitle"),
        artist: g("kMRMediaRemoteNowPlayingInfoArtist"),
        album: g("kMRMediaRemoteNowPlayingInfoAlbum"),
        dur: g("kMRMediaRemoteNowPlayingInfoDuration"),
        pos: g("kMRMediaRemoteNowPlayingInfoElapsedTime"),
        rate: g("kMRMediaRemoteNowPlayingInfoPlaybackRate")
      })
    }
  }
}
'''


def _osa_js(script, timeout=8):
    if sys.platform != "darwin":
        return ""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _ytm_from_system():
    raw = _osa_js(YTM_NOWPLAYING_JXA)
    if not raw:
        return None
    try:
        j = json.loads(raw)
    except Exception:
        return None
    if not j.get("title"):
        return None

    def num(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    return {"title": j["title"], "artist": j.get("artist", ""),
            "album": j.get("album", ""), "art": "",
            "progress_ms": int(num(j.get("pos")) * 1000),
            "duration_ms": int(num(j.get("dur")) * 1000),
            "is_playing": num(j.get("rate")) > 0,
            "uri": "", "volume": 50, "supports_volume": False}


_YTM_ROUTE = {"name": "", "at": 0}


def ytm_now_playing():
    """Whichever route answers. The winner is remembered for a minute so a
    steady setup isn't paying for three lookups every couple of seconds."""
    routes = [("desktop app", _ytm_from_api),
              ("browser", _ytm_from_browser),
              ("now playing", _ytm_from_system)]
    if _YTM_ROUTE["name"] and time.time() - _YTM_ROUTE["at"] < 60:
        routes.sort(key=lambda r: r[0] != _YTM_ROUTE["name"])
    for name, fn in routes:
        try:
            t = fn()
        except Exception:
            continue
        if t:
            if _YTM_ROUTE["name"] != name:
                log("youtube music: reading from the", name)
            _YTM_ROUTE.update(name=name, at=time.time())
            if not t.get("art"):
                t["art"] = art_soon(t["artist"], t["title"], t.get("album", ""))
            t.update(ts=time.time(), source="ytmusic", device="This Mac")
            return t
    return None


def ytm_control(action, value=None):
    try:
        if _YTM_ROUTE["name"] == "desktop app" or _ytm_running():
            path = {"play": "/api/v1/play", "pause": "/api/v1/pause",
                    "playpause": "/api/v1/toggle-play", "next": "/api/v1/next",
                    "previous": "/api/v1/previous",
                    "volume": "/api/v1/volume"}.get(action)
            if path:
                _ytm_api(path, "POST",
                         {"volume": int(value or 0)} if action == "volume" else None)
                return True, ""
    except Exception:
        pass                          # the API plugin may simply be switched off
    js = _ytm_js(action, value)
    if not js:
        return False, "Unknown control."
    if _ytm_browser_run(js):
        return True, ""
    return False, ("Open YouTube Music in a browser tab, or switch on the "
                   "desktop app's API server.")


def _ytm_running():
    return _app_running(YTM_APP)


def _osa(script, timeout=6):
    """Run a snippet of AppleScript. Returns "" when the app isn't there."""
    if sys.platform != "darwin":
        return ""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _app_running(app):
    return _osa(f'tell application "System Events" to (name of processes) contains "{app}"') == "true"


# ------------------------------------------------------------- windows
# Windows has no AppleScript, but it does have the media layer behind the
# volume-key overlay: whatever is playing registers there, so one road covers
# Spotify's app, YouTube Music in any browser, and anything else.

WIN_APPS = {
    "spotify": ("spotify",),
    "ytmusic": ("chrome", "msedge", "brave", "vivaldi", "opera", "firefox",
                "arc", "youtube"),
    "apple": ("applemusic", "itunes", "apple.music"),
}


def _win_media():
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Mgr)
        return Mgr
    except Exception:
        return None


async def _win_session(Mgr, service):
    """Prefer the app they actually chose; fall back to whatever has the
    system's attention."""
    mgr = await Mgr.request_async()
    want = WIN_APPS.get(service) or ()
    try:
        for s in (mgr.get_sessions() or []):
            aid = (s.source_app_user_model_id or "").lower()
            if any(w in aid for w in want):
                return s
    except Exception:
        pass
    return mgr.get_current_session()


def _win_now_playing(service):
    Mgr = _win_media()
    if not Mgr:
        return None
    import asyncio

    async def grab():
        s = await _win_session(Mgr, service)
        if s is None:
            return None
        p = await s.try_get_media_properties_async()
        title = (p.title or "").strip()
        if not title:
            return None
        artist = (p.artist or "").strip()
        album = (getattr(p, "album_title", "") or "").strip()
        pos = dur = 0
        try:
            tl = s.get_timeline_properties()
            pos = int(tl.position.total_seconds() * 1000)
            dur = int(tl.end_time.total_seconds() * 1000)
        except Exception:
            pass
        playing = True
        try:
            playing = int(s.get_playback_info().playback_status) == 4   # PLAYING
        except Exception:
            pass
        return {"title": title, "artist": artist, "album": album,
                "art": art_soon(artist, title, album),
                "ts": time.time(), "source": service,
                "progress_ms": max(0, pos), "duration_ms": max(0, dur),
                "uri": "", "is_playing": playing,
                # Windows' media layer can't set volume, but with Spotify
                # linked the control falls through to Spotify's own API - so
                # the bars stay live. No made-up level either: a fixed 50
                # snapped the bars back to the middle on every poll.
                "volume": None,
                "supports_volume": service == "spotify"
                                   and bool(_load_tokens().get("refresh_token")),
                "device": "This PC"}

    try:
        return asyncio.run(grab())
    except Exception as e:
        log("windows media:", e)
        return None


def _win_control(service, action, value=None):
    Mgr = _win_media()
    if not Mgr:
        return False, "Windows media controls aren't available."
    if action == "volume":
        # Not something this layer exposes; Spotify's own API picks it up.
        return False, ""
    import asyncio

    async def go():
        s = await _win_session(Mgr, service)
        if s is None:
            return False
        fn = {"play": s.try_play_async, "pause": s.try_pause_async,
              "playpause": s.try_toggle_play_pause_async,
              "next": s.try_skip_next_async,
              "previous": s.try_skip_previous_async}.get(action)
        if not fn:
            return False
        await fn()
        return True

    try:
        ok = asyncio.run(go())
    except Exception as e:
        return False, str(e)
    return (True, "") if ok else (False, "Nothing is playing to control.")


def local_now_playing(service):
    """What the Music or Spotify app is playing, via AppleScript."""
    if sys.platform == "win32":
        return _win_now_playing(service)
    if service == "ytmusic":
        return ytm_now_playing()
    app = APPS.get(service)
    if not app or not _app_running(app):
        return None
    sep = "\u241f"
    if service == "spotify":
        script = f'''tell application "{app}"
            if player state is stopped then return ""
            set t to current track
            return (name of t) & "{sep}" & (artist of t) & "{sep}" & (album of t) & "{sep}" & ((duration of t) as text) & "{sep}" & ((player position) as text) & "{sep}" & (player state as text) & "{sep}" & (sound volume as text) & "{sep}" & (artwork url of t) & "{sep}" & (spotify url of t)
        end tell'''
    else:
        script = f'''tell application "{app}"
            if player state is stopped then return ""
            set t to current track
            return (name of t) & "{sep}" & (artist of t) & "{sep}" & (album of t) & "{sep}" & ((duration of t) as text) & "{sep}" & ((player position) as text) & "{sep}" & (player state as text) & "{sep}" & (sound volume as text) & "{sep}" & "" & "{sep}" & ""
        end tell'''
    out = _osa(script)
    if not out:
        return None
    parts = out.split("\u241f")
    if len(parts) < 7:
        return None
    title, artist, album, dur, pos, state, vol = parts[:7]
    art = parts[7] if len(parts) > 7 else ""
    uri = parts[8] if len(parts) > 8 else ""

    def num(x, d=0.0):
        try:
            return float(str(x).replace(",", "."))
        except Exception:
            return d

    dur_ms = int(num(dur) * (1 if service == "spotify" and num(dur) > 1000 else 1000))
    if service == "spotify" and num(dur) > 1000:     # Spotify reports milliseconds
        dur_ms = int(num(dur))
    if not art:
        art = art_soon(artist, title, album)
    return {
        "title": title, "artist": artist, "album": album, "art": art,
        "ts": time.time(), "source": service,
        "progress_ms": int(num(pos) * 1000), "duration_ms": dur_ms,
        "uri": uri, "is_playing": state.lower() == "playing",
        "volume": int(num(vol, 50)), "supports_volume": True,
        "device": "This Mac",
    }


def art_for(artist, title, album):
    """Apple Music's AppleScript won't hand over artwork, so look it up."""
    key = (artist + "|" + (album or title)).lower()
    if key in _ART_LOOKUP:
        return _ART_LOOKUP[key]
    url = ""
    try:
        first = (artist or "").split(",")[0].split("&")[0].strip()
        q = urllib.parse.quote(f"{first} {album or title}")
        req = urllib.request.Request(
            f"https://itunes.apple.com/search?entity=album&limit=1&term={q}",
            headers={"User-Agent": "b-side/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read().decode()).get("results") or []
        if res and res[0].get("artworkUrl100"):
            url = res[0]["artworkUrl100"].replace("100x100bb", "1000x1000bb")
    except Exception:
        pass
    _ART_LOOKUP[key] = url
    return url


def art_soon(artist, title, album):
    """Cached artwork straight away; anything else is looked up in the
    background and patched in when it lands. A cold iTunes reply used to sit
    in the middle of the polling loop and hold the whole track change up."""
    key = (artist + "|" + (album or title)).lower()
    if key in _ART_LOOKUP:
        return _ART_LOOKUP[key]

    def fill():
        try:
            url = art_for(artist, title, album)
        except Exception:
            return
        cur = get_state().get("now_playing") or {}
        if url and cur.get("title") == title and cur.get("artist") == artist \
                and not cur.get("art"):
            set_state(now_playing=dict(cur, art=url))

    threading.Thread(target=fill, daemon=True).start()
    return ""


def local_control(service, action, value=None):
    if sys.platform == "win32":
        return _win_control(service, action, value)
    if service == "ytmusic":
        return ytm_control(action, value)
    app = APPS.get(service)
    if not app:
        return False, "No music app chosen yet."
    if not _app_running(app):
        return False, f"{app} isn't open."
    if action in ("play", "pause", "playpause"):
        cmd = {"play": "play", "pause": "pause", "playpause": "playpause"}[action]
        _osa(f'tell application "{app}" to {cmd}')
    elif action == "volume":
        _osa(f'tell application "{app}" to set sound volume to {max(0, min(100, int(value)))}')
    elif action == "next":
        _osa(f'tell application "{app}" to next track')
    elif action == "previous":
        _osa(f'tell application "{app}" to previous track')
    else:
        return False, "Unknown control."
    WAKE_SPOTIFY.set()
    return True, ""


def open_player(service):
    """Clicking the turntable: bring the music app up and start it playing."""
    if sys.platform == "win32":
        target = {"spotify": "spotify:", "apple": "musics:",
                  "ytmusic": f"https://{YTM_URL}"}.get(service)
        if not target:
            return False, "Pick a music service in settings first."
        try:
            os.startfile(target)        # noqa: whatever is registered for it
        except Exception as e:
            return False, str(e)
        time.sleep(1.5)
        _win_control(service, "play")
        return True, ""
    if service == "ytmusic":
        if _ytm_running():
            _osa(f'tell application "{YTM_APP}" to activate')
        else:
            # Whatever already has the tab open wins; otherwise the default
            # browser opens it fresh.
            for app in CHROMIUM + ("Safari",):
                if _app_running(app) and _ytm_browser_run("'ok'"):
                    _osa(f'tell application "{app}" to activate')
                    break
            else:
                _osa(f'open location "https://{YTM_URL}"')
                time.sleep(2.5)
        time.sleep(0.6)
        ytm_control("play")
        return True, ""
    app = APPS.get(service)
    if not app:
        return False, "Pick a music service in settings first."
    _osa(f'tell application "{app}" to activate')
    time.sleep(0.6)
    _osa(f'tell application "{app}" to play')
    return True, ""


_WEB_POS = {"key": None, "ms": 0, "dur": 0, "at": 0.0, "playing": None}

def _borrow_position(track):
    """Fill progress_ms / duration_ms from the Spotify web API. Asks at most
    every ten seconds (and straight away on a new song); in between it runs
    the clock forward itself, which is all the page does anyway."""
    key = (track.get("title") or "").strip().lower()
    now = time.time()
    playing = bool(track.get("is_playing", True))
    if (key != _WEB_POS["key"] or now - _WEB_POS["at"] > 10
            or playing != _WEB_POS["playing"]):        # pause/resume: re-read now
        try:
            api = spotify_now_playing()
        except Exception as e:
            api = None
            log("spotify web (position):", e)
        if api and (api.get("title") or "").strip().lower() == key and api.get("duration_ms"):
            _WEB_POS.update(key=key, ms=int(api.get("progress_ms") or 0),
                            dur=int(api["duration_ms"]), at=now, playing=playing)
        elif key != _WEB_POS["key"]:
            _WEB_POS.update(key=None)
            return
    if _WEB_POS["key"] != key:
        return
    ms = _WEB_POS["ms"]
    if playing:
        ms += int((now - _WEB_POS["at"]) * 1000)
    track["duration_ms"] = _WEB_POS["dur"]
    track["progress_ms"] = min(ms, _WEB_POS["dur"])


def player_loop():
    """Follow whichever app the person chose, every couple of seconds."""
    load_last_track()
    if (CFG.get("guest_queue") or {}).get("enabled"):
        log(f"guest: QR points at {guest_url()}")
    poll = cfg("player_poll_seconds", 2)
    was_playing = False
    last_queue = 0

    while True:
        try:
            service = cfg("service", "spotify")
            track = local_now_playing(service)

            # Spotify connected to the web API: use it when the desktop app
            # isn't running (playing on a phone or a speaker, say).
            if not track and service == "spotify" and _load_tokens().get("refresh_token"):
                try:
                    track = spotify_now_playing()
                except Exception as e:
                    log("spotify web:", e)

            # Spotify's Windows app names the song to the system but often
            # leaves position and length at zero - so no lyric line can light
            # up and the arm can't travel. Borrow them from the web API.
            if (track and not track.get("duration_ms") and service == "spotify"
                    and _load_tokens().get("refresh_token")):
                _borrow_position(track)

            if service == "spotify":
                granted = _load_tokens().get("scope", "")
                connected = bool(_load_tokens().get("refresh_token"))
                set_state(spotify_needs_login=not connected,
                          guest_ready=connected and QUEUE_SCOPE in granted)
            else:
                set_state(spotify_needs_login=False, guest_ready=False)

            paused = track if (track and not track.get("is_playing")) else None
            playing = track if (track and track.get("is_playing")) else None
            set_state(spotify_paused=paused)

            if playing:
                cur = get_state().get("now_playing") or {}
                same = (cur.get("title") == playing["title"]
                        and cur.get("artist") == playing["artist"])
                if same:
                    playing["ts"] = cur.get("ts", playing["ts"])
                else:
                    # The record that just came off, for the left of the deck.
                    if cur.get("title"):
                        set_state(prev_track={k: cur.get(k) for k in
                                              ("title", "artist", "album", "art", "uri")})
                    remember_track(playing)
                    log("playing:", playing["artist"], "-", playing["title"],
                        f"[{service}]")
                req_by = GUEST_REQUESTS.get(playing.get("uri"))
                if req_by:
                    playing["requested_by"] = req_by["name"]
                set_state(now_playing=playing, spotify_playing=True)
                if service == "spotify" and _load_tokens().get("refresh_token") and (
                        not same or time.time() - last_queue > 15):
                    try:
                        set_state(queue=spotify_queue())
                    except Exception:
                        pass
                    last_queue = time.time()
                was_playing = True
            else:
                if was_playing:
                    set_state(now_playing=None, queue=[])
                    was_playing = False
                set_state(spotify_playing=False)
        except Exception as e:
            log("player:", e)

        # A flat two seconds means a new song can sit unnoticed for two
        # seconds. We know when this one runs out, so watch closely as it
        # gets there and go back to idling once the next one has landed.
        nap_for = poll
        try:
            np_ = get_state().get("now_playing")
            if np_ and np_.get("duration_ms") and np_.get("is_playing"):
                left = (np_["duration_ms"] - (np_.get("progress_ms") or 0)) / 1000.0 \
                    - (time.time() - (np_.get("ts") or time.time()))
                if left < 4:
                    nap_for = 0.35
        except Exception:
            pass
        if WAKE_SPOTIFY.wait(nap_for):
            WAKE_SPOTIFY.clear()


import socket  # WiZ bulbs talk straight to the LAN

# ------------------------------------------------------------------- mic

def clean_clip(audio, rate, np):
    """Strip low-frequency rumble and normalise level.

    A laptop mic a few feet from the speakers picks up far more desk and fan
    rumble than music. Fingerprinting keys off mid-range spectral peaks, so
    the bass has to go before the clip is worth sending anywhere.
    """
    x = np.asarray(audio, dtype=np.float32).flatten()
    if x.size == 0:
        return x

    spec = np.fft.rfft(x)
    freq = np.fft.rfftfreq(x.size, 1.0 / rate)

    # Roll off below 150 Hz rather than cutting a hole in the spectrum.
    ramp = np.clip((freq - 60.0) / 180.0, 0.0, 1.0) ** 2
    spec *= ramp
    # Nothing musical up there, and it's where mic hiss lives.
    spec[freq > 15000.0] = 0.0

    y = np.fft.irfft(spec, n=x.size).astype(np.float32)

    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 1e-6:
        y = y * (0.89 / peak)
    return y


def to_wav_bytes(samples, rate, np):
    import io
    import wave as _wave
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# Records a short clip from the mic, sends it to AudD, stores the match.

class ConfigError(RuntimeError):
    """Something the user has to fix: a missing package, a dead token."""


def shazam_recognize(wav_bytes):
    """Identify a clip via Shazam's own catalogue.

    Uses shazamio, which reverse-engineers the Shazam app's API. No key and
    no quota, but it is unofficial: if Shazam changes something it can stop
    working, and there's nobody to complain to.
    """
    import asyncio

    try:
        from shazamio import Shazam
    except ImportError:
        raise ConfigError("Run: pip install shazamio")

    async def go():
        return await Shazam().recognize(wav_bytes)

    # The clip just finished recording, so "now" is where the clip ended.
    ended_at = time.time()

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        data = loop.run_until_complete(go())
    finally:
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(None)

    track = (data or {}).get("track") or {}
    if not track:
        return None

    album = ""
    try:
        for section in (track.get("sections") or []):
            for row in (section.get("metadata") or []):
                if (row.get("title") or "").lower() == "album":
                    album = row.get("text", "")
                    break
            if album:
                break
    except Exception:
        pass

    art = ""
    try:
        imgs = track.get("images") or {}
        art = imgs.get("coverarthq") or imgs.get("coverart") or ""
    except Exception:
        pass

    out = {
        "title": track.get("title", ""),
        "artist": track.get("subtitle", ""),
        "album": album,
        "art": art,
        "ts": time.time(),
        "source": "shazam",
    }
    # Where in the song the clip was, and how long the song is, so the
    # tonearm can move and the next listen can wait for the song to end.
    try:
        off = float(((data.get("matches") or [{}])[0]).get("offset") or 0)
        clip = float(cfg("listen_clip_seconds", 7))
        dur = track_length_ms(out["artist"], out["title"])
        if off > 0 and dur:
            pos = (off + clip) * 1000
            if pos < dur:
                out.update(duration_ms=dur, progress_ms=int(pos), progress_at=ended_at)
    except Exception as e:
        log(f"listen: couldn't place the needle ({e})")
    return out


_LEN_CACHE = {}

def track_length_ms(artist, title):
    """Song length from Spotify's catalogue, falling back to Apple's."""
    key = (artist + "|" + title).lower()
    if key in _LEN_CACHE:
        return _LEN_CACHE[key]
    first = (artist or "").split(",")[0].split("&")[0].split(" feat")[0].strip()
    ms = 0
    try:
        q = urllib.parse.quote(f"track:{title} artist:{first}")
        _, j = _spotify_call("GET", f"https://api.spotify.com/v1/search?type=track&limit=1&q={q}")
        items = (j.get("tracks") or {}).get("items") or []
        if items:
            ms = int(items[0].get("duration_ms") or 0)
    except Exception:
        pass
    if not ms:
        try:
            q = urllib.parse.quote(f"{first} {title}")
            req = urllib.request.Request(f"https://itunes.apple.com/search?entity=song&limit=1&term={q}",
                                         headers={"User-Agent": "desk-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read().decode()).get("results") or []
            if res:
                ms = int(res[0].get("trackTimeMillis") or 0)
        except Exception:
            pass
    _LEN_CACHE[key] = ms
    return ms


def recognize(wav_bytes):
    """Run whichever recognizer the config asks for.

    'shazam'  - Shazam only (free, unofficial)
    'audd'    - AudD only (needs a paid or trial token)
    'both'    - Shazam first, AudD as a backstop
    """
    mode = (cfg("recognizer", "shazam") or "shazam").lower()
    token = cfg("audd_token")

    if mode == "audd":
        if not token:
            raise ConfigError("No AudD token set")
        return audd_recognize(wav_bytes, token)

    result = shazam_recognize(wav_bytes)
    if result or mode != "both":
        return result

    if token:
        return audd_recognize(wav_bytes, token)
    return None


def _audio_libs():
    import numpy as np
    import sounddevice as sd
    return np, sd


def can_identify():
    """True when a click could actually name a record: the audio libraries are
    installed and some recognizer is reachable. Worked out once - importing
    sounddevice is slow, and the answer doesn't change while we're running."""
    global _CAN_IDENTIFY
    if _CAN_IDENTIFY is None:
        try:
            _audio_libs()
            mode = (cfg("recognizer", "shazam") or "shazam").lower()
            if mode == "audd" and not cfg("audd_token"):
                _CAN_IDENTIFY = False
            else:
                _CAN_IDENTIFY = True
        except Exception:
            _CAN_IDENTIFY = False
    return _CAN_IDENTIFY


def identify_now():
    """Record one clip and ask AudD what it is. Returns (result, note)."""
    try:
        np, sd = _audio_libs()
    except ImportError:
        return None, "Run: pip install sounddevice numpy"

    rate = 44100
    clip = cfg("listen_clip_seconds", 12)
    device = cfg("mic_device", None)
    threshold = cfg("silence_threshold", 0.004)
    stamp = datetime.now().strftime("%H:%M")

    audio = sd.rec(int(clip * rate), samplerate=rate,
                   channels=1, dtype="float32", device=device)
    sd.wait()

    level = float(np.sqrt(np.mean(np.square(audio))))
    if level < threshold:
        return None, f"too quiet ({level:.4f}) at {stamp}"

    wav = to_wav_bytes(clean_clip(audio, rate, np), rate, np)
    result = recognize(wav)               # raises on auth/quota trouble
    if result:
        return result, f"matched at {stamp}"
    return None, f"no match at {stamp}"


def listen_loop():
    try:
        _listen_loop()
    except Exception as e:
        set_state(listen_error=f"listener stopped: {e}", listening=False)
        log("listen: THREAD DIED:", repr(e))


def _listen_loop():
    if not cfg("listen", False):
        print("listen: click-to-identify mode")
        return

    mode = (cfg("recognizer", "shazam") or "shazam").lower()
    if mode == "audd" and not cfg("audd_token"):
        set_state(listen_error="No AudD token in config.json")
        print("listen: no audd_token, skipping")
        return

    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        set_state(listen_error="Run: pip install sounddevice numpy")
        print("listen: pip install sounddevice numpy")
        return

    import io
    import wave

    interval = cfg("listen_interval_seconds", 45)
    clip = cfg("listen_clip_seconds", 12)
    device = cfg("mic_device", None)
    threshold = cfg("silence_threshold", 0.004)
    forget = cfg("forget_after_minutes", 8)
    # Seconds. Short enough that a track change isn't stale for long,
    # long enough not to re-identify the same song constantly.
    recheck = cfg("recheck_after_seconds", 45)
    rate = 44100

    set_state(listening=True)
    log(f"listen: on, every {interval}s, {clip}s clips, device={device}, "
        f"recognizer={mode}")

    while True:
        try:
            # Spotify knows better than the mic ever will.
            if get_state().get("spotify_playing"):
                nap(interval)
                continue

            set_state(listen_note="listening\u2026")
            audio = sd.rec(int(clip * rate), samplerate=rate,
                           channels=1, dtype="float32", device=device)
            sd.wait()

            level = float(np.sqrt(np.mean(np.square(audio))))
            stamp = datetime.now().strftime("%H:%M")

            # Something is already identified and still recent: a track runs
            # for minutes, so there's no sense asking again every pass.
            if level >= threshold:
                cur = get_state().get("now_playing")
                if cur:
                    left = recheck - (time.time() - cur.get("ts", 0))
                    if cur.get("duration_ms") and cur.get("progress_at") and cur.get("ts"):
                        ends = cur["progress_at"] + (cur["duration_ms"] - cur["progress_ms"]) / 1000
                        left = min(ends + 4 - time.time(), 15 * 60)
                    if left > 0:
                        mins, secs = divmod(int(left), 60)
                        when = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
                        set_state(listen_note=f"playing \u00b7 recheck in {when}")
                        nap(interval)
                        continue

            if level < threshold:
                # A silent pass means the side ended or the needle moved:
                # drop the hold so the next sound is identified at once.
                cur0 = get_state().get("now_playing")
                if cur0:
                    cur0 = dict(cur0); cur0["ts"] = 0
                    set_state(now_playing=cur0)
                set_state(listen_note=f"quiet ({level:.4f}) at {stamp}")
                # Room is quiet. Let the record fall off after a while.
                np_now = get_state().get("now_playing")
                if np_now:
                    age = time.time() - np_now.get("ts", 0)
                    if age > forget * 60:
                        set_state(now_playing=None)
                nap(interval)
                continue

            wav = to_wav_bytes(clean_clip(audio, rate, np), rate, np)

            try:
                result = recognize(wav)
            except ConfigError as e:
                # Something the user has to fix. Say so and back off.
                set_state(listen_error=str(e), listen_note="")
                log("listen:", e)
                time.sleep(max(interval, 300))
                continue
            except Exception as e:
                # A network blip is not a reason to stop listening.
                short = str(e).split("\n")[0][:60] or type(e).__name__
                set_state(listen_note=f"retrying \u2014 {short}", listen_error=None)
                log("listen: transient:", e)
                time.sleep(interval)
                continue

            if get_state().get("spotify_playing"):
                continue

            if result:
                remember_track(result)
                set_state(now_playing=result, listen_error=None,
                          listen_note=f"matched at {stamp}")
                extra = ""
                if result.get("duration_ms"):
                    extra = f" [{result['progress_ms'] // 1000}s of {result['duration_ms'] // 1000}s]"
                log("heard:", result["artist"], "-", result["title"] + extra)
            else:
                set_state(listen_note=f"no match at {stamp}", listen_error=None)
                log(f"listen: heard audio ({level:.4f}) but no match")
        except ConfigError as e:
            set_state(listen_error=str(e), listen_note="")
            log("listen:", e)
            time.sleep(max(interval, 300))
            continue
        except Exception as e:
            short = str(e).split("\n")[0][:60] or type(e).__name__
            set_state(listen_note=f"retrying \u2014 {short}", listen_error=None)
            log("listen: transient:", e)
        nap(interval)

def audd_recognize(wav_bytes, token):
    """POST the clip to AudD as multipart/form-data. Returns a dict or None."""
    boundary = "----deskdash" + str(int(time.time() * 1000))
    parts = []

    def field(name, value):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
            .encode("utf-8"))

    field("api_token", token)
    field("return", "apple_music,spotify")
    parts.append(
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"file\"; filename=\"clip.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n".encode("utf-8"))
    parts.append(wav_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        "https://api.audd.io/",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        j = json.loads(r.read().decode("utf-8"))

    if j.get("status") == "error":
        err = (j.get("error") or {})
        raise ConfigError(
            f"AudD {err.get('error_code', '?')}: {err.get('error_message', 'unknown')}")
    if not j.get("result"):
        return None
    res = j["result"]
    album = res.get("album") or ""
    art = ""
    try:
        am = res.get("apple_music") or {}
        art = (am.get("artwork", {}).get("url") or "").replace("{w}", "600").replace("{h}", "600")
        if not album:
            album = am.get("albumName", "")
    except Exception:
        pass
    if not art:
        try:
            sp = res.get("spotify") or {}
            imgs = sp.get("album", {}).get("images", [])
            if imgs:
                art = imgs[0].get("url", "")
            if not album:
                album = sp.get("album", {}).get("name", "")
        except Exception:
            pass
    return {
        "title": res.get("title", ""),
        "artist": res.get("artist", ""),
        "album": album,
        "art": art,
        "ts": time.time(),
        "source": "mic",
    }


# --------------------------------------------------------------------- ac
# Tuya cloud (Smart Life). The AC is a virtual remote on the IR blaster.
TUYA_HOSTS = {"in": "https://openapi.tuyain.com", "eu": "https://openapi.tuyaeu.com",
              "us": "https://openapi.tuyaus.com", "cn": "https://openapi.tuyacn.com"}
_TUYA = {"token": "", "exp": 0, "ir": "", "remote": "", "name": ""}
_TUYA_LOCK = threading.Lock()
AC_MODES = ["cool", "heat", "auto", "fan", "dry"]
AC_FANS = ["auto", "low", "mid", "high"]


def _tuya_req(method, path, body=None, token=True):
    import hashlib, hmac, uuid
    t = cfg("tuya") or {}
    cid, secret = t.get("access_id", ""), t.get("access_secret", "")
    if not cid or not secret:
        raise ConfigError("Tuya isn't set up")
    host = TUYA_HOSTS.get(t.get("region", "in"), TUYA_HOSTS["in"])
    raw = json.dumps(body).encode() if body is not None else b""
    tok = _tuya_token() if token else ""
    ts, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
    to_sign = "\n".join([method, hashlib.sha256(raw).hexdigest(), "", path])
    sign = hmac.new(secret.encode(), (cid + tok + ts + nonce + to_sign).encode(),
                    hashlib.sha256).hexdigest().upper()
    headers = {"client_id": cid, "sign": sign, "t": ts, "nonce": nonce,
               "sign_method": "HMAC-SHA256", "Content-Type": "application/json"}
    if tok:
        headers["access_token"] = tok
    req = urllib.request.Request(host + path, data=raw if body is not None else None,
                                 method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode("utf-8"))
    if not j.get("success"):
        code, msg = j.get("code"), j.get("msg", "")
        if code in (1010, 1011):                 # token expired / invalid
            _TUYA["exp"] = 0
        if code == 28841002 or "expire" in msg.lower():
            raise ConfigError("Tuya trial expired: renew it on iot.tuya.com")
        raise ConfigError(f"Tuya: {msg} ({code})")
    return j.get("result")


def _tuya_token():
    if _TUYA["token"] and time.time() < _TUYA["exp"]:
        return _TUYA["token"]
    r = _tuya_req("GET", "/v1.0/token?grant_type=1", token=False)
    _TUYA["token"] = r["access_token"]
    _TUYA["exp"] = time.time() + int(r.get("expire_time", 3600)) - 120
    return _TUYA["token"]


def tuya_find_ac():
    """Walk the linked devices, ask each IR hub for its remotes, keep the AC."""
    if _TUYA["remote"]:
        return True
    devs = []
    try:
        r = _tuya_req("GET", "/v2.0/cloud/thing/device?page_size=20")
        devs = r if isinstance(r, list) else (r or {}).get("list", [])
    except ConfigError as e:
        log(f"ac: device list failed ({e}), trying the older endpoint")
        r = _tuya_req("GET", "/v1.0/iot-01/associated-users/devices?size=50")
        devs = (r or {}).get("devices", [])
    log("ac: devices " + ", ".join(f"{d.get('name')}[{d.get('category')}]" for d in devs))
    # Newer Smart Life setups list the AC as its own device ("infrared_ac"):
    # its id is the remote id, and the IR hub ("wnykq") is the one it sits on.
    hubs = [d for d in devs if d.get("category") == "wnykq"]
    for d in devs:
        nm = (d.get("name") or "").lower()
        if d.get("category") == "infrared_ac" or (
                d.get("category") in ("qt", "infrared_ac") and
                ("air" in nm or re.search(r"\bac\b", nm))):
            hub = d.get("gateway_id") or d.get("parent_id") or (hubs[0]["id"] if hubs else "")
            if hub:
                _TUYA.update(ir=hub, remote=d.get("id"), name=d.get("name") or "AC")
                log(f"ac: using {_TUYA['name']} on hub {hub}")
                return True
    fallback = None
    for d in devs:
        did = d.get("id")
        try:
            remotes = _tuya_req("GET", f"/v2.0/infrareds/{did}/remotes") or []
            log(f"ac: remotes on {d.get('name')}: {str(remotes)[:300]}")
            if isinstance(remotes, dict):
                remotes = remotes.get("list") or remotes.get("remotes") or []
        except Exception as e:
            log(f"ac: remotes on {d.get('name')} failed: {e}")
            continue
        for rm in remotes:
            log(f"ac: hub {d.get('name')} has remote {rm.get('remote_name')} cat {rm.get('category_id')}")
            cand = (did, rm.get("remote_id"), rm.get("remote_name") or "AC")
            if str(rm.get("category_id")) == "5" or "ac" in cand[2].lower() or "air" in cand[2].lower():
                _TUYA.update(ir=cand[0], remote=cand[1], name=cand[2])
                return True
            fallback = fallback or cand
    if fallback:
        _TUYA.update(ir=fallback[0], remote=fallback[1], name=fallback[2])
        return True
    return False


def _dev_codes():
    """Standard-instruction codes the AC device accepts, learned once."""
    if "codes" not in _TUYA:
        f = _tuya_req("GET", f"/v1.0/iot-03/devices/{_TUYA['remote']}/functions") or {}
        _TUYA["codes"] = {x.get("code"): x for x in f.get("functions", [])}
        log("ac: device codes " + ", ".join(f"{c}={v.get('values','')[:80]}" for c, v in _TUYA["codes"].items()))
    return _TUYA["codes"]

def _pick(codes, *names):
    for n in names:
        if n in codes:
            return n
    return None

def _ir_try(method, variants):
    """Tuya's IR API differs by version and region; try each, keep what works."""
    order = variants
    if _TUYA.get("ir_ver") is not None:
        order = [variants[_TUYA["ir_ver"]]] + [v for i, v in enumerate(variants) if i != _TUYA["ir_ver"]]
    last = None
    for path, body in order:
        try:
            r = _tuya_req(method, path, body)
            i = variants.index((path, body))
            if _TUYA.get("ir_ver") != i:
                log(f"ac: IR route works: {method} {path.split(_TUYA['ir'])[0]}…{path.split(_TUYA['remote'])[-1]}")
            _TUYA["ir_ver"] = i
            return r
        except ConfigError as e:
            last = e
            if "(1108)" not in str(e) and "(1109)" not in str(e):
                raise
    raise last


def ac_status():
    # The IR route is what actually makes the blaster fire; prefer it.
    if _TUYA.get("mode_api") != "dev":
        try:
            ir, rm = _TUYA['ir'], _TUYA['remote']
            r = _ir_try("GET", [
                (f"/v2.0/infrareds/{ir}/remotes/{rm}/ac/status", None),
                (f"/v1.0/infrareds/{ir}/remotes/{rm}/ac/status", None),
                (f"/v2.0/infrareds/{ir}/air-conditioners/{rm}/status", None),
                (f"/v1.0/infrareds/{ir}/air-conditioners/{rm}/status", None),
            ]) or {}
            _TUYA["ir_ver"] = None       # status and control paths are counted separately
            _TUYA["mode_api"] = "ir"
            def n(k, d):
                try: return int(r.get(k, d))
                except Exception: return d
            return {"power": n("power", 0) == 1, "temp": n("temp", 24),
                    "mode": AC_MODES[n("mode", 0) % 5], "fan": AC_FANS[n("wind", 0) % 4],
                    "name": _TUYA["name"]}
        except ConfigError as e:
            log(f"ac: IR api unavailable ({e})")
            set_state(ac_error="Enable 'IR Control Hub Open Service' on iot.tuya.com")
            _TUYA["mode_api"] = "dev-retry"
    if _TUYA.get("mode_api") != "ir":
        try:
            codes = _dev_codes()
            st = {x["code"]: x.get("value") for x in
                  (_tuya_req("GET", f"/v1.0/iot-03/devices/{_TUYA['remote']}/status") or [])}
            if _TUYA.get("mode_api") != "dev-retry": _TUYA["mode_api"] = "dev"
            pw = _pick(st, "switch", "power", "switch_1")
            tp = _pick(st, "temp", "temp_set", "T")
            md = _pick(st, "mode", "M")
            fn = _pick(st, "wind", "fan_speed_enum", "fan", "F")
            def as_mode(v):
                if isinstance(v, int) or str(v).isdigit(): return AC_MODES[int(v) % 5]
                return str(v or "cool").lower()
            def as_fan(v):
                if isinstance(v, int) or str(v).isdigit(): return AC_FANS[int(v) % 4]
                return str(v or "auto").lower()
            pv = st.get(pw) if pw else (get_state().get("ac") or {}).get("power", False)
            return {"power": pv in (True, 1, "1", "on", "true"),
                    "temp": int(st.get(tp) or 24), "mode": as_mode(st.get(md)),
                    "fan": as_fan(st.get(fn)), "name": _TUYA["name"]}
        except ConfigError as e:
            log(f"ac: device status failed ({e})")
            raise
    r = _tuya_req("GET", f"/v2.0/infrareds/{_TUYA['ir']}/remotes/{_TUYA['remote']}/ac/status") or {}
    def num(k, d):
        try: return int(r.get(k, d))
        except Exception: return d
    return {"power": num("power", 0) == 1, "temp": num("temp", 24),
            "mode": AC_MODES[num("mode", 0) % 5], "fan": AC_FANS[num("wind", 0) % 4],
            "name": _TUYA["name"]}


def ac_control(action, value=None):
    with _TUYA_LOCK:
        try:
            if not tuya_find_ac():
                return False, "Couldn't find an AC on your IR blaster."
            st = get_state().get("ac") or ac_status()
            power, temp = st.get("power"), int(st.get("temp", 24))
            mode, fan = st.get("mode", "cool"), st.get("fan", "auto")
            if action == "power":
                power = not power
            elif action == "temp":
                temp = max(16, min(30, temp + (1 if int(value) > 0 else -1)))
                power = True
            elif action == "mode":
                mode = AC_MODES[(AC_MODES.index(mode) + 1) % 5] if value is None else value
            elif action == "fan":
                fan = AC_FANS[(AC_FANS.index(fan) + 1) % 4]
            else:
                return False, "Unknown control."
            sent = False
            try:
                ir, rm = _TUYA['ir'], _TUYA['remote']
                scene = {"power": 1 if power else 0, "mode": AC_MODES.index(mode),
                         "temp": temp, "wind": AC_FANS.index(fan)}
                _ir_try("POST", [
                    (f"/v2.0/infrareds/{ir}/air-conditioners/{rm}/scenes", scene),
                    (f"/v1.0/infrareds/{ir}/air-conditioners/{rm}/scenes", scene),
                    (f"/v2.0/infrareds/{ir}/air-conditioners/{rm}/command",
                     {"code": "power", "value": scene["power"]} if action == "power" else
                     {"code": "temp", "value": temp} if action == "temp" else
                     {"code": "mode", "value": scene["mode"]} if action == "mode" else
                     {"code": "wind", "value": scene["wind"]}),
                ])
                sent = True
            except ConfigError as e:
                log(f"ac: IR send failed ({e}), trying device commands")
            if not sent:
                codes = _dev_codes()
                cmds = []
                def enc(code, word, idx):
                    vals = str((codes.get(code) or {}).get("values", ""))
                    if '"Integer"' in vals: return idx
                    return word if f'"{word}"' in vals else idx
                pw = _pick(codes, "switch", "power", "switch_1")
                if "PowerOn" in codes and (action == "power" or not st.get("power")):
                    k = "PowerOn" if power else "PowerOff"
                    cmds.append({"code": k, "value": k})
                elif pw and action == "power":
                    typ = (codes[pw].get("type") or "").lower()
                    cmds.append({"code": pw, "value": power if typ == "boolean" else (1 if power else 0)})
                tp = _pick(codes, "temp", "temp_set", "T")
                md = _pick(codes, "mode", "M")
                fn = _pick(codes, "wind", "fan_speed_enum", "fan", "F")
                if power:
                    if tp and action == "temp": cmds.append({"code": tp, "value": temp})
                    if md and action == "mode": cmds.append({"code": md, "value": enc(md, mode, AC_MODES.index(mode))})
                    if fn and action == "fan": cmds.append({"code": fn, "value": enc(fn, fan, AC_FANS.index(fan))})
                log(f"ac: sending {cmds}")
                if not cmds:
                    return False, "Turn the AC on first." if not power else "That control isn't supported."
                _tuya_req("POST", f"/v1.0/iot-03/devices/{_TUYA['remote']}/commands", {"commands": cmds})
            new = {"power": power, "temp": temp, "mode": mode, "fan": fan, "name": _TUYA["name"]}
            set_state(ac=new, ac_error=None)
            return True, ""
        except ConfigError as e:
            return False, str(e)
        except Exception as e:
            log(f"ac: control failed {e}")
            return False, "Couldn't reach Tuya."


# ---- soundbar: a plain IR remote ("Audio") on the same blaster
_SB = {"id": "", "name": "", "keys": {}, "cat": None}

def sb_find():
    if _SB["id"]:
        return True
    r = _tuya_req("GET", "/v2.0/cloud/thing/device?page_size=20")
    devs = r if isinstance(r, list) else (r or {}).get("list", [])
    for d in devs:
        nm = (d.get("name") or "").lower()
        if d.get("category") in ("qt", "infrared_amplifier") and any(
                w in nm for w in ("audio", "sound", "speaker", "amp")):
            _SB.update(id=d["id"], name=d.get("name") or "Soundbar")
            break
    if not _SB["id"]:
        return False
    if not _TUYA["ir"]:
        hubs = [d for d in devs if d.get("category") == "wnykq"]
        _TUYA["ir"] = hubs[0]["id"] if hubs else ""
    k = _tuya_req("GET", f"/v2.0/infrareds/{_TUYA['ir']}/remotes/{_SB['id']}/keys") or {}
    _SB["cat"] = k.get("category_id")
    _SB["keys"] = {x.get("key"): x for x in k.get("key_list", [])}
    log("soundbar: keys " + ", ".join(_SB["keys"]))
    return True

def _sb_key(want):
    keys = _SB["keys"]
    norm = {re.sub(r"[^a-z+\-]", "", (k or "").lower()): k for k in keys}
    table = {"power": ["power", "poweron", "on", "off"],
             "vol_up": ["volume+", "vol+", "volumeup", "volup", "+"],
             "vol_down": ["volume-", "vol-", "volumedown", "voldown", "-"],
             "mute": ["mute"]}
    for c in table.get(want, []):
        if c in norm:
            return norm[c]
    for n, k in norm.items():                     # looser match
        if want == "vol_up" and "vol" in n and ("up" in n or "+" in n): return k
        if want == "vol_down" and "vol" in n and ("down" in n or "-" in n): return k
    return None

def sb_control(action):
    with _TUYA_LOCK:
        try:
            if not sb_find():
                return False, "Couldn't find the soundbar."
            key = _sb_key(action)
            if not key:
                return False, "The soundbar remote has no key for that."
            x = _SB["keys"][key]
            _tuya_req("POST", f"/v2.0/infrareds/{_TUYA['ir']}/remotes/{_SB['id']}/command",
                      {"category_id": _SB["cat"], "key_id": x.get("key_id"), "key": key})
            return True, ""
        except ConfigError as e:
            log(f"soundbar: {e}")
            return False, str(e)
        except Exception as e:
            log(f"soundbar: {e}")
            return False, "Couldn't reach Tuya."


# ---- smart bulbs (Tuya light categories)
LIGHT_CATS = ("dj", "dd", "fwd", "xdd", "dc", "fsd", "tgq", "tgkg", "cz", "pc", "kg")
_LAMPS = {}          # id -> {"name", "codes"}

def lamps_find():
    r = _tuya_req("GET", "/v2.0/cloud/thing/device?page_size=20")
    devs = r if isinstance(r, list) else (r or {}).get("list", [])
    for d in devs:
        if d.get("category") in LIGHT_CATS and d["id"] not in _LAMPS:
            f = _tuya_req("GET", f"/v1.0/iot-03/devices/{d['id']}/functions") or {}
            codes = {x.get("code"): x for x in f.get("functions", [])}
            _LAMPS[d["id"]] = {"name": d.get("name") or "Plug", "codes": codes,
                               "plug": d.get("category") in ("cz", "pc", "kg")}
            log(f"lamp: found {d.get('name')} [{d.get('category')}] codes {', '.join(codes)}")

def _lc(codes, *names):
    for n in names:
        if n in codes: return n

def _range(codes, code):
    try:
        v = json.loads(codes[code].get("values") or "{}")
        return int(v.get("min", 10)), int(v.get("max", 1000))
    except Exception:
        return 10, 1000

def lamps_status():
    out = []
    for lid, L in _LAMPS.items():
        st = {x["code"]: x.get("value") for x in
              (_tuya_req("GET", f"/v1.0/iot-03/devices/{lid}/status") or [])}
        c = L["codes"]
        sw = _lc(st, "switch_led", "switch_1", "switch")
        br = _lc(c, "bright_value_v2", "bright_value")
        tp = _lc(c, "temp_value_v2", "temp_value")
        pct = None
        if br and st.get(br) is not None:
            lo, hi = _range(c, br); pct = round((int(st[br]) - lo) * 100 / max(1, hi - lo))
        warm = None
        if tp and st.get(tp) is not None:
            lo, hi = _range(c, tp); warm = round((int(st[tp]) - lo) * 100 / max(1, hi - lo))
        out.append({"id": lid, "name": L["name"], "on": bool(st.get(sw)),
                    "bright": pct, "warmth": warm, "has_temp": bool(tp),
                    "plug": L.get("plug", False), "online": True})
    return out

def lamp_control(lid, action, value=None):
    if lid.startswith("st:"):
        ok, msg = st_control(lid[3:], action, value)
        if ok:
            try: _merge_lamps("st:", st_status())
            except Exception: pass
        return ok, msg
    if lid.startswith("wiz:"):
        ok, msg = wiz_control(lid[4:], action, value)
        if ok:
            try:
                set_state(lamps=wiz_status() + [l for l in (get_state().get("lamps") or [])
                                                if not str(l.get("id", "")).startswith("wiz:")])
            except Exception: pass
        return ok, msg
    with _TUYA_LOCK:
        L = _LAMPS.get(lid)
        if not L:
            return False, "Lamp not found."
        c = L["codes"]
        cmds = []
        try:
            if action == "power":
                sw = _lc(c, "switch_led", "switch_1", "switch")
                cmds.append({"code": sw, "value": bool(value)})
            elif action == "bright":
                br = _lc(c, "bright_value_v2", "bright_value")
                lo, hi = _range(c, br)
                cmds += [{"code": _lc(c, "switch_led", "switch_1", "switch"), "value": True},
                         {"code": br, "value": int(lo + (hi - lo) * max(0, min(100, int(value))) / 100)}]
                if "work_mode" in c: cmds.insert(1, {"code": "work_mode", "value": "white"})
            elif action == "warmth":
                tp = _lc(c, "temp_value_v2", "temp_value")
                lo, hi = _range(c, tp)
                cmds.append({"code": tp, "value": int(lo + (hi - lo) * max(0, min(100, int(value))) / 100)})
                if "work_mode" in c: cmds.insert(0, {"code": "work_mode", "value": "white"})
            else:
                return False, "Unknown control."
            cmds = [x for x in cmds if x["code"]]
            _tuya_req("POST", f"/v1.0/iot-03/devices/{lid}/commands", {"commands": cmds})
            try: set_state(lamps=[l for l in (get_state().get("lamps") or [])
                                  if str(l.get("id", "")).startswith("wiz:")] + lamps_status())
            except Exception: pass
            return True, ""
        except ConfigError as e:
            log(f"lamp: {e}"); return False, str(e)
        except Exception as e:
            log(f"lamp: {e}"); return False, "Couldn't reach Tuya."


# ---- WiZ bulbs: local UDP on port 38899, no cloud
import socket
_WIZ = {}            # ip -> {"name", "mac"}
WIZ_PORT = 38899

def _wiz(ip, method, params=None, timeout=1.5):
    msg = {"method": method, "params": params or {}}
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.settimeout(timeout)
    try:
        for _ in range(2):                    # UDP: send twice, it's cheap
            sk.sendto(json.dumps(msg).encode(), (ip, WIZ_PORT))
            try:
                data, _a = sk.recvfrom(2048)
                return json.loads(data.decode()).get("result") or {}
            except socket.timeout:
                continue
        return None
    finally:
        sk.close()

def wiz_discover():
    ips = set()
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sk.settimeout(2.5)
    try:
        home = home_ip() or ""
        targets = ["255.255.255.255"]
        if home.count(".") == 3:
            targets.append(home.rsplit(".", 1)[0] + ".255")
        for t in targets:
            try: sk.sendto(b'{"method":"getPilot","params":{}}', (t, WIZ_PORT))
            except OSError: pass
        end = time.time() + 2.5
        while time.time() < end:
            try:
                data, (ip, _p) = sk.recvfrom(2048)
                if b"result" in data: ips.add(ip)
            except socket.timeout:
                break
    finally:
        sk.close()
    for ip in (cfg("wiz_ips") or []):      # manual fallback if broadcast is blocked
        ips.add(ip)
    for ip in ips:
        if ip not in _WIZ:
            info = _wiz(ip, "getSystemConfig") or {}
            n = len(_WIZ) + 1
            _WIZ[ip] = {"name": (cfg("wiz_names") or {}).get(ip) or ("Lamp" if n == 1 else f"Lamp {n}"),
                        "mac": info.get("mac", "")}
            log(f"wiz: found bulb at {ip} ({info.get('moduleName', '?')})")

def wiz_status():
    out = []
    for ip, W in _WIZ.items():
        p = _wiz(ip, "getPilot")
        if p is None:
            out.append({"id": "wiz:" + ip, "name": W["name"], "on": False, "bright": None,
                        "warmth": None, "has_temp": True, "online": False})
            continue
        t = p.get("temp")
        out.append({"id": "wiz:" + ip, "name": W["name"], "on": bool(p.get("state")),
                    "bright": int(p.get("dimming", 100)),
                    "warmth": round((6500 - int(t)) * 100 / 4300) if t else None,
                    "has_temp": True, "online": True})
    return out

def wiz_control(ip, action, value):
    if action == "power":
        params = {"state": bool(value)}
    elif action == "bright":
        params = {"state": True, "dimming": max(10, min(100, int(value)))}
    elif action == "warmth":        # 0 = cool 6500K, 100 = warm 2200K
        params = {"state": True, "temp": int(6500 - 4300 * max(0, min(100, int(value))) / 100)}
    else:
        return False, "Unknown control."
    r = _wiz(ip, "setPilot", params)
    if r is None:
        return False, "The lamp didn't answer. Is it switched on at the wall?"
    return True, ""

def wiz_loop():
    last_scan = 0
    while True:
        try:
            if time.time() - last_scan > (600 if _WIZ else 60):
                wiz_discover(); last_scan = time.time()
            if _WIZ:
                set_state(lamps=wiz_status() + [l for l in (get_state().get("lamps") or [])
                                                if not str(l.get("id", "")).startswith("wiz:")])
        except Exception as e:
            log(f"wiz: {e}")
        time.sleep(20)


# ---- SmartThings lights (WiZ etc. linked into SmartThings)
_ST = {}             # id -> name
ST_API = "https://api.smartthings.com/v1"

ST_TOKENS = os.path.join(HERE, "st_tokens.json")
ST_REDIRECT = "https://httpbin.org/get"
ST_SCOPES = "r:devices:* x:devices:*"

def _st_app():
    a = cfg("smartthings_app") or {}
    return a.get("client_id", ""), a.get("client_secret", "")

def st_auth_url():
    cid, _ = _st_app()
    if not cid:
        return ""
    return ("https://api.smartthings.com/oauth/authorize?" + urllib.parse.urlencode(
        {"client_id": cid, "response_type": "code", "redirect_uri": ST_REDIRECT, "scope": ST_SCOPES}))

def _st_token_call(form):
    import base64
    cid, sec = _st_app()
    form["client_id"] = cid
    req = urllib.request.Request(
        "https://auth-global.api.smartthings.com/oauth/token",
        data=urllib.parse.urlencode(form).encode(), method="POST",
        headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode())
    j["expires_at"] = time.time() + int(j.get("expires_in", 86399)) - 300
    with open(ST_TOKENS, "w", encoding="utf-8") as f:
        json.dump(j, f)
    return j

def st_exchange(code):
    code = (code or "").strip()
    m = re.search(r"code[\"'=:\s]+([A-Za-z0-9_\-]+)", code)   # accept the whole httpbin page too
    if m: code = m.group(1)
    try:
        _st_token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": ST_REDIRECT})
    except urllib.error.HTTPError as e:
        return False, "SmartThings didn't accept that code. Codes only work once, try Connect again."
    _ST.clear()
    set_state(st_needs_login=False)
    return True, ""

def _st_bearer():
    try:
        with open(ST_TOKENS, "r", encoding="utf-8") as f:
            t = json.load(f)
    except Exception:
        t = None
    if t and t.get("access_token"):
        if time.time() < t.get("expires_at", 0):
            return t["access_token"]
        try:
            return _st_token_call({"grant_type": "refresh_token",
                                   "refresh_token": t["refresh_token"]})["access_token"]
        except Exception as e:
            log(f"smartthings: refresh failed {e}")
    return cfg("smartthings_token") or ""

def _st(method, path, body=None):
    tok = _st_bearer()
    if not tok:
        raise ConfigError("no SmartThings token")
    req = urllib.request.Request(ST_API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            if _st_app()[0]:
                set_state(st_needs_login=True, st_auth_url=st_auth_url())
                raise ConfigError("Reconnect SmartThings")
            raise ConfigError("SmartThings token expired: make a new one")
        raise

def st_find():
    j = _st("GET", "/devices")
    for d in j.get("items", []):
        caps = {c.get("id") for comp in d.get("components", []) for c in comp.get("capabilities", [])}
        if "switch" in caps and d["deviceId"] not in _ST:
            light = "switchLevel" in caps or "colorTemperature" in caps
            cat = " ".join(c.get("name", "") for comp in d.get("components", [])
                           for c in comp.get("categories", [])).lower()
            if not light and not any(w in cat for w in ("plug", "outlet", "switch", "smartplug")):
                continue
            _ST[d["deviceId"]] = {"name": d.get("label") or d.get("name") or "Lamp",
                                  "temp": "colorTemperature" in caps, "plug": not light}
            log(f"smartthings: found {'light' if light else 'plug'} {_ST[d['deviceId']]['name']}")

def st_status():
    out = []
    for did, D in _ST.items():
        try:
            m = (_st("GET", f"/devices/{did}/status").get("components") or {}).get("main", {})
            on = (m.get("switch", {}).get("switch", {}) or {}).get("value") == "on"
            lvl = (m.get("switchLevel", {}).get("level", {}) or {}).get("value")
            k = (m.get("colorTemperature", {}).get("colorTemperature", {}) or {}).get("value")
            out.append({"id": "st:" + did, "name": D["name"], "on": on,
                        "bright": int(lvl) if lvl is not None else None,
                        "warmth": round((6500 - int(k)) * 100 / 4300) if k else None,
                        "has_temp": D["temp"], "plug": D.get("plug", False), "online": True})
        except ConfigError:
            raise
        except Exception:
            out.append({"id": "st:" + did, "name": D["name"], "on": False, "bright": None,
                        "warmth": None, "has_temp": D["temp"], "online": False})
    return out

def st_control(did, action, value):
    if action == "power":
        cmds = [{"component": "main", "capability": "switch", "command": "on" if value else "off"}]
    elif action == "bright":
        cmds = [{"component": "main", "capability": "switchLevel", "command": "setLevel",
                 "arguments": [max(1, min(100, int(value)))]}]
    elif action == "warmth":
        cmds = [{"component": "main", "capability": "colorTemperature", "command": "setColorTemperature",
                 "arguments": [int(6500 - 4300 * max(0, min(100, int(value))) / 100)]}]
    else:
        return False, "Unknown control."
    try:
        _st("POST", f"/devices/{did}/commands", {"commands": cmds})
        return True, ""
    except ConfigError as e:
        return False, str(e)
    except Exception as e:
        log(f"smartthings: {e}")
        return False, "Couldn't reach SmartThings."

def _merge_lamps(prefix, fresh):
    keep = [l for l in (get_state().get("lamps") or []) if not str(l.get("id", "")).startswith(prefix)]
    set_state(lamps=fresh + keep)

def st_loop():
    if not cfg("smartthings_token") and not _st_app()[0]:
        return
    if _st_app()[0] and not os.path.exists(ST_TOKENS):
        set_state(st_needs_login=True, st_auth_url=st_auth_url())
    last = 0
    while True:
        try:
            if time.time() - last > 600 or not _ST:
                st_find(); last = time.time()
            if _ST:
                _merge_lamps("st:", st_status())
        except ConfigError as e:
            log(f"smartthings: {e}")
            set_state(ac_error=str(e)) if not get_state().get("ac") else None
        except Exception as e:
            log(f"smartthings: {e}")
        time.sleep(20)


def ac_loop():
    if not (cfg("tuya") or {}).get("access_id"):
        return
    while True:
        try:
            with _TUYA_LOCK:
                try:
                    lamps_find()
                    if _LAMPS:
                        set_state(lamps=[l for l in (get_state().get("lamps") or [])
                                         if str(l.get("id", "")).startswith("wiz:")] + lamps_status())
                except Exception as e:
                    log(f"lamp: {e}")
                if tuya_find_ac():
                    set_state(ac=ac_status(), ac_error=None)
                else:
                    set_state(ac=None, ac_error="No AC found on the IR blaster")
                    log("ac: no AC remote found")
        except ConfigError as e:
            set_state(ac_error=str(e)); log(f"ac: {e}")
        except Exception as e:
            log(f"ac: {e}")
        time.sleep(60)


# --------------------------------------------------------------- modules
# Everything optional the dashboard can show, beside the record.

PLAYS_LOG = os.path.join(USER_DIR, "plays.jsonl")
_MOD_CACHE = {}


def record_play(t, at=None):
    """One line per song played, so the history panel has something to count."""
    try:
        with open(PLAYS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"title": t.get("title", ""), "artist": t.get("artist", ""),
                                "album": t.get("album", ""), "art": t.get("art", ""),
                                "ms": int(t.get("duration_ms") or 0),
                                "at": at or time.time()}) + "\n")
    except Exception:
        pass


def _has_stats_scope():
    got = (_load_tokens().get("scope") or "")
    return all(s in got for s in STATS_SCOPES)


def _has_library_scope():
    return LIBRARY_SCOPE in (_load_tokens().get("scope") or "")


def backfill_spotify():
    """Spotify remembers the last 50 things you played; B-Side only sees what
    happens while it's open. Pull theirs in so the counts mean something on
    day one. Nothing here is a duplicate: each play is keyed on its own
    timestamp, which Spotify gives us."""
    if not _has_stats_scope():
        return 0
    try:
        _, j = _spotify_call(
            "GET", "https://api.spotify.com/v1/me/player/recently-played?limit=50")
    except Exception as e:
        log("history: couldn't read recently played -", e)
        return 0
    items = j.get("items") or []
    if not items:
        return 0
    # Spotify stamps a play when it ends; we stamp it when we first see it, so
    # the same song arrives with two different times. Match on the song as well
    # as the clock, or every track played with B-Side open gets counted twice.
    have = [(round(r.get("at", 0)),
             (r.get("title") or "").strip().lower(),
             (r.get("artist") or "").split(",")[0].strip().lower())
            for r in _read_plays(days=400)]
    added = 0
    for it in items:
        tr = it.get("track") or {}
        when = it.get("played_at") or ""
        try:                       # played_at is UTC, so read it as UTC
            at = calendar.timegm(time.strptime(when[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        title = (tr.get("name") or "").strip()
        artists = [a.get("name", "") for a in (tr.get("artists") or [])]
        key = (title.lower(), (artists[0] if artists else "").strip().lower())
        if any(t == key[0] and a == key[1] and abs(at - s) < 900 for s, t, a in have):
            continue
        imgs = ((tr.get("album") or {}).get("images") or [])
        record_play({"title": title, "artist": ", ".join(artists),
                     "album": (tr.get("album") or {}).get("name", ""),
                     "art": imgs[-1]["url"] if imgs else "",
                     "duration_ms": tr.get("duration_ms") or 0}, at=at)
        have.append((round(at), key[0], key[1]))
        added += 1
    if added:
        log(f"history: pulled {added} plays from Spotify")
    return added


def apple_library_stats():
    """Music.app counts every play itself, for years. Read it in one go -
    asking track by track would take a minute on a real library."""
    if not _app_running("Music"):
        return None
    sep = "␟"
    script = f'''tell application "Music"
        set ts to (every track of library playlist 1 whose played count > 0)
        if (count of ts) is 0 then return ""
        set AppleScript's text item delimiters to "{sep}"
        return ((artist of ts) as text) & " " & ((played count of ts) as text) \
            & " " & ((duration of ts) as text)
    end tell'''
    out = _osa(script, timeout=25)
    if not out:
        return None
    try:
        artists, counts, durs = [p.split(sep) for p in out.split(" ")[:3]]
    except Exception:
        return None
    by_artist, plays, seconds = {}, 0, 0.0
    for i, name in enumerate(artists):
        try:
            n = int(float(counts[i]))
            d = float(str(durs[i]).replace(",", "."))
        except Exception:
            continue
        name = (name or "").strip()
        plays += n
        seconds += n * d
        if name:
            by_artist[name] = by_artist.get(name, 0) + n
    if not plays:
        return None
    top = max(by_artist, key=by_artist.get) if by_artist else None
    return {"plays": plays, "hours": round(seconds / 3600), "top_artist": top,
            "top_plays": by_artist.get(top, 0) if top else 0}


def _read_plays(days=400):
    cutoff = time.time() - days * 86400
    rows = []
    try:
        with open(PLAYS_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("at", 0) >= cutoff:
                    rows.append(r)
    except FileNotFoundError:
        pass
    return rows


def _spotify_top_artist():
    """Spotify's own sense of who you've had on, over about six months."""
    if not _has_stats_scope():
        return None
    try:
        _, j = _spotify_call(
            "GET", "https://api.spotify.com/v1/me/top/artists"
                   "?limit=1&time_range=medium_term")
        items = j.get("items") or []
        return (items[0].get("name") or "").strip() or None if items else None
    except Exception:
        return None


def history_stats():
    """How much music, over what stretch, and who's been on most.

    Only ever counts a stretch the log actually covers. Spotify hands back the
    last 50 plays and nothing older, which on day one is a day or two of
    listening - calling that 'this year' would be a lie, so a period is only
    reported once the log reaches back past its start. Months fill in as the
    app gets used. Apple Music is different: Music.app has counted every play
    for years, so there the totals are its own."""
    apple = apple_library_stats() if cfg("service") == "apple" else None
    rows = _read_plays()
    if not rows and not apple:
        return None

    now = datetime.now()
    since = min((r["at"] for r in rows), default=time.time())
    # Rolling windows, all three. Calendar ones read as nonsense early in a
    # month: on the 2nd, 'this month' was two days while 'this week' was
    # seven, so the month showed fewer plays than the week.
    month_start = time.time() - 30 * 86400
    year_start = time.time() - 365 * 86400

    week = [r for r in rows if r["at"] > time.time() - 7 * 86400]
    month = [r for r in rows if r["at"] >= month_start]
    year = [r for r in rows if r["at"] >= year_start]

    def minutes(pool):
        ms = sum(int(r.get("ms") or 0) for r in pool)
        gaps = [r for r in pool if not r.get("ms")]
        ms += len(gaps) * 3 * 60 * 1000  # older lines predate the length field
        return round(ms / 60000)

    # Widest period the log can honestly speak for, with a day of slack so a
    # fresh log doesn't claim the week either.
    spans = "week" if since <= time.time() - 6 * 86400 else "some"
    if since <= month_start + 86400:     # same day of slack as the week
        spans = "month"
    if since <= year_start + 86400:
        spans = "year"

    counts = {}
    for r in rows:
        k = (r.get("artist") or "").split(",")[0].strip()
        if k:
            counts[k] = counts.get(k, 0) + 1
    artist = max(counts, key=counts.get) if counts else None
    plays = counts.get(artist, 0) if artist else 0
    top_from = "log"
    if apple and apple.get("top_artist"):
        artist, plays, top_from = apple["top_artist"], apple["top_plays"], "apple"
    elif plays < 3:                      # too thin to mean anything - ask Spotify
        spot = _spotify_top_artist()
        if spot:
            artist, plays, top_from = spot, 0, "spotify"

    recent, seen = [], set()
    for r in sorted(rows, key=lambda r: -r.get("at", 0)):
        # the same song can arrive credited two ways ("Carti" / "Carti, The
        # Weeknd"), so match on the lead artist only
        key = ((r.get("title") or "").strip().lower(),
               (r.get("artist") or "").split(",")[0].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        recent.append({"title": r.get("title"), "artist": r.get("artist"),
                       "art": r.get("art"), "at": r.get("at")})
        if len(recent) >= 3:
            break

    out = {"week": len(week), "month": len(month), "year": len(year),
           "mins_week": minutes(week), "mins_month": minutes(month),
           "mins_year": minutes(year), "spans": spans, "since": since,
           "top_artist": artist, "top_plays": plays, "top_from": top_from,
           "recent": recent,
           "source": "apple" if apple else "spotify",
           "linked": bool(apple) or _has_stats_scope()}
    if apple:
        out["all_plays"] = apple["plays"]
        out["all_hours"] = apple["hours"]
    return out


def _my_artists(limit=8):
    """The artists they actually listen to - Spotify's own top list first,
    then anyone the play log has seen more than once."""
    out = []
    if cfg("service", "spotify") == "spotify" and _has_stats_scope():
        for span in ("short_term", "medium_term"):
            try:
                _, j = _spotify_call(
                    "GET", "https://api.spotify.com/v1/me/top/artists"
                           f"?limit=15&time_range={span}")
                for a in (j.get("items") or []):
                    name = (a.get("name") or "").strip()
                    if name and name not in out:
                        out.append(name)
            except Exception:
                pass
    counts = {}
    for r in _read_plays(days=120):
        a = (r.get("artist") or "").split(",")[0].strip()
        if a:
            counts[a] = counts.get(a, 0) + 1
    for a in sorted(counts, key=lambda a: -counts[a]):
        if counts[a] >= 2 and a not in out:
            out.append(a)
    if not out:                       # brand new: whatever is on right now
        cur = (get_state().get("last_track") or {}).get("artist", "")
        cur = cur.split(",")[0].strip()
        if cur:
            out.append(cur)
    return out[:limit]


def concerts():
    """Shows by the artists they listen to, in their own city where possible."""
    artists = _my_artists()
    if not artists:
        return None
    app_id = cfg("bandsintown_app_id", "b-side-dashboard")
    here = (cfg("place") or "").split(",")[0].strip().lower()
    near, elsewhere = [], []
    for artist in artists:
        try:
            url = ("https://rest.bandsintown.com/artists/%s/events?app_id=%s&date=upcoming"
                   % (urllib.parse.quote(artist, safe=""), urllib.parse.quote(app_id)))
            req = urllib.request.Request(url, headers={"User-Agent": "B-Side/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                events = json.loads(r.read().decode() or "[]")
        except Exception:
            continue
        for e in (events or [])[:8]:
            v = e.get("venue") or {}
            city = ", ".join(x for x in (v.get("city"), v.get("country")) if x)
            row = {"artist": artist, "venue": v.get("name", ""), "city": city,
                   "when": (e.get("datetime") or "")[:10], "url": e.get("url", "")}
            (near if here and here in city.lower() else elsewhere).append(row)
        time.sleep(0.4)

    rows = sorted(near, key=lambda e: e["when"])[:5]
    if not rows:
        # Nothing at home: only bother with the handful they play most.
        keep = set(artists[:3])
        rows = sorted([e for e in elsewhere if e["artist"] in keep],
                      key=lambda e: e["when"])[:3]
    if not rows:
        return None
    return {"near": bool(near), "place": cfg("place", ""), "events": rows}


def _album(a):
    """Tidy one Spotify album object into what the panel shows."""
    imgs = a.get("images") or []
    return {"title": a.get("name", ""),
            "artist": ", ".join(x.get("name", "") for x in (a.get("artists") or [])),
            "year": (a.get("release_date") or "")[:4],
            "art": imgs[0]["url"] if imgs else "",
            "uri": a.get("uri", ""),
            "url": (a.get("external_urls") or {}).get("spotify", "")}


def _shelf_from_saved(rnd):
    """The albums they've actually added to their library - the real shelf.

    Asks for the count first, then reads one album from a random spot in it,
    so a 600-album library costs the same two small calls as a ten-album one."""
    _, j = _spotify_call("GET", "https://api.spotify.com/v1/me/albums?limit=1")
    total = int(j.get("total") or 0)
    if not total:
        return None
    off = rnd.randrange(total)
    _, j = _spotify_call(
        "GET", f"https://api.spotify.com/v1/me/albums?limit=1&offset={off}")
    items = j.get("items") or []
    return _album(items[0].get("album") or {}) if items else None


def _shelf_from_music_app(rnd):
    """The same idea on Apple: albums sitting in their Music library."""
    if not _app_running("Music"):
        return None
    sep = "␟"
    script = f'''tell application "Music"
        set ts to (every track of library playlist 1 whose album is not "")
        if (count of ts) is 0 then return ""
        set AppleScript's text item delimiters to "{sep}"
        return ((album of ts) as text) & " " & ((album artist of ts) as text) \
            & " " & ((artist of ts) as text)
    end tell'''
    out = _osa(script, timeout=25)
    if not out:
        return None
    try:
        albums, aartists, artists = [p.split(sep) for p in out.split(" ")[:3]]
    except Exception:
        return None
    shelf = {}
    for i, name in enumerate(albums):
        name = (name or "").strip()
        who = ((aartists[i] if i < len(aartists) else "") or
               (artists[i] if i < len(artists) else "")).strip()
        if name and who:
            shelf[(who.lower(), name.lower())] = (who, name)
    if not shelf:
        return None
    who, name = rnd.choice(list(shelf.values()))
    return {"title": name, "artist": who, "year": "",
            "art": art_for(who, name, name) or "", "uri": "", "url": ""}


def _shelf_from_recent(rnd):
    """Albums off their own recently-played. Same endpoint the history panel
    already uses, so if history works this works."""
    _, j = _spotify_call(
        "GET", "https://api.spotify.com/v1/me/player/recently-played?limit=50")
    seen = {}
    for it in (j.get("items") or []):
        al = ((it.get("track") or {}).get("album") or {})
        if al.get("album_type") == "album" and al.get("id"):
            seen[al["id"]] = al
    return _album(rnd.choice(list(seen.values()))) if seen else None


def _shelf_from_log(rnd):
    """Last resort, and the only one that needs nothing from Spotify: an album
    out of the play log, with cover art from iTunes."""
    albums = {}
    for r in _read_plays(days=180):
        name = (r.get("album") or "").strip()
        artist = (r.get("artist") or "").strip()
        if name and artist and name.lower() != (r.get("title") or "").lower():
            albums[(artist.lower(), name.lower())] = (artist, name, r.get("art") or "")
    if not albums:
        return None
    artist, name, art = rnd.choice(list(albums.values()))
    return {"title": name, "artist": artist, "year": "",
            "art": art or art_for(artist, name, name) or "", "uri": "", "url": ""}


def album_of_the_day():
    """One album out of their library, picked fresh each day.

    Their saved albums are the shelf. The two routes under it only matter when
    the library is empty or unreadable - a panel that silently gives up is
    worse than a slightly less interesting pick. Each route logs its own
    failure, so there's never a mystery about which step went wrong."""
    key = "aotd:" + datetime.now().strftime("%Y-%m-%d")
    hit = _MOD_CACHE.get(key)
    if hit and hit[1]:
        return hit[1]                # only a real pick is worth keeping all day

    import random
    rnd = random.Random(datetime.now().strftime("%Y%m%d"))
    apple = cfg("service") == "apple"
    linked = bool(_load_tokens().get("refresh_token"))
    routes = []
    if apple:
        routes.append(("your Music library", lambda: _shelf_from_music_app(rnd)))
    elif linked and _has_library_scope():
        routes.append(("your saved albums", lambda: _shelf_from_saved(rnd)))
    if linked:
        routes.append(("recently played", lambda: _shelf_from_recent(rnd)))
    routes.append(("the play log", lambda: _shelf_from_log(rnd)))

    pick = None
    for name, fn in routes:
        try:
            pick = fn()
        except Exception as e:
            log(f"shelf: {name} failed -", e)
            continue
        if pick:
            log(f"shelf: {pick['artist']} - {pick['title']} (from {name})")
            break
    if not pick:
        log("shelf: nothing to pick from yet")
    out = dict(pick) if pick else None
    if out is not None and not apple and linked and not _has_library_scope():
        out["reconnect"] = True      # so the panel can say why it's not the shelf
    _MOD_CACHE[key] = (time.time(), out)
    return out


def modules_loop():
    """Keeps the optional panels fed, gently."""
    while True:
        try:
            mods = CFG.get("modules") or {}
            if cfg("service") == "spotify":
                backfill_spotify()          # their own history, not just ours
            np_ = get_state().get("now_playing")
            if np_ and not os.path.exists(PLAYS_LOG):
                record_play(np_)            # so the panel isn't blank on day one

            # One panel per try. Sharing one meant a slow concerts lookup or a
            # bad response could stop the panels after it from ever running.
            for name, on, fn in (
                    ("history", mods.get("history", True), history_stats),
                    ("aotd", mods.get("aotd", True), album_of_the_day),
                    ("concerts", mods.get("concerts", False), concerts)):
                if not on:
                    set_state(**{name: None})
                    continue
                try:
                    set_state(**{name: fn()})
                except Exception as e:
                    log(f"modules: {name} -", e)
            set_state(onthisday=None)       # panel retired
        except Exception as e:
            log("modules:", e)
        time.sleep(300 if get_state().get("aotd") else 90)


_MODULES_STARTED = threading.Event()



def update_loop():
    """Look for a newer version now and then and let the dashboard mention it.
    It never installs anything by itself - the person still presses the
    button. The first look is delayed so it can't slow a cold start."""
    time.sleep(45)
    while True:
        try:
            if _repo():
                got = update_check()
                if got.get("ok") and got.get("files"):
                    set_state(update={"files": got["files"]})
                    log("update: available -", ", ".join(got["files"]))
                else:
                    set_state(update=None)
        except Exception as e:
            log("update:", e)
        time.sleep(6 * 3600)


def _has_room():
    """True when this copy has been given any smart-home keys: Tuya, WiZ,
    or SmartThings. A plain install has none and never touches the LAN."""
    return bool((cfg("tuya") or {}).get("access_id")
                or cfg("wiz") or cfg("smartthings_token") or _st_app()[0])


def start_modules():
    """The app only knows about the original loops, so the panels start
    themselves when server.py is loaded. Safe to call more than once."""
    if _MODULES_STARTED.is_set():
        return
    _MODULES_STARTED.set()
    threading.Thread(target=modules_loop, daemon=True).start()
    threading.Thread(target=lyrics_loop, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    # The room half: the mic, the AC, and the bulbs. Each of these returns
    # straight away when its section of config.json is absent, so a friend's
    # copy with no Tuya keys simply never starts them.
    # The room only wakes on a machine that's been set up for it. Without
    # this a friend's Mac would scan the LAN for WiZ bulbs every minute and
    # macOS would ask them about 'local network access' for no reason.
    threading.Thread(target=listen_loop, daemon=True).start()   # gated on cfg('listen')
    threading.Thread(target=crate_loop, daemon=True).start()    # gated on discogs_username
    if _has_room():
        for fn in (ac_loop, wiz_loop, st_loop):
            threading.Thread(target=fn, daemon=True).start()
        log("room: smart-home config found, looking for devices")
    log("modules: panels running")


# ------------------------------------------------------------------ crate
# A random record from your Discogs collection, shown on the idle platter.

_CRATE = []


def _discogs(url):
    headers = {"User-Agent": "DeskDashboard/1.0"}
    tok = cfg("discogs_token")
    if tok:
        headers["Authorization"] = "Discogs token=" + tok
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def load_crate(user):
    """Every release in the collection's 'All' folder (folder 0)."""
    out, page, pages = [], 1, 1
    while page <= pages and page <= 40:
        j = _discogs(f"https://api.discogs.com/users/{urllib.parse.quote(user)}"
                     f"/collection/folders/0/releases?per_page=100&page={page}")
        pages = int((j.get("pagination") or {}).get("pages") or 1)
        for rel in j.get("releases") or []:
            b = rel.get("basic_information") or {}
            # Discogs disambiguates same-named artists as "Name (2)"; drop that.
            artists = ", ".join(re.sub(r"\s\(\d+\)$", "", a.get("name", ""))
                                for a in b.get("artists") or [])
            out.append({
                "title": b.get("title", ""),
                "artist": artists,
                "year": b.get("year") or "",
                "art": b.get("cover_image") or b.get("thumb") or "",
                "url": f"https://www.discogs.com/release/{b.get('id')}" if b.get("id") else "",
            })
        page += 1
        time.sleep(2.5)      # unauthenticated limit is 25 requests a minute
    return out


COVER_CACHE = os.path.join(USER_DIR, "covers_cache.json")

def _clean(t):
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", t or "")   # (Remastered), [Deluxe] ...
    return re.sub(r"\s+", " ", t).strip().lower()

def official_cover(artist, title):
    """Studio artwork from Spotify, else Apple Music. '' if neither knows it."""
    first = (artist or "").split(",")[0].strip()
    want = _clean(title)
    try:
        q = urllib.parse.quote(f'album:{_clean(title)} artist:{first}')
        _, j = _spotify_call("GET", f"https://api.spotify.com/v1/search?type=album&limit=5&q={q}")
        items = (j.get("albums") or {}).get("items") or []
        items.sort(key=lambda a: _clean(a.get("name")) != want)   # exact title first
        for a in items:
            if a.get("images"):
                return a["images"][0]["url"]
    except Exception:
        pass
    try:
        q = urllib.parse.quote(f"{first} {_clean(title)}")
        req = urllib.request.Request(
            f"https://itunes.apple.com/search?entity=album&limit=5&term={q}",
            headers={"User-Agent": "desk-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8")).get("results") or []
        res.sort(key=lambda a: _clean(a.get("collectionName")) != want)
        for a in res:
            if a.get("artworkUrl100"):
                return a["artworkUrl100"].replace("100x100bb", "1000x1000bb")
    except Exception:
        pass
    return ""

def swap_covers(recs):
    """Replace Discogs' user photos with official art, cached between runs."""
    try:
        with open(COVER_CACHE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    changed = False
    for r in recs:
        key = (r["artist"] + " | " + r["title"]).lower()
        if not cache.get(key):                 # misses are retried next load
            url = official_cover(r["artist"], r["title"])
            time.sleep(0.3)
            if url:
                cache[key] = url
                changed = True
        if cache.get(key):
            r["discogs_art"] = r["art"]
            r["art"] = cache[key]
    if changed:
        try:
            with open(COVER_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        except Exception:
            pass
    found = sum(1 for r in recs if r.get("discogs_art"))
    log(f"crate: official covers for {found}/{len(recs)} records")
    return recs


def shuffle_crate():
    import random
    if _CRATE:
        cur = (get_state().get("crate") or {}).get("title")
        choices = [r for r in _CRATE if r["title"] != cur] or _CRATE
        set_state(crate=random.choice(choices))


def crate_loop():
    user = cfg("discogs_username")
    if not user:
        log("crate: no discogs_username, skipping")
        return
    rotate = int(cfg("crate_rotate_minutes", 20)) * 60
    loaded_at = 0
    while True:
        try:
            if time.time() - loaded_at > 12 * 3600 or not _CRATE:
                recs = load_crate(user)
                if recs:
                    _CRATE[:] = recs            # show something straight away
                    recs = swap_covers([dict(r) for r in recs])
                if recs:
                    _CRATE[:] = recs
                    loaded_at = time.time()
                    log(f"crate: {len(recs)} records in {user}'s collection")
                else:
                    log(f"crate: {user}'s collection came back empty — is it set to public?")
            shuffle_crate()
        except urllib.error.HTTPError as e:
            log("crate:", e.code, "— check discogs_username, and that the collection is public")
            time.sleep(1800)
            continue
        except Exception as e:
            log("crate:", e)
        time.sleep(rotate)


# ------------------------------------------------------------ liner notes
# Producer and sample credits from Genius, for whatever's on the platter.

_NOTES_CACHE = {}


def track_key(np_):
    return ((np_ or {}).get("artist", "") + "|" + (np_ or {}).get("title", "")).lower()


def _clean_title(t):
    """'Song - Remastered 2011 (feat. X)' -> 'Song'. Genius search chokes on
    Spotify's edition suffixes."""
    t = re.sub(r"\s*[\(\[][^\)\]]*(feat|with|remaster|version|edit|live|mono|stereo|deluxe)[^\)\]]*[\)\]]",
               "", t, flags=re.I)
    t = re.sub(r"\s+-\s+.*(remaster|version|edit|live|mono|stereo|mix).*$", "", t, flags=re.I)
    return t.strip()


def _genius(path):
    token = cfg("genius_token")
    req = urllib.request.Request("https://api.genius.com" + path,
                                 headers={"Authorization": "Bearer " + token,
                                          "User-Agent": "desk-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8")).get("response") or {}


def fetch_notes(title, artist):
    title_c = _clean_title(title)
    first = _fold(re.split(r",|&| x | and ", artist)[0])
    hits = _genius("/search?q=" + urllib.parse.quote(f"{title_c} {artist.split(',')[0]}")).get("hits") or []

    song_id = None
    for h in hits:
        r_ = h.get("result") or {}
        pa = _fold((r_.get("primary_artist") or {}).get("name", ""))
        if first and (first in pa or pa in first):
            song_id = r_.get("id")
            break
    if not song_id:
        return None

    song = _genius(f"/songs/{song_id}?text_format=plain").get("song") or {}
    producers = [a.get("name", "") for a in song.get("producer_artists") or [] if a.get("name")]
    samples = []
    for rel in song.get("song_relationships") or []:
        if rel.get("relationship_type") in ("samples", "interpolates"):
            for sng in rel.get("songs") or []:
                samples.append({
                    "title": sng.get("title", ""),
                    "artist": (sng.get("primary_artist") or {}).get("name", ""),
                    "kind": rel.get("relationship_type"),
                })
    return {"producers": producers[:4], "samples": samples[:3], "url": song.get("url", "")}


# ----------------------------------------------------------------- lyrics
# LRCLIB is community-run, needs no key, and carries timestamped lines for a
# good share of tracks - which is what lets the words follow the needle
# instead of just sitting there.

_LYRICS_CACHE = {}


def _parse_lrc(text):
    """[00:12.34] one line -> (12340, "one line"). A line can carry several
    stamps when a phrase repeats, so each one becomes its own entry."""
    out = []
    for raw in (text or "").splitlines():
        stamps = re.findall(r"\[(\d+):(\d+(?:[.:]\d+)?)\]", raw)
        if not stamps:
            continue
        words = re.sub(r"\[[^\]]*\]", "", raw).strip()
        for mins, secs in stamps:
            try:
                at = int(mins) * 60000 + int(float(secs.replace(":", ".")) * 1000)
            except ValueError:
                continue
            out.append([at, words])
    out.sort(key=lambda r: r[0])
    return out


def _lrclib(path, params):
    url = "https://lrclib.net/api/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "B-Side/1.0 (https://github.com/; desk dashboard)"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode() or "null")


def fetch_lyrics(np_):
    artist = (np_.get("artist") or "").split(",")[0].strip()
    title = _clean_title(np_.get("title") or "")
    if not artist or not title:
        return None
    dur = int((np_.get("duration_ms") or 0) / 1000)
    hit = None
    try:                                   # exact match first, duration and all
        p = {"artist_name": artist, "track_name": title}
        if np_.get("album"):
            p["album_name"] = np_["album"]
        if dur:
            p["duration"] = dur
        hit = _lrclib("get", p)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    except Exception:
        raise
    if not hit:                            # then a looser search
        try:
            res = _lrclib("search", {"track_name": title, "artist_name": artist}) or []
            for r in res[:5]:
                if not dur or abs((r.get("duration") or 0) - dur) <= 8:
                    hit = r
                    break
            if not hit and res:
                hit = res[0]
        except Exception:
            return None
    if not hit or hit.get("instrumental"):
        return {"lines": [], "plain": [], "instrumental": bool(hit and hit.get("instrumental"))}
    synced = _parse_lrc(hit.get("syncedLyrics") or "")
    plain = [l.strip() for l in (hit.get("plainLyrics") or "").splitlines()]
    return {"lines": synced, "plain": plain, "instrumental": False,
            "synced": bool(synced)}


def lyrics_loop():
    """Follows the track, not the clock - lyrics only change when the song
    does, so this is cheap even at a two second tick."""
    last = None
    while True:
        try:
            if (CFG.get("modules") or {}).get("lyrics", False):
                np_ = get_state().get("now_playing")
                key = track_key(np_) if np_ else None
                if key != last:
                    last = key
                    if not key:
                        set_state(lyrics=None)
                    else:
                        if key not in _LYRICS_CACHE:
                            got = fetch_lyrics(np_)
                            _LYRICS_CACHE[key] = got
                            if len(_LYRICS_CACHE) > 120:
                                _LYRICS_CACHE.pop(next(iter(_LYRICS_CACHE)))
                            log("lyrics:", key, "->",
                                "none" if not got else
                                "instrumental" if got.get("instrumental") else
                                f"{len(got['lines'])} timed lines" if got.get("synced") else
                                f"{len(got['plain'])} lines, not timed")
                        got = _LYRICS_CACHE[key]
                        set_state(lyrics=dict(got, key=key) if got else {"key": key,
                                  "lines": [], "plain": [], "instrumental": False})
            elif last is not None:
                last = None
                set_state(lyrics=None)
        except Exception as e:
            log("lyrics:", e)
            _LYRICS_CACHE.pop(last, None)
            last = None
            time.sleep(20)
        time.sleep(2)


# ---------------------------------------------------------------- updates
# server.py and index.html are read from the Application Support folder in
# preference to the copies inside the .app, which is what makes updating
# possible without a new build: fetch those two files, drop them in, restart.
# app.py is compiled in and can only change with a new .dmg, so it's checked
# too - just to say so rather than pretend.

UPDATE_FILES = ("server.py", "index.html")
_UPDATE = {"at": 0, "state": None}


def _repo():
    return (cfg("update_repo", "") or "").strip().strip("/")


def _raw(name):
    """Takes either "owner/repo" or a plain base URL, so the files can live
    anywhere public - handy when the source repo itself is private."""
    base = _repo()
    if base.startswith("http://") or base.startswith("https://"):
        url = base.rstrip("/") + "/" + name
    else:
        url = (f"https://raw.githubusercontent.com/{base}/"
               f"{cfg('update_branch', 'main')}/{name}")
    req = urllib.request.Request(url, headers={"User-Agent": "B-Side/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read()


def _digest(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()[:16]


def _here(name):
    """What's running now: the override copy if there is one, else the one
    baked into the app."""
    for d in (USER_DIR, HERE):
        p = os.path.join(d, name)
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    return f.read()
            except Exception:
                pass
    return None


def update_check(force=False):
    if not _repo():
        return {"ok": False, "why": "no_repo"}
    if not force and _UPDATE["state"] and time.time() - _UPDATE["at"] < 1800:
        return _UPDATE["state"]
    out = {"ok": True, "files": [], "needs_build": False, "checked": time.time()}
    try:
        for name in UPDATE_FILES:
            remote = _raw(name)
            mine = _here(name)
            if mine is None or _digest(remote) != _digest(mine):
                out["files"].append(name)
        # Whether app.py changed can't be checked from inside the frozen app,
        # so the panel simply doesn't claim either way.
    except urllib.error.HTTPError as e:
        out = {"ok": False, "why": "private" if e.code in (403, 404) else str(e)}
    except Exception as e:
        out = {"ok": False, "why": str(e)}
    _UPDATE.update(at=time.time(), state=out)
    return out


def update_apply():
    """Write the new files beside the settings, where they win over the ones
    inside the app. Nothing is replaced until every file has arrived."""
    if not _repo():
        return False, "No repository set in settings."
    got = {}
    try:
        for name in UPDATE_FILES:
            got[name] = _raw(name)
    except Exception as e:
        return False, f"Couldn't download: {e}"
    if not all(len(v) > 500 for v in got.values()):
        return False, "That download looked wrong, so nothing was changed."
    try:
        os.makedirs(USER_DIR, exist_ok=True)
        for name, data in got.items():
            tmp = os.path.join(USER_DIR, name + ".new")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(USER_DIR, name))
    except Exception as e:
        return False, f"Couldn't save: {e}"
    log("update: wrote", ", ".join(got))
    _UPDATE.update(at=0, state=None)
    return True, ""


def notes_loop():
    if not cfg("genius_token"):
        log("notes: no genius_token, skipping")
        return
    last = None
    while True:
        try:
            np_ = get_state().get("now_playing")
            key = track_key(np_) if np_ else None
            if key and key != last:
                last = key
                if key not in _NOTES_CACHE:
                    _NOTES_CACHE[key] = fetch_notes(np_.get("title", ""), np_.get("artist", ""))
                    if len(_NOTES_CACHE) > 200:
                        _NOTES_CACHE.pop(next(iter(_NOTES_CACHE)))
                n = _NOTES_CACHE[key]
                set_state(notes=dict(n, key=key) if n else {"key": key})
                if n:
                    log("notes:", key, "->", len(n["producers"]), "producers,",
                        len(n["samples"]), "samples")
        except Exception as e:
            log("notes:", e)
            _NOTES_CACHE.pop(last, None)     # let it retry next time
            last = None
            time.sleep(30)
        time.sleep(3)


# ----------------------------------------------------------------- guests
# Friends scan a QR code, search Spotify from their phone and add a song to
# your queue. Only reachable from your own network, and only these routes.

GUEST_REQUESTS = {}        # track uri -> {"name", "title", "artist", "at"}
_GUEST_LAST = {}           # client ip -> time of their last request
_GUEST_LOCK = threading.Lock()


def lan_ip():
    """This machine's address on the home network, for the QR code."""
    import socket
    s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s_.connect(("10.255.255.255", 1))      # no packet is actually sent
        return s_.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s_.close()


def home_ip():
    """The address phones on your WiFi can reach.

    With a VPN on, the default route goes through the tunnel, so the
    obvious guess is the VPN's address (e.g. 10.2.0.2), which nothing at
    home can reach. Prefer the usual home-router ranges instead."""
    ips = all_ips()
    for prefix in ("192.168.",) + tuple(f"172.{n}." for n in range(16, 32)) + ("10.",):
        for ip in ips:
            if ip.startswith(prefix):
                return ip
    return lan_ip()


def guest_url():
    override = (CFG.get("guest_queue") or {}).get("host")   # pin it if the guess is wrong
    return f"http://{override or home_ip()}:{cfg('port', 8765)}/guest"


def all_ips():
    """Every IPv4 address this machine has, for diagnosing the QR code."""
    import socket
    out = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.add(info[4][0])
    except Exception:
        pass
    return sorted(ip for ip in out if not ip.startswith("127."))


def _spotify_call(method, url, data=None):
    token = spotify_access_token()
    if not token:
        raise ConfigError("Spotify isn't connected")
    hdr = {"Authorization": "Bearer " + token}
    if data:
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=hdr)
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.read().decode("utf-8")
        try:
            return r.status, (json.loads(raw) if raw.strip() else {})
        except ValueError:
            # Some endpoints (queue add) answer 200 with a non-JSON body.
            return r.status, {}


def guest_search(q):
    q = (q or "").strip()[:80]
    if not q:
        return []
    url = ("https://api.spotify.com/v1/search?type=track&limit=8&q="
           + urllib.parse.quote(q))
    _, j = _spotify_call("GET", url)
    out = []
    for t in ((j.get("tracks") or {}).get("items") or []):
        imgs = (t.get("album") or {}).get("images") or []
        out.append({
            "uri": t.get("uri", ""),
            "title": t.get("name", ""),
            "artist": ", ".join(a.get("name", "") for a in t.get("artists") or []),
            "album": (t.get("album") or {}).get("name", ""),
            "art": imgs[-1]["url"] if imgs else "",   # smallest is plenty on a phone
            "explicit": bool(t.get("explicit")),
        })
    return out


def guest_queue(uri, name, title, artist, ip):
    if not re.fullmatch(r"spotify:track:[A-Za-z0-9]{10,40}", uri or ""):
        return False, "That isn't a Spotify track."
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:24] or "A guest"
    cooldown = int((CFG.get("guest_queue") or {}).get("cooldown_seconds", 20))

    with _GUEST_LOCK:
        wait = cooldown - (time.time() - _GUEST_LAST.get(ip, 0))
        if wait > 0:
            return False, f"Give it {int(wait) + 1}s before adding another."
        _GUEST_LAST[ip] = time.time()

    try:
        _spotify_call("POST", "https://api.spotify.com/v1/me/player/queue?uri="
                      + urllib.parse.quote(uri), data=b"")
    except urllib.error.HTTPError as e:
        with _GUEST_LOCK:
            _GUEST_LAST.pop(ip, None)          # a failed add shouldn't cost a turn
        if e.code == 404:
            return False, "Nothing's playing on Spotify right now, so there's no queue to add to."
        if e.code == 403:
            return False, "Spotify won't allow queueing on this account (it needs Premium)."
        if e.code == 401:
            return False, "The host needs to reconnect Spotify."
        return False, f"Spotify said no ({e.code})."

    GUEST_REQUESTS[uri] = {"name": name, "title": title, "artist": artist,
                           "at": time.time()}
    while len(GUEST_REQUESTS) > 60:           # keep it bounded
        GUEST_REQUESTS.pop(next(iter(GUEST_REQUESTS)))
    log(f"guest: {name} queued {artist} - {title}")
    try:
        set_state(queue=spotify_queue())
    except Exception:
        pass
    return True, "Added. It'll play after what's already queued."


def qr_svg(text):
    try:
        import segno
    except ImportError:
        return None
    import io
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2,
                                    dark="#141210", light="#f2ece2")
    return buf.getvalue()

# ------------------------------------------------------------------- demo
# ?demo=1 shows sample live games with your real clubs' crests and colours,
# so you can see match-day mode without waiting for a real kick-off.

_DEMO = {"t0": 0}


def demo_football():
    now = datetime.now()
    if time.time() - _DEMO["t0"] > 120:
        _DEMO["t0"] = time.time()          # restart the demo every 2 minutes
    goal = time.time() - _DEMO["t0"] > 12  # a goal goes in 12s after you open it

    def game(gid, home, hid, away, aid, hs, as_, league, status, hf=True, af=True):
        m = {"id": gid, "home": home, "away": away, "home_id": hid, "away_id": aid,
             "home_followed": hf, "away_followed": af,
             "home_score": hs, "away_score": as_, "league": league,
             "comp": SHORT.get(league, league), "status": status,
             "when": (now - timedelta(minutes=60)).isoformat(),
             "live": True, "finished": False, "alt": "", "alt_label": ""}
        w = watch_for(league)
        m["link"], m["link_label"] = (w[1], "Watch on " + w[0]) if w else ("", "")
        for side, tid in (("home", hid), ("away", aid)):
            info = team_info(tid)
            m[side + "_badge"] = info.get("badge", "")
            m[side + "_colour"] = info.get("colour", "")
            m[side + "_short"] = info.get("short", "")
        return m

    live = [
        game("d1", "Real Madrid", "133738", "Atlético Madrid", "133729",
             2 if goal else 1, 0, "Spanish La Liga", "2H"),
        game("d2", "Arsenal", "133604", "Chelsea", "133610", 0, 0,
             "English Premier League", "1H"),
        game("d3", "Barcelona", "133739", "Paris Saint-Germain", "133714", 2, 1,
             "UEFA Champions League", "HT"),
    ]
    later = [dict(game("d4", "Liverpool", "133602", "Manchester City", "133613", 3, 1,
                       "English Premier League", "FT"),
                  live=False, finished=True, when=(now - timedelta(days=1)).isoformat(),
                  link="https://www.youtube.com/results?search_query=Liverpool+vs+Manchester+City+highlights",
                  link_label="Highlights")]
    nxt = [dict(game("d5", "Juventus", "133676", "Bayern Munich", "133664", None, None,
                     "UEFA Champions League", "NS"),
                live=False, finished=False, when=(now + timedelta(days=1, hours=3)).isoformat(),
                link="", link_label="")]
    return {"live": live, "recent": later, "upcoming": nxt, "error": None}


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    GUEST_PATHS = ("/guest", "/api/guest/search", "/api/guest/info")

    def _is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _guard(self, path):
        """Other devices on the network may only reach the guest page."""
        if self._is_local() or path in self.GUEST_PATHS or path == "/api/guest/queue":
            return True
        self._send(403, json.dumps({"error": "not available"}))
        return False

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._guard(path):
            return

        if path == "/guest":
            try:
                with open(os.path.join(HERE, "guest.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "guest.html missing", "text/plain")
            return

        if path == "/api/guest/info":
            st = get_state()
            np_ = st.get("now_playing") or {}
            self._send(200, json.dumps({
                "ready": bool(st.get("guest_ready")),
                "playing": bool(st.get("spotify_playing")),
                "now": {"title": np_.get("title", ""), "artist": np_.get("artist", "")}
                       if st.get("spotify_playing") else None,
            }))
            return

        if path == "/api/guest/search":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                self._send(200, json.dumps({"results": guest_search((qs.get("q") or [""])[0])}))
            except Exception as e:
                self._send(200, json.dumps({"results": [], "error": "Search isn't working right now."}))
                log("guest search:", e)
            return

        if path == "/api/qr.svg":
            svg = qr_svg(guest_url())
            if not svg:
                self._send(404, "Run: pip install segno", "text/plain")
                return
            self._send(200, svg, "image/svg+xml")
            return
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html missing", "text/plain")
            return
        if path == "/callback":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (qs.get("code") or [""])[0]
            err = (qs.get("error") or [""])[0]
            page = ("<html><body style='background:#141210;color:#f2ece2;"
                    "font-family:system-ui;display:flex;align-items:center;"
                    "justify-content:center;height:100vh;text-align:center'>"
                    "<div><h2>%s</h2><p>%s</p></div></body></html>")
            if err or not code:
                self._send(400, page % ("Spotify connection failed", err or "no code"),
                           "text/html; charset=utf-8")
                return
            try:
                spotify_exchange_code(code)
                set_state(spotify_needs_login=False)
                log("spotify: connected")
                self._send(200, page % ("Spotify connected",
                                        "You can close this tab."),
                           "text/html; charset=utf-8")
            except Exception as e:
                log("spotify: exchange failed:", e)
                self._send(400, page % ("Spotify connection failed", str(e)),
                           "text/html; charset=utf-8")
            return

        if path == "/api/spotify/login":
            url = spotify_login_url()
            if not url:
                self._send(200, json.dumps(
                    {"ok": False, "error": "No spotify_client_id in config.json"}))
                return
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
            self._send(200, json.dumps({"ok": True, "url": url}))
            return

        if path == "/api/art":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            src = (qs.get("u") or [""])[0]
            if not src.startswith(("http://", "https://")):
                self._send(400, b"bad url", "text/plain")
                return
            cached = _ART_CACHE.get(src)
            if cached is None:
                try:
                    req = urllib.request.Request(
                        src, headers={"User-Agent": "desk-dashboard/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        cached = (r.read(),
                                  r.headers.get("Content-Type", "image/jpeg"))
                except Exception as e:
                    log("art:", e)
                    # A 1x1 transparent gif: the image element settles now
                    # rather than holding a connection while it retries.
                    import base64
                    self._send(200, base64.b64decode(
                        "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"),
                        "image/gif")
                    return
                if len(_ART_CACHE) > 24:
                    _ART_CACHE.clear()
                _ART_CACHE[src] = cached
            body, ctype = cached
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/crate":
            self._send(200, json.dumps({"records": _CRATE}))
            return

        if path == "/api/state":
            s = get_state()
            s["v"] = STATE_VERSION
            s["server_time"] = datetime.now().isoformat()
            if "demo=1" in (urllib.parse.urlparse(self.path).query or ""):
                s["football"] = demo_football()
            if (CFG.get("guest_queue") or {}).get("enabled"):
                s["guest_url"] = guest_url()
            s["settings"] = public_settings()
            s["can_identify"] = can_identify()
            self._send(200, json.dumps(s))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._guard(path):
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}

        if path == "/api/crate/shuffle":
            shuffle_crate()
            self._send(200, json.dumps({"ok": True, "crate": get_state().get("crate")}))
            return

        if path == "/api/ac":
            ok, msg = ac_control(data.get("action"), data.get("value"))
            self._send(200, json.dumps({"ok": ok, "message": msg,
                                        "ac": get_state().get("ac")}))
            return

        if path == "/api/lamp":
            ok, msg = lamp_control(str(data.get("id", "")), data.get("action"),
                                   data.get("value"))
            self._send(200, json.dumps({"ok": ok, "message": msg,
                                        "lamps": get_state().get("lamps")}))
            return

        if path == "/api/st/code":
            ok, msg = st_exchange(data.get("code"))
            self._send(200, json.dumps({"ok": ok, "message": msg}))
            return

        if path == "/api/identify":
            # One at a time: a second click while the mic is busy would record
            # the same seven seconds twice and spend two lookups on it.
            if IDENTIFYING.is_set():
                self._send(200, json.dumps({"busy": True}))
                return
            IDENTIFYING.set()
            set_state(identifying=True, listen_note="listening\u2026")
            try:
                result, note = identify_now()
                if result:
                    set_state(now_playing=result, listen_note=note, listen_error=None)
                else:
                    set_state(listen_note=note, listen_error=None)
                self._send(200, json.dumps({"ok": True, "note": note}))
            except Exception as e:
                set_state(listen_error=str(e), listen_note="")
                self._send(200, json.dumps({"ok": False, "error": str(e)}))
            finally:
                set_state(identifying=False)
                IDENTIFYING.clear()
            return

        if path == "/api/playlists":
            self._send(200, json.dumps(spotify_playlists(force=bool(data.get("force")))))
            return

        if path == "/api/playlists/play":
            ok, msg = play_playlist(str(data.get("uri", "")), bool(data.get("shuffle")))
            self._send(200, json.dumps({"ok": ok, "message": msg}))
            return

        if path == "/api/soundbar":
            ok, msg = sb_control(data.get("action"))
            self._send(200, json.dumps({"ok": ok, "message": msg}))
            return

        if path == "/api/football/search":
            self._send(200, json.dumps(search_football(str(data.get("q", "")))))
            return

        if path == "/api/football/league":
            teams = league_teams(data.get("id"))
            self._send(200, json.dumps({"teams": teams}))
            return

        if path == "/api/teams":
            picked = []
            for t in (data.get("teams") or [])[:40]:
                if isinstance(t, dict):
                    name, tid = str(t.get("name", ""))[:60].strip(), str(t.get("id") or "")
                else:
                    name, tid = str(t)[:60].strip(), ""
                if not name:
                    continue
                entry = {"name": name}
                if tid:
                    entry["id"] = tid
                picked.append(entry)
            fb = dict(CFG.get("football") or {})
            fb["teams"] = picked
            fb["enabled"] = bool(data.get("enabled", True))
            save_config({"football": fb})
            WAKE_FOOTBALL.set()
            self._send(200, json.dumps({"ok": True, "settings": public_settings()}))
            return

        if path == "/api/settings":
            patch = data.get("settings") or {}
            allowed = ("service", "colour", "place", "latitude", "longitude",
                       "country", "auto_location", "football", "guest_queue",
                       "setup_done", "always_on_top", "sport", "modules", "layout",
                       "update_repo", "playlists")
            clean = {k: v for k, v in patch.items() if k in allowed}
            if clean.get("auto_location"):
                got = locate()
                if got:
                    clean.update(got)
            ok = save_config(clean)
            WAKE_WEATHER.set()
            self._send(200, json.dumps({"ok": ok, "settings": public_settings()}))
            return

        if path == "/api/update/check":
            self._send(200, json.dumps(update_check(force=bool(data.get("force")))))
            return

        if path == "/api/update/apply":
            ok, why = update_apply()
            self._send(200, json.dumps({"ok": ok, "message": why}))
            return

        if path == "/api/location/search":
            self._send(200, json.dumps({"places": geo_search(str(data.get("q", "")))}))
            return

        if path == "/api/location/set":
            got = {k: data.get(k) for k in ("place", "latitude", "longitude", "country")}
            if got.get("latitude") is None or got.get("longitude") is None:
                self._send(200, json.dumps({"ok": False, "message": "Pick a place from the list."}))
                return
            save_config(dict(got, auto_location=False))
            WAKE_WEATHER.set()
            self._send(200, json.dumps({"ok": True, "settings": public_settings()}))
            return

        if path == "/api/spotify/disconnect":
            try:
                os.remove(TOKEN_PATH)
            except FileNotFoundError:
                pass
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "message": str(e)}))
                return
            _pkce.clear()
            set_state(spotify_needs_login=True, guest_ready=False, queue=[])
            log("spotify: disconnected")
            self._send(200, json.dumps({"ok": True}))
            return

        if path == "/api/settings/locate":
            got = locate() if data.get("auto") else geocode(str(data.get("place", "")))
            if got:
                save_config(dict(got, auto_location=bool(data.get("auto"))))
                WAKE_WEATHER.set()
            self._send(200, json.dumps({"ok": bool(got), "settings": public_settings(),
                                        "message": "" if got else "Couldn't find that place."}))
            return

        if path == "/api/player/open":
            self._send(200, json.dumps(dict(zip(("ok", "message"),
                                                open_player(cfg("service", "spotify"))))))
            return

        if path == "/api/spotify/resume":
            ok, msg = spotify_resume(str(data.get("uri", "")))
            self._send(200, json.dumps({"ok": ok, "message": msg}))
            return

        if path == "/api/spotify/devices":
            try:
                self._send(200, json.dumps({"ok": True, "devices": spotify_devices()}))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "message": "Couldn't load devices."}))
            return
        if path == "/api/spotify/transfer":
            ok, msg = spotify_transfer(str(data.get("id", "")))
            self._send(200, json.dumps({"ok": ok, "message": msg}))
            return

        if path == "/api/spotify/control":
            action, value = data.get("action"), data.get("value")
            service = cfg("service", "spotify")
            if action in ("play", "pause") and get_state().get("now_playing") is None \
                    and not get_state().get("spotify_paused"):
                ok, msg = open_player(service)          # nothing loaded: just start it
            else:
                ok, msg = local_control(service, action, value)
                if not ok and service == "spotify" and _load_tokens().get("refresh_token"):
                    ok, msg = spotify_control(action, value)   # fall back to the web API
            self._send(200, json.dumps({"ok": ok, "message": msg}))
            return

        if path == "/api/guest/queue":
            ok, msg = guest_queue(data.get("uri"), data.get("name"),
                                  str(data.get("title", ""))[:120],
                                  str(data.get("artist", ""))[:120],
                                  self.client_address[0])
            self._send(200, json.dumps({"ok": ok, "message": msg}))
            return

        if path == "/api/nowplaying":
            title = (data.get("title") or "").strip()
            if not title:
                set_state(now_playing=None)
            else:
                set_state(now_playing={
                    "title": title,
                    "artist": (data.get("artist") or "").strip(),
                    "album": "",
                    "art": "",
                    "ts": time.time(),
                    "source": "manual",
                })
            self._send(200, json.dumps({"ok": True}))
            return

        self._send(404, json.dumps({"error": "not found"}))

start_modules()      # after every loop above is defined


def main():
    for fn in (weather_loop, football_loop, player_loop, notes_loop):
        threading.Thread(target=fn, daemon=True).start()

    port = cfg("port", 8765)
    host = "0.0.0.0" if (CFG.get("guest_queue") or {}).get("enabled") else "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"\n  Desk dashboard running at http://localhost:{port}")
    print("  Ctrl-C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")

if __name__ == "__main__":
    main()
