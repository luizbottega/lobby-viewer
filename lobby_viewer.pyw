"""
Lobby Viewer: your own simple champ select reveal for League of Legends (Windows).

Shows every teammate's Riot ID in champ select (even in ranked, where names are hidden),
with their role, rank, win rate, most played champions and stats from their last 10 games.
Keeps the last 5 champ selects, so you can check them after a game or a dodge.

Everything comes from YOUR League Client running on this PC (127.0.0.1).
No servers, no analytics. The only things that leave your PC are the op.gg link (when you
open it) and the Update button checking GitHub for a new version.

Setup:   double-click "Lobby Viewer.bat" once (installs psutil, requests and pywebview)
Run:     double-click lobby_viewer.pyw   (with the League Client open)
Preview: python lobby_viewer.pyw --demo  (fake data, no League needed)
"""

import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path


def ensure_packages():
    """Installs missing libraries, so an update that needs a new one still opens."""
    needed = {"psutil": "psutil", "requests": "requests", "webview": "pywebview"}
    missing = [pkg for module, pkg in needed.items() if importlib.util.find_spec(module) is None]
    if missing:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *missing],
                       creationflags=flags, check=False)


ensure_packages()

import psutil  # noqa: E402
import requests  # noqa: E402
import urllib3  # noqa: E402
import webview  # noqa: E402

# ---------------------------------------------------------------- settings

VERSION = "1.2.0"            # raise this with every change, so the Update button sees it
UPDATE_URL = "https://raw.githubusercontent.com/luizbottega/lobby-viewer/main/lobby_viewer.pyw"

POLL_SECONDS = 2
DETAIL_GAMES = 10            # games shown in "last 10"
PAGE_SIZE = 20               # the client returns match history 20 games at a time
MAX_SEASON_GAMES = 100       # most games read per player for "most played"
SEASON_START = datetime(2026, 1, 8)   # change this when a new ranked season starts
TOP_CHAMPIONS = 3
SOLO_QUEUE_ID = 420
SUMMONERS_RIFT_MAP_ID = 11
REMAKE_SECONDS = 300         # games shorter than this are remakes and are skipped
SUPPORT_MAX_CS_PER_MIN = 2.0
HISTORY_SIZE = 5             # champ selects kept in the history
HERE = Path(__file__).parent
HISTORY_FILE = HERE / "lobby_history.json"
SETTINGS_FILE = HERE / "settings.json"
SIZES = {"full": (1100, 780), "panel": (460, 900)}

# The League Client uses a self-signed certificate on 127.0.0.1, so local requests skip it.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------- League Client access


class LeagueClient:
    """Talks to the two local APIs the League Client exposes."""

    def __init__(self, lcu_port, lcu_token, rc_port, rc_token):
        self.lcu_base = f"https://127.0.0.1:{lcu_port}"
        self.lcu_auth = ("riot", lcu_token)
        self.rc_base = f"https://127.0.0.1:{rc_port}"
        self.rc_auth = ("riot", rc_token)
        self.session = requests.Session()
        self.session.verify = False

    def lcu(self, path, timeout=10):
        resp = self.session.get(self.lcu_base + path, auth=self.lcu_auth, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def riot(self, path, timeout=5):
        resp = self.session.get(self.rc_base + path, auth=self.rc_auth, timeout=timeout)
        resp.raise_for_status()
        return resp.json()


def find_client():
    """Reads ports and passwords from the LeagueClientUx.exe command line."""
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] != "LeagueClientUx.exe":
            continue
        try:
            args = " ".join(proc.cmdline())
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            raise PermissionError("Windows blocked reading the League process. Run as administrator.")

        def arg(name):
            match = re.search(rf"--{name}=([^\s\"]+)", args)
            return match.group(1) if match else None

        values = [arg("app-port"), arg("remoting-auth-token"),
                  arg("riotclient-app-port"), arg("riotclient-auth-token")]
        if all(values):
            return LeagueClient(*values)
    return None


# ---------------------------------------------------------------- data


def champion_names(client):
    data = client.lcu("/lol-game-data/assets/v1/champion-summary.json")
    return {c["id"]: c["name"] for c in data if c.get("id", -1) > 0}


def web_region(client):
    try:
        info = client.riot("/riotclient/region-locale")
        return (info.get("webRegion") or info.get("region") or "br").lower()
    except requests.RequestException:
        return "br"


def my_puuid(client):
    try:
        return client.lcu("/lol-summoner/v1/current-summoner").get("puuid", "")
    except requests.RequestException:
        return ""


def champ_select_team(client):
    """The champ select chat room lists everyone's Riot ID, even when names are hidden."""
    data = client.riot("/chat/v5/participants")
    team = []
    for p in data.get("participants", []):
        if "champ-select" in p.get("cid", ""):
            team.append({"name": p.get("game_name", "?"), "tag": p.get("game_tag", ""),
                         "puuid": p.get("puuid", "")})
    return team


def get_rank(client, puuid):
    entry = client.lcu(f"/lol-ranked/v1/ranked-stats/{puuid}").get("queueMap", {}) \
        .get("RANKED_SOLO_5x5", {})
    tier = entry.get("tier") or "UNRANKED"
    if tier == "NONE":
        tier = "UNRANKED"
    division = entry.get("division")
    if division in (None, "", "NA") or tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        division = ""
    return {"tier": tier, "division": division, "lp": entry.get("leaguePoints", 0),
            "wins": entry.get("wins", 0), "losses": entry.get("losses", 0)}


def game_position(game, player):
    """The lane a match history game was played in, in champ select's names."""
    timeline = player.get("timeline", {})
    lane, role = timeline.get("lane"), timeline.get("role")
    if role in ("SUPPORT", "DUO_SUPPORT"):
        minutes = max(game.get("gameDuration", 0) / 60, 1)
        cs = player.get("stats", {}).get("totalMinionsKilled", 0) / minutes
        # "SUPPORT" is also League's fallback when it can't place someone, so off bot lane
        # only trust it for players who barely farmed.
        if lane in ("BOTTOM", "BOT") or cs < SUPPORT_MAX_CS_PER_MIN:
            return "utility"
    return {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "middle", "MID": "middle",
            "BOTTOM": "bottom", "BOT": "bottom"}.get(lane)


def parse_games(data, names):
    """Turns one page of match history into simple game records, newest first."""
    raw = (data.get("games") or {}).get("games") or []
    raw.sort(key=lambda g: g.get("gameCreation", 0), reverse=True)
    games = []
    for g in raw:
        player = g["participants"][0]          # the list only contains this player
        s = player.get("stats", {})
        duration = g.get("gameDuration", 0)
        games.append({
            "id": g.get("gameId"),
            "champion": names.get(player.get("championId"), "?"),
            "champion_id": player.get("championId", 0),
            "spells": [player.get("spell1Id", 0), player.get("spell2Id", 0)],
            "position": game_position(g, player),
            "rift": g.get("mapId") == SUMMONERS_RIFT_MAP_ID,
            "win": bool(s.get("win")),
            "kills": s.get("kills", 0), "deaths": s.get("deaths", 0),
            "assists": s.get("assists", 0),
            "cs_min": (s.get("totalMinionsKilled", 0) + s.get("neutralMinionsKilled", 0))
            / max(duration / 60, 1),
            "solo": g.get("queueId") == SOLO_QUEUE_ID,
            "remake": duration < REMAKE_SECONDS,
            "created": g.get("gameCreation", 0) / 1000,   # seconds since 1970
        })
    return games


def get_games_page(client, puuid, names, page):
    start = page * PAGE_SIZE
    data = client.lcu(f"/lol-match-history/v1/products/lol/{puuid}/matches"
                      f"?begIndex={start}&endIndex={start + PAGE_SIZE - 1}", timeout=15)
    return parse_games(data, names)


def get_season_games(client, puuid, names, first_page):
    """Ranked Solo/Duo games this season, reading older pages until the season start.

    Returns the games and whether the whole season was covered. For other players the
    client often sends the same recent games again instead of older ones, so every game is
    counted once (by its id) and reading stops as soon as a page brings nothing new.
    """
    season_start = SEASON_START.timestamp()
    games, seen, page = [], set(), 0
    covered_season = False

    def add(new_games):
        added = 0
        for g in new_games:
            key = g["id"] or (g["created"], g["champion"])
            if key not in seen:
                seen.add(key)
                games.append(g)
                added += 1
        return added

    add(first_page)
    while len(games) < MAX_SEASON_GAMES:
        if games and min(g["created"] for g in games) < season_start:
            covered_season = True                  # reached games from before the season
            break
        page += 1
        try:
            more = get_games_page(client, puuid, names, page)
        except requests.RequestException:
            break
        if not more or add(more) == 0:
            break                                  # no older games available
    season = [g for g in games
              if g["solo"] and not g["remake"] and g["created"] >= season_start]
    return season, covered_season


def last_games_summary(games):
    last = [g for g in games if not g["remake"]][:DETAIL_GAMES]
    if not last:
        return None
    n = len(last)
    k = sum(g["kills"] for g in last) / n
    d = sum(g["deaths"] for g in last) / n
    a = sum(g["assists"] for g in last) / n
    wins = sum(g["win"] for g in last)
    champ, times = Counter(g["champion"] for g in last).most_common(1)[0]
    return {
        "record": f"{wins}W {n - wins}L",
        "kda": f"{k:.1f} / {d:.1f} / {a:.1f}",
        "ratio": f"{(k + a) / max(d, 1):.2f}",
        "cs": f"{sum(g['cs_min'] for g in last) / n:.1f}",
        "favorite": f"{champ} x{times}",
        "strip": [[g["win"], g["champion"]] for g in last],
    }


def rank_text(rank):
    if rank["tier"] == "UNRANKED":
        return "Unranked"
    parts = [rank["tier"].title(), rank["division"], f"{rank['lp']} LP"]
    return " ".join(part for part in parts if part)


def winrate(rank, season_games, recent_games):
    """(percent or None, explanation). League only reports real losses for some players, so
    when losses come back as 0 the win rate is worked out from their games instead."""
    if rank["losses"] > 0:
        total = rank["wins"] + rank["losses"]
        return round(100 * rank["wins"] / total), f"{rank['wins']}W {rank['losses']}L, season"
    if season_games:
        wins = sum(g["win"] for g in season_games)
        return round(100 * wins / len(season_games)), f"last {len(season_games)} ranked games"
    played = [g for g in recent_games if not g["remake"]]
    if played:
        wins = sum(g["win"] for g in played)
        return round(100 * wins / len(played)), f"last {len(played)} games"
    return None, "no games found"


def top_champions(season_games, covered_season):
    stats = {}
    for g in season_games:
        games, wins = stats.get(g["champion"], (0, 0))
        stats[g["champion"]] = (games + 1, wins + g["win"])
    best = sorted(stats.items(), key=lambda item: item[1][0], reverse=True)[:TOP_CHAMPIONS]
    return {
        "list": [{"name": name, "games": games, "winrate": round(100 * wins / games)}
                 for name, (games, wins) in best],
        "title": "MOST PLAYED THIS SEASON" if covered_season
        else f"MOST PLAYED · LAST {len(season_games)} RANKED",
    }


def opgg_link(players, region):
    names = ",".join(f"{p['name']}#{p['tag']}" for p in players)
    return f"https://op.gg/lol/multisearch/{region}?summoners={urllib.parse.quote(names)}"


# ---------------------------------------------------------------- role guessing
#
# In ranked, Riot hides which champ select slot belongs to which teammate. Their recent games
# usually give them away: the lanes they play, the summoner spells they take (people keep
# the same spells in the same keys) and the champions they play. Every slot is scored
# against every teammate and the best overall match wins, so two players never share a slot.


def play_profile(games):
    rift = [g for g in games if g.get("rift")]
    return {
        "games": len(rift),
        "positions": Counter(g["position"] for g in rift if g["position"]),
        "spells": Counter(tuple(g["spells"]) for g in rift),
        "champions": Counter(g["champion_id"] for g in rift if g["champion_id"]),
    }


def slot_score(profile, slot):
    if not profile or not profile["games"]:
        return 0.0
    games = profile["games"]
    position_weight = 0.25 if slot["autofilled"] else 1.0   # autofilled: history says little
    score = position_weight * profile["positions"][slot["position"]] / games
    d, f = slot["spells"]
    score += (profile["spells"][(d, f)] + 0.25 * profile["spells"][(f, d)]) / games
    if slot["champion_id"]:
        score += 2.0 * profile["champions"][slot["champion_id"]] / games
    return score


def best_assignment(scores):
    """For each candidate, the slot index it most likely has. Tries every option (5! = 120)."""
    n_slots = len(scores[0]) if scores else 0
    best = [float("-inf"), [None] * len(scores)]

    def search(i, total, used, current):
        if i == len(scores):
            if total > best[0]:
                best[0], best[1] = total, list(current)
            return
        if len(scores) - i > n_slots - len(used):  # more players than slots: allow skipping
            current.append(None)
            search(i + 1, total, used, current)
            current.pop()
        for s in range(n_slots):
            if s not in used:
                used.add(s)
                current.append(s)
                search(i + 1, total + scores[i][s], used, current)
                current.pop()
                used.discard(s)

    search(0, 0.0, set(), [])
    return best[1]


def guess_roles(team, session, me, profiles, names):
    """Role, pick order and champion for each teammate, by puuid."""
    my_team = sorted(session.get("myTeam") or [], key=lambda m: m.get("cellId", 0))
    local_cell = session.get("localPlayerCellId")
    team_ids = {p["puuid"] for p in team}
    roles, open_slots = {}, []

    for order, member in enumerate(my_team, start=1):
        champ = member.get("championId") or member.get("championPickIntent") or 0
        info = {"position": member.get("assignedPosition") or "", "order": order,
                "autofilled": bool(member.get("isAutofilled")), "guessed": False,
                "champion": names.get(champ, "") if champ else ""}
        puuid = member.get("puuid") or (me if member.get("cellId") == local_cell else "")
        if puuid in team_ids:
            roles[puuid] = info                    # Riot shows who this is
        else:
            open_slots.append({**info, "spells": (member.get("spell1Id", 0),
                                                  member.get("spell2Id", 0)),
                               "champion_id": champ})

    unplaced = [p["puuid"] for p in team if p["puuid"] not in roles]
    if unplaced and open_slots:
        scores = [[slot_score(profiles.get(puuid), slot) for slot in open_slots]
                  for puuid in unplaced]
        for puuid, s in zip(unplaced, best_assignment(scores)):
            if s is not None:
                slot = open_slots[s]
                roles[puuid] = {k: slot[k] for k in ("position", "order", "autofilled", "champion")}
                roles[puuid]["guessed"] = True
    return roles


# ---------------------------------------------------------------- shared state


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, value):
    try:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


class State:
    """Everything the window shows. The worker writes it, the window reads it."""

    def __init__(self, demo):
        self.lock = threading.Lock()
        self.demo = demo
        self.status = "Starting..."
        self.live = None                      # the champ select happening now
        self.history = [] if demo else load_json(HISTORY_FILE, [])[:HISTORY_SIZE]
        settings = {} if demo else load_json(SETTINGS_FILE, {})
        self.auto_open = bool(settings.get("auto_open", False))
        self.layout = settings.get("layout") if settings.get("layout") in SIZES else "full"
        self.update_available = False

    def save_history(self):
        if not self.demo:                     # demo lobbies are fake, keep them out of the file
            save_json(HISTORY_FILE, self.history[:HISTORY_SIZE])

    def save_settings(self):
        if not self.demo:
            save_json(SETTINGS_FILE, {"auto_open": self.auto_open, "layout": self.layout})

    def set_status(self, text):
        with self.lock:
            self.status = text

    def on_team(self, team, region):
        with self.lock:
            if self.live is None:
                # New champ select: saved right away, so it is there even after a dodge.
                self.live = {"time": datetime.now().strftime("%d/%m %H:%M"), "region": region,
                             "players": [], "info": {}, "roles": {}}
                self.history = [self.live] + self.history[:HISTORY_SIZE - 1]
            self.live["players"] = team
            self.live["region"] = region
            self.save_history()

    def on_player(self, puuid, info):
        with self.lock:
            if self.live is not None:
                self.live["info"][puuid] = info
                self.save_history()

    def on_roles(self, roles):
        with self.lock:
            if self.live is not None and roles != self.live.get("roles"):
                self.live["roles"] = roles
                self.save_history()

    def on_ended(self):
        with self.lock:
            self.live = None

    def snapshot(self):
        with self.lock:
            live = next((i for i, lobby in enumerate(self.history) if lobby is self.live), -1)
            return json.loads(json.dumps({
                "status": self.status, "version": VERSION, "layout": self.layout,
                "auto_open": self.auto_open, "update_available": self.update_available,
                "live": live, "history": self.history,
            }))


# ---------------------------------------------------------------- background worker


class Worker(threading.Thread):
    """Polls the client and updates the shared state.

    Each player is looked up once per champ select, even if the player list changes while
    people join, so nothing is loaded twice.
    """

    def __init__(self, state):
        super().__init__(daemon=True)
        self.state = state
        self.pool = ThreadPoolExecutor(max_workers=5)
        self.profiles = {}                    # puuid -> play profile, for role guessing

    def run(self):
        names, region, me, opened = {}, "br", "", False
        loaded, last_team = set(), None
        while True:
            try:
                client = find_client()
            except PermissionError as error:
                self.state.set_status(str(error))
                time.sleep(5)
                continue
            if not client:
                self.state.set_status("Waiting for the League Client...")
                names = {}
                time.sleep(POLL_SECONDS)
                continue
            try:
                if not names:
                    names = champion_names(client)
                    region = web_region(client)
                    me = my_puuid(client)
                team = champ_select_team(client)
            except requests.RequestException:
                self.state.set_status("Connecting to the League Client...")
                time.sleep(POLL_SECONDS)
                continue

            if not team:
                if last_team:
                    self.state.on_ended()
                self.state.set_status("Connected. Waiting for champ select...")
                loaded, last_team, opened, self.profiles = set(), None, False, {}
            else:
                if team != last_team:
                    last_team = team
                    self.state.on_team(team, region)
                    self.state.set_status(f"Champ select · {len(team)} players found")
                for p in team:
                    if p["puuid"] and p["puuid"] not in loaded:
                        loaded.add(p["puuid"])
                        self.pool.submit(self._load, client, p, names)
                try:
                    session = client.lcu("/lol-champ-select/v1/session", timeout=5)
                    self.state.on_roles(guess_roles(team, session, me, self.profiles, names))
                except requests.RequestException:
                    pass
                if len(team) >= 5 and not opened and self.state.auto_open:
                    webbrowser.open(opgg_link(team, region))
                    opened = True
            time.sleep(POLL_SECONDS)

    def _load(self, client, player, names):
        """Sends the quick stats first, then most played (needs more games)."""
        puuid = player["puuid"]
        try:
            rank = get_rank(client, puuid)
        except Exception:
            rank = {"tier": "UNRANKED", "division": "", "lp": 0, "wins": 0, "losses": 0}
        recent = []
        for _ in range(2):                         # the client sometimes times out once
            try:
                recent = get_games_page(client, puuid, names, 0)
                break
            except Exception:
                continue
        self.profiles[puuid] = play_profile(recent)
        wr, note = winrate(rank, [], recent)
        info = {"rank": rank_text(rank), "tier": rank["tier"], "wr": wr, "wr_note": note,
                "recent": last_games_summary(recent), "champions": None}
        self.state.on_player(puuid, dict(info))

        try:
            season, covered_season = get_season_games(client, puuid, names, recent)
        except Exception:
            season, covered_season = [], False
        info["champions"] = top_champions(season, covered_season)
        info["wr"], info["wr_note"] = winrate(rank, season, recent)
        self.state.on_player(puuid, info)


class DemoWorker(threading.Thread):
    """Fake lobby so you can see the window without League open."""

    def __init__(self, state):
        super().__init__(daemon=True)
        self.state = state

    def run(self):
        import random
        champs = ["Ahri", "Lux", "Jinx", "Lee Sin", "Thresh", "Garen", "Yasuo", "Ezreal", "Leona"]
        team = [{"name": n, "tag": t, "puuid": str(i)} for i, (n, t) in enumerate(
            [("TheWicked", "ttv"), ("Miojo Frito", "1234"), ("tilted top", "BR1"),
             ("Jungle Diff", "666"), ("sup gap", "BR2")])]
        positions = ["middle", "bottom", "top", "jungle", "utility"]
        self.state.set_status("Demo mode · fake champ select")
        self.state.on_team(team[:3], "br")
        time.sleep(0.5)
        self.state.on_team(team, "br")
        self.state.on_roles({p["puuid"]: {"position": positions[i], "order": i + 1,
                                          "autofilled": i == 1, "guessed": i != 0,
                                          "champion": "Akshan" if i == 0 else ""}
                             for i, p in enumerate(team)})
        for i, p in enumerate(team):
            time.sleep(0.3)
            games = [{"champion": random.choice(champs[:5]), "win": random.random() < 0.55,
                      "kills": random.randint(0, 12), "deaths": random.randint(1, 9),
                      "assists": random.randint(0, 15), "cs_min": random.uniform(1, 9),
                      "solo": True, "remake": False, "created": time.time()}
                     for _ in range(random.randint(12, 20))]
            tier = random.choice(["GOLD", "PLATINUM", "EMERALD", "MASTER", "UNRANKED"])
            rank = {"tier": tier, "division": "" if tier == "MASTER" else "II",
                    "lp": random.randint(0, 99), "wins": random.randint(20, 80),
                    "losses": random.randint(20, 80) if i % 2 == 0 else 0}
            wr, note = winrate(rank, games, games)
            self.state.on_player(p["puuid"], {
                "rank": rank_text(rank), "tier": tier, "wr": wr, "wr_note": note,
                "recent": last_games_summary(games),
                "champions": top_champions(games, False)})


# ---------------------------------------------------------------- updates


def version_of(text):
    match = re.search(r'^VERSION = "([\d.]+)"', text, re.MULTILINE)
    return tuple(int(n) for n in match.group(1).split(".")) if match else (0,)


def fetch_latest():
    """The newest lobby_viewer.pyw on GitHub, as text."""
    resp = requests.get(UPDATE_URL, timeout=10)
    resp.raise_for_status()
    compile(resp.text, "lobby_viewer.pyw", "exec")   # refuse a broken download
    return resp.text


def install_update(text):
    """Keeps the current file as lobby_viewer.bak, then replaces it with the new version."""
    me = Path(__file__)
    me.with_suffix(".bak").write_text(me.read_text(encoding="utf-8"), encoding="utf-8")
    me.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- window bridge


class Api:
    """What the window can ask Python to do. Names starting with _ stay hidden from it."""

    def __init__(self, state):
        self._state = state
        self._window = None
        self._latest = None
        self._restart = False

    def get_state(self):
        return self._state.snapshot()

    def open_opgg(self, index):
        with self._state.lock:
            lobbies = self._state.history
            lobby = lobbies[index] if 0 <= index < len(lobbies) else None
        if lobby and lobby["players"]:
            webbrowser.open(opgg_link(lobby["players"], lobby.get("region", "br")))

    def set_auto_open(self, value):
        with self._state.lock:
            self._state.auto_open = bool(value)
            self._state.save_settings()

    def set_layout(self, layout):
        if layout not in SIZES:
            return
        with self._state.lock:
            self._state.layout = layout
            self._state.save_settings()
        if self._window:
            self._window.resize(*SIZES[layout])

    def _check_update(self):
        try:
            text = fetch_latest()
        except Exception:
            return
        if version_of(text) > version_of(f'VERSION = "{VERSION}"'):
            self._latest = text
            with self._state.lock:
                self._state.update_available = True

    def update(self):
        self._state.set_status("Checking for updates...")
        try:
            text = self._latest or fetch_latest()
        except Exception:
            self._state.set_status("Couldn't reach GitHub. Try again later.")
            return
        if version_of(text) <= version_of(f'VERSION = "{VERSION}"'):
            self._state.set_status(f"You already have the latest version ({VERSION}).")
            return
        if self._state.demo:
            self._state.set_status("Demo mode: an update was found but not installed.")
            return
        try:
            install_update(text)
        except OSError:
            self._state.set_status("Couldn't save the update. Is the folder read-only?")
            return
        self._state.set_status("Updated! Restarting...")
        self._restart = True
        self._window.destroy()


# ---------------------------------------------------------------- the page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Lobby Viewer</title>
<style>
:root {
  --bg1: #2a2d33; --bg2: #1b1d21; --bg3: #16171a; --side: #202227;
  --text: #d9dce1; --white: #ffffff; --muted: #868b94; --line: #ffffff12;
  --blue: #2d9bff; --good: #4fb6ff; --bad: #ff8a4c;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif;
  color: var(--text); overflow: hidden; user-select: none;
  background: radial-gradient(circle at 75% 15%, var(--bg1) 0%, var(--bg2) 55%, var(--bg3) 100%);
}
button { font: inherit; cursor: pointer; }
.app { display: flex; height: 100%; }
.spacer { flex-grow: 1; }

/* ---- sidebar (full layout) */
nav.side { width: 200px; flex-shrink: 0; position: relative; background: var(--side);
  box-shadow: 4px 0 24px #0000004d; display: flex; flex-direction: column; padding: 28px 0 22px; }
.accent { position: absolute; left: 0; top: 0; width: 4px; height: 170px;
  background: var(--blue); box-shadow: 0 0 12px var(--blue); }
.logo { padding: 0 26px; font-size: 19px; font-weight: 600; font-style: italic; color: var(--white); }
.menu { display: flex; flex-direction: column; gap: 4px; padding: 34px 14px 0; }
.menu h2 { margin: 0 12px 6px; font-size: 13px; font-weight: 600; color: var(--white); }
.menu button { text-align: left; background: none; border: 0; color: var(--muted);
  padding: 8px 12px; border-radius: 8px; font-size: 13px; }
.menu button:hover { background: #ffffff08; color: var(--text); }
.menu button.on { color: var(--blue); }
.menu sup { color: var(--blue); font-size: 9px; margin-left: 2px; }
.toggle { display: flex; align-items: center; gap: 8px; padding: 8px 12px; font-size: 13px; color: var(--muted); cursor: pointer; }
.toggle input { accent-color: var(--blue); }
.actions { display: flex; flex-direction: column; gap: 10px; padding: 0 22px; }
.btn-main { background: var(--blue); color: var(--white); border: 0; border-radius: 20px;
  padding: 10px 0; font-weight: 600; font-size: 12px; box-shadow: 0 0 14px #2d9bff80; }
.btn-main:hover { box-shadow: 0 0 20px #2d9bffb0; }
.btn-ghost { display: flex; align-items: center; justify-content: center; gap: 6px;
  background: transparent; color: var(--muted); border: 1px solid #ffffff1f; border-radius: 20px;
  padding: 8px 0; font-size: 11px; }
.btn-ghost:hover { color: var(--text); border-color: #ffffff38; }
.hot { color: #111 !important; background: #d8ff3d !important; border-color: #d8ff3d !important; box-shadow: 0 0 12px #d8ff3d80; }

/* ---- main area */
main { flex-grow: 1; position: relative; padding: 26px 32px; overflow-y: auto; overflow-x: hidden; }
.bignum { position: absolute; right: 22px; top: -14px; font-size: 220px; font-weight: 200;
  color: #ffffff07; line-height: 1; pointer-events: none; }
.head { display: flex; align-items: flex-end; justify-content: space-between; position: relative; margin-bottom: 16px; }
.title { font-size: 14px; font-weight: 600; color: var(--white); }
.sub { display: flex; align-items: center; gap: 8px; margin-top: 4px; font-size: 11px; color: var(--muted); }
.sub::before { content: ""; width: 16px; height: 2px; border-radius: 2px; background: var(--blue); }
.legend { font-size: 10px; color: var(--muted); display: flex; gap: 16px; }
.list { display: flex; flex-direction: column; gap: 10px; position: relative; }
.empty { padding: 90px 0; text-align: center; color: var(--muted); font-size: 13px; }

.card { display: grid; grid-template-columns: 76px 220px 1fr 220px; gap: 22px; align-items: center;
  background: #ffffff06; border: 1px solid var(--line); border-radius: 14px; padding: 12px 18px; }
.who { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.name { font-size: 15px; font-weight: 600; color: var(--white); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tag { font-size: 11px; font-weight: 400; color: var(--muted); }
.tier { font-size: 12px; font-weight: 600; }
.note { font-size: 10px; color: var(--muted); }
.pills { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 3px; }
.pill { font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 999px; border: 1px solid; }
.pill.warn { color: #ffb86b; border-color: #ffb86b66; }
.pill.dim { color: var(--muted); border-color: #ffffff22; font-weight: 500; }
.label { font-size: 9px; letter-spacing: 1px; color: var(--muted); }
.champs { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.champ { display: grid; grid-template-columns: 96px 66px 40px; font-size: 12px; }
.champ b { font-weight: 600; }
.form { display: flex; flex-direction: column; gap: 6px; }
.formhead { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.bars { display: flex; gap: 3px; }
.bar { width: 9px; height: 22px; border-radius: 4px; }
.small { font-size: 11px; }
.ring { position: relative; width: 70px; height: 70px; }
.ring svg { display: block; width: 100%; height: 100%; }
.ring .num { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-size: 22px; font-weight: 300; }
.ring .num small { font-size: 9px; font-weight: 600; margin-top: 8px; }
.loading { color: var(--muted); font-size: 11px; }

/* ---- history */
.hrow { display: flex; align-items: center; gap: 16px; background: #ffffff06; border: 1px solid var(--line);
  border-radius: 14px; padding: 14px 18px; cursor: pointer; }
.hrow:hover { background: #ffffff0c; }
.when { width: 90px; font-size: 13px; font-weight: 600; color: var(--blue); flex-shrink: 0; }
.hnames { flex-grow: 1; font-size: 12px; line-height: 1.6; }
.hrow .btn-ghost { padding: 6px 12px; flex-shrink: 0; }
.back { background: none; border: 0; color: var(--muted); font-size: 12px; padding: 0; margin-bottom: 4px; align-self: flex-start; }
.back:hover { color: var(--text); }

/* ---- side panel layout */
.ptop { display: none; }
body.panel nav.side { display: none; }
body.panel .ptop { display: block; position: relative; padding: 16px 16px 4px 20px; }
body.panel .ptop .accent { height: 110px; }
.prow { display: flex; align-items: center; gap: 8px; }
.ptabs { display: flex; gap: 16px; align-items: center; padding: 10px 0 2px; font-size: 12px; }
.ptabs button { background: none; border: 0; padding: 0; color: var(--muted); }
.ptabs button.on { color: var(--blue); }
.icon { display: flex; align-items: center; justify-content: center; width: 32px; height: 32px;
  background: transparent; color: var(--muted); border: 1px solid #ffffff1f; border-radius: 16px; padding: 0; }
.icon:hover { color: var(--text); }
body.panel .app { flex-direction: column; }
body.panel main { padding: 8px 14px 16px; min-height: 0; }
body.panel .bignum { font-size: 140px; top: -40px; }
body.panel .head, body.panel .legend { display: none; }
body.panel .list { gap: 8px; }
body.panel .card { grid-template-columns: 56px minmax(0, 1fr); gap: 4px 14px; padding: 12px 14px; align-items: start; }
body.panel .card .ring { grid-row: span 3; width: 56px; height: 56px; }
body.panel .ring .num { font-size: 17px; }
body.panel .champs { flex-direction: row; flex-wrap: wrap; gap: 2px 12px; }
body.panel .champ { display: flex; gap: 4px; font-size: 11px; }
body.panel .champs .label, body.panel .form .label { display: none; }
body.panel .formhead { justify-content: flex-start; }
body.panel .form > .small:last-child { display: none; }
body.panel .bar { width: 8px; height: 16px; }
body.panel .when { width: 64px; }
body.panel .hrow { gap: 10px; padding: 12px 14px; }
</style>
</head>
<body class="full">
<div class="app">
  <nav class="side" aria-label="Main">
    <div class="accent"></div>
    <div class="logo">Lobby Viewer</div>
    <div class="menu">
      <h2>Champ select</h2>
      <button id="m-current" onclick="go('current')">Current lobby</button>
      <button id="m-history" onclick="go('history')">History <sup id="m-count"></sup></button>
    </div>
    <div class="menu">
      <h2>Options</h2>
      <label class="toggle"><input type="checkbox" id="auto" onchange="api().set_auto_open(this.checked)"> Auto open op.gg</label>
      <button onclick="api().set_layout('panel')">Switch to side panel</button>
    </div>
    <div class="spacer"></div>
    <div class="actions">
      <button class="btn-main" onclick="openOpgg()">Open op.gg</button>
      <button class="btn-ghost" id="upd" onclick="api().update()"></button>
    </div>
  </nav>
  <div class="ptop">
    <div class="accent"></div>
    <div class="prow">
      <div class="logo" style="padding:0;font-size:16px">Lobby Viewer</div>
      <div class="spacer"></div>
      <button class="icon" id="upd2" aria-label="Update" onclick="api().update()"></button>
      <button class="icon" id="expand" aria-label="Switch to full layout" onclick="api().set_layout('full')"></button>
      <button class="btn-main" style="padding:8px 14px" onclick="openOpgg()">op.gg</button>
    </div>
    <div class="ptabs">
      <button id="p-current" onclick="go('current')">Current</button>
      <button id="p-history" onclick="go('history')">History</button>
      <div class="spacer"></div>
      <span class="note" id="p-status"></span>
    </div>
  </div>
  <main>
    <div class="bignum" id="bignum" aria-hidden="true"></div>
    <div class="head">
      <div><div class="title" id="h-title"></div><div class="sub" id="h-sub"></div></div>
      <div class="legend"><span>ring = win rate</span><span>bars = last 10 games</span></div>
    </div>
    <div class="list" id="list"></div>
  </main>
</div>
<script>
const NEON = ["#2d9bff", "#ff2e74", "#d8ff3d", "#3ee6e0", "#b06bff"];
const TIERS = { IRON: "#a39187", BRONZE: "#c98b6b", SILVER: "#b9c4ce", GOLD: "#e8c46a",
  PLATINUM: "#5cd1c8", EMERALD: "#47d18f", DIAMOND: "#8fa8ff", MASTER: "#c38bf0",
  GRANDMASTER: "#ff6b6b", CHALLENGER: "#ffe08a", UNRANKED: "#868b94" };
const ROLES = { top: "Top", jungle: "Jungle", middle: "Mid", bottom: "Bot", utility: "Support" };
const ICON_UPDATE = '<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';
const ICON_EXPAND = '<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/></svg>';

let S = null, view = "current", opened = null, lastKey = "";
const api = () => window.pywebview.api;
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const wrColor = (p) => p >= 55 ? "var(--good)" : p < 48 ? "var(--bad)" : "var(--text)";

function go(v) { view = v; opened = null; render(true); }
function openLobby(i) { view = "lobby"; opened = i; render(true); }
function openOpgg() {
  const i = view === "lobby" ? opened : S.live;
  if (i >= 0) api().open_opgg(i);
}

function ring(wr, color) {
  const r = 29, c = 2 * Math.PI * r;
  const len = wr == null ? 0 : (wr / 100) * c;
  const label = wr == null ? "no win rate" : wr + " percent win rate";
  return `<div class="ring"><svg role="img" aria-label="${label}" viewBox="0 0 70 70" style="filter:drop-shadow(0 0 6px ${color})">
    <circle cx="35" cy="35" r="${r}" fill="none" stroke="#ffffff14" stroke-width="4"/>
    <circle cx="35" cy="35" r="${r}" fill="none" stroke="${color}" stroke-width="4" stroke-linecap="round"
      stroke-dasharray="${len.toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 35 35)"/></svg>
    <div class="num" style="color:${color}">${wr == null ? "–" : wr + "<small>%</small>"}</div></div>`;
}

function card(p, info, role, color) {
  const pills = [];
  if (role && role.position) pills.push(`<span class="pill" style="color:${color};border-color:${color}66" title="${role.guessed ? "Guessed from their recent games" : "Shown by League"}">${ROLES[role.position] || esc(role.position)}${role.guessed ? " ?" : ""}</span>`);
  if (role && role.autofilled) pills.push('<span class="pill warn">Autofill</span>');
  if (role && role.champion) pills.push(`<span class="pill dim">${esc(role.champion)}</span>`);
  const who = `<div class="who">
      <div class="name">${esc(p.name)} <span class="tag">#${esc(p.tag)}</span></div>
      ${info ? `<div class="tier" style="color:${TIERS[info.tier] || "var(--muted)"}">${esc(info.rank)}</div>
      <div class="note">${esc(info.wr_note || info.winrate || "")}</div>` : '<div class="loading">Loading…</div>'}
      ${pills.length ? `<div class="pills">${pills.join("")}</div>` : ""}</div>`;
  let champs = '<div class="champs"><div class="loading">Reading ranked games…</div></div>';
  if (info && info.champions) {
    const rows = info.champions.list.map((c) => `<div class="champ"><span style="color:var(--white)">${esc(c.name)}</span>
      <span style="color:var(--muted)">${c.games} ${c.games === 1 ? "game" : "games"}</span>
      <b style="color:${wrColor(c.winrate)}">${c.winrate}%</b></div>`).join("");
    champs = `<div class="champs"><div class="label">${esc(info.champions.title || "MOST PLAYED")}</div>
      ${rows || '<div class="loading">No ranked games this season</div>'}</div>`;
  }
  let form = '<div class="form"></div>';
  const r = info && info.recent;
  if (r) {
    const bars = r.strip.map(([win, champ]) => `<div class="bar" title="${esc(champ)}: ${win ? "win" : "loss"}"
      style="background:${color};opacity:${win ? 1 : 0.22};box-shadow:0 0 6px ${color}"></div>`).join("");
    form = `<div class="form">
      <div class="formhead"><span class="label">LAST 10</span><div class="bars">${bars}</div><span class="small" style="color:var(--white)">${esc(r.record)}</span></div>
      <div class="small">${esc(r.kda)} <span style="color:var(--muted)">· KDA</span> ${esc(r.ratio || "")}</div>
      <div class="small" style="color:var(--muted)">${esc(r.cs)} cs/min · fav <span style="color:var(--white)">${esc(r.favorite)}</span></div></div>`;
  } else if (info) {
    form = '<div class="form"><div class="loading">No recent games</div></div>';
  }
  return `<div class="card">${ring(info ? info.wr : null, color)}${who}${champs}${form}</div>`;
}

function lobbyCards(lobby) {
  const roles = lobby.roles || {};
  const players = [...lobby.players].sort((a, b) =>
    ((roles[a.puuid] || {}).order || 99) - ((roles[b.puuid] || {}).order || 99));
  return players.map((p, i) => card(p, (lobby.info || {})[p.puuid], roles[p.puuid], NEON[i % NEON.length])).join("");
}

function render(force) {
  if (!S) return;
  const key = JSON.stringify(S) + view + opened;
  if (!force && key === lastKey) return;
  lastKey = key;

  document.body.className = S.layout;
  document.getElementById("auto").checked = S.auto_open;
  const upd = document.getElementById("upd"), upd2 = document.getElementById("upd2");
  upd.innerHTML = `${ICON_UPDATE} ${S.update_available ? "Update available" : "Update · v" + esc(S.version)}`;
  upd2.innerHTML = ICON_UPDATE;
  upd.classList.toggle("hot", S.update_available);
  upd2.classList.toggle("hot", S.update_available);
  document.getElementById("expand").innerHTML = ICON_EXPAND;
  document.getElementById("m-count").textContent = S.history.length || "";
  for (const v of ["current", "history"]) {
    const on = view === v || (v === "history" && view === "lobby");
    document.getElementById("m-" + v).classList.toggle("on", on);
    document.getElementById("p-" + v).classList.toggle("on", on);
  }
  document.getElementById("p-status").textContent = S.status;

  const list = document.getElementById("list");
  const title = document.getElementById("h-title"), sub = document.getElementById("h-sub");
  const big = document.getElementById("bignum");
  if (view === "current") {
    const lobby = S.live >= 0 ? S.history[S.live] : null;
    title.textContent = "Current lobby";
    sub.textContent = S.status;
    big.textContent = lobby ? String(lobby.players.length).padStart(2, "0") : "";
    list.innerHTML = lobby && lobby.players.length ? lobbyCards(lobby)
      : '<div class="empty">Your teammates will show up here in champ select.</div>';
  } else if (view === "history") {
    title.textContent = "History";
    sub.textContent = "your last 5 champ selects";
    big.textContent = String(S.history.length).padStart(2, "0");
    list.innerHTML = S.history.length ? S.history.map((l, i) => `<div class="hrow" onclick="openLobby(${i})">
        <div class="when">${i === S.live ? "Now" : esc(l.time)}</div>
        <div class="hnames">${l.players.map((p) => esc(p.name) + '<span class="tag">#' + esc(p.tag) + "</span>").join(", &nbsp;")}</div>
        <button class="btn-ghost" onclick="event.stopPropagation(); api().open_opgg(${i})">op.gg</button></div>`).join("")
      : '<div class="empty">Your last 5 champ selects will show up here.</div>';
  } else {
    const lobby = S.history[opened];
    title.textContent = lobby ? "Lobby from " + lobby.time : "Lobby";
    sub.textContent = "saved champ select";
    big.textContent = "";
    list.innerHTML = '<button class="back" onclick="go(\'history\')">‹ Back to history</button>' + (lobby ? lobbyCards(lobby) : "");
  }
}

async function tick() {
  try { S = await api().get_state(); render(false); } catch (e) { console.log(e); }
  setTimeout(tick, 500);
}
let started = false;
function start() {
  if (started || !(window.pywebview && window.pywebview.api && window.pywebview.api.get_state)) return;
  started = true;
  tick();
}
window.addEventListener("pywebviewready", start);
const waitForApi = setInterval(() => { start(); if (started) clearInterval(waitForApi); }, 200);
</script>
</body>
</html>
"""


def show_error(error):
    """The window couldn't open, so say why in a plain Windows message box."""
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(
            "Lobby Viewer",
            "The window couldn't open.\n\n"
            f"{error}\n\n"
            "Your previous version is saved next to this file as lobby_viewer.bak. "
            "Send a print of this message so it can be fixed.")
        root.destroy()
    except Exception:
        pass


def main():
    demo = "--demo" in sys.argv
    state = State(demo)
    api = Api(state)
    w, h = SIZES[state.layout]
    window = webview.create_window(f"Lobby Viewer {VERSION}", html=PAGE, js_api=api,
                                   width=w, height=h, min_size=(440, 500),
                                   background_color="#16171a")
    api._window = window
    (DemoWorker if demo else Worker)(state).start()
    threading.Thread(target=api._check_update, daemon=True).start()
    try:
        webview.start()
    except Exception as error:                 # e.g. Edge WebView2 missing or broken
        show_error(error)
        return
    if api._restart:
        subprocess.Popen([sys.executable, str(Path(__file__))])


if __name__ == "__main__":
    main()
