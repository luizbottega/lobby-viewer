"""
Lobby Viewer: your own simple champ select reveal for League of Legends (Windows).

Shows every teammate's Riot ID in champ select (even in ranked, where names are hidden),
with rank, win rate, most played champions this season and stats from their last 10 games.
Keeps the last 5 champ selects, so you can check them after a game or a dodge.

Everything comes from YOUR League Client running on this PC (127.0.0.1).
No servers, no analytics, no auto-updates. The only thing that leaves your PC is the
op.gg link, and only if you click the button (or turn on auto open).

Setup:   pip install psutil requests
Run:     double-click lobby_viewer.pyw   (with the League Client open)
Preview: python lobby_viewer.pyw --demo  (fake data, no League needed)
"""

import json
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import psutil
import requests
import urllib3

# ---------------------------------------------------------------- settings

VERSION = "1.1.0"            # raise this with every change, so the Update button sees it
UPDATE_URL = "https://raw.githubusercontent.com/luizbottega/lobby-viewer/main/lobby_viewer.pyw"

POLL_SECONDS = 2
DETAIL_GAMES = 10            # games shown in "last 10"
PAGE_SIZE = 20               # the client returns match history 20 games at a time
MAX_SEASON_GAMES = 100       # most games read per player for "most played this season"
SEASON_START = datetime(2026, 1, 8)   # change this when a new ranked season starts
TOP_CHAMPIONS = 3
SOLO_QUEUE_ID = 420
REMAKE_SECONDS = 300         # games shorter than this are remakes and are skipped
HISTORY_SIZE = 5             # champ selects kept in the history
HISTORY_FILE = Path(__file__).with_name("lobby_history.json")

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
            "champion": names.get(player.get("championId"), "?"),
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
    """Ranked Solo/Duo games this season, reading more pages until the season start."""
    season_start = SEASON_START.timestamp()
    games, page, reached_limit = list(first_page), 0, False
    while True:
        if games and games[-1]["created"] < season_start:
            break                                  # already past the season start
        if len(games) >= MAX_SEASON_GAMES:
            reached_limit = True
            break
        page += 1
        try:
            more = get_games_page(client, puuid, names, page)
        except requests.RequestException:
            break
        if not more:
            break
        games.extend(more)
    season = [g for g in games
              if g["solo"] and not g["remake"] and g["created"] >= season_start]
    return season, reached_limit


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
        "kda": f"{k:.1f} / {d:.1f} / {a:.1f}   KDA {(k + a) / max(d, 1):.2f}",
        "cs": f"{sum(g['cs_min'] for g in last) / n:.1f} CS/min",
        "favorite": f"{champ} x{times}",
        "strip": [[g["win"], g["champion"]] for g in last],
    }


def rank_text(rank):
    if rank["tier"] == "UNRANKED":
        return "Unranked"
    parts = [rank["tier"].title(), rank["division"], f"{rank['lp']} LP"]
    return " ".join(part for part in parts if part)


def winrate_text(rank, season_games, recent_games):
    # League only reports real losses for your own account and for some players, so when
    # losses come back as 0 the season win rate is worked out from their games instead.
    if rank["losses"] > 0:
        total = rank["wins"] + rank["losses"]
        return f"{round(100 * rank['wins'] / total)}%  ({rank['wins']}W {rank['losses']}L, season)"
    if season_games:
        wins = sum(g["win"] for g in season_games)
        return f"{round(100 * wins / len(season_games))}%  ({len(season_games)} ranked games)"
    played = [g for g in recent_games if not g["remake"]]
    if played:
        wins = sum(g["win"] for g in played)
        return f"{round(100 * wins / len(played))}%  (last {len(played)} games)"
    return "No games found"


def top_champions(season_games, reached_limit):
    stats = {}
    for g in season_games:
        games, wins = stats.get(g["champion"], (0, 0))
        stats[g["champion"]] = (games + 1, wins + g["win"])
    best = sorted(stats.items(), key=lambda item: item[1][0], reverse=True)[:TOP_CHAMPIONS]
    return {
        "list": [{"name": name, "games": games, "winrate": round(100 * wins / games)}
                 for name, (games, wins) in best],
        "note": f"last {MAX_SEASON_GAMES} games" if reached_limit else "",
    }


def opgg_link(team, region):
    names = ",".join(f"{p['name']}#{p['tag']}" for p in team)
    return f"https://op.gg/lol/multisearch/{region}?summoners={urllib.parse.quote(names)}"


# ---------------------------------------------------------------- background worker


class Worker(threading.Thread):
    """Polls the client and sends updates to the window through a queue.

    Each player is looked up once per champ select, even if the player list changes while
    people join, so nothing is loaded or drawn twice.
    """

    def __init__(self, out, auto_open):
        super().__init__(daemon=True)
        self.out = out
        self.auto_open = auto_open
        self.pool = ThreadPoolExecutor(max_workers=5)

    def run(self):
        names, region, opened = {}, "br", False
        loaded = set()            # puuids already looked up in this champ select
        last_team = None
        while True:
            try:
                client = find_client()
            except PermissionError as error:
                self.out.put(("status", str(error)))
                time.sleep(5)
                continue
            if not client:
                self.out.put(("status", "Waiting for the League Client..."))
                time.sleep(POLL_SECONDS)
                continue
            try:
                if not names:
                    names = champion_names(client)
                    region = web_region(client)
                team = champ_select_team(client)
            except requests.RequestException:
                self.out.put(("status", "Connecting to the League Client..."))
                time.sleep(POLL_SECONDS)
                continue

            if not team:
                if last_team:
                    self.out.put(("ended",))
                self.out.put(("status", "Connected. Waiting for champ select..."))
                loaded, last_team, opened = set(), None, False
            else:
                if team != last_team:
                    last_team = team
                    self.out.put(("team", team, region))
                    self.out.put(("status", f"Champ select: {len(team)} players found"))
                for p in team:
                    if p["puuid"] and p["puuid"] not in loaded:
                        loaded.add(p["puuid"])
                        self.pool.submit(self._load, client, p, names)
                if len(team) >= 5 and not opened and self.auto_open.get():
                    webbrowser.open(opgg_link(team, region))
                    opened = True
            time.sleep(POLL_SECONDS)

    def _load(self, client, player, names):
        """Sends the quick stats first, then most played this season (needs more games)."""
        puuid = player["puuid"]
        try:
            rank = get_rank(client, puuid)
        except Exception:
            rank = {"tier": "UNRANKED", "division": "", "lp": 0, "wins": 0, "losses": 0}
        try:
            recent = get_games_page(client, puuid, names, 0)
        except Exception:
            recent = []
        info = {"rank": rank_text(rank), "tier": rank["tier"],
                "winrate": winrate_text(rank, [], recent),
                "recent": last_games_summary(recent), "champions": None}
        self.out.put(("player", puuid, dict(info)))

        try:
            season, reached_limit = get_season_games(client, puuid, names, recent)
        except Exception:
            season, reached_limit = [], False
        info["champions"] = top_champions(season, reached_limit)
        info["winrate"] = winrate_text(rank, season, recent)
        self.out.put(("player", puuid, info))


class DemoWorker(threading.Thread):
    """Fake lobby so you can see the window without League open."""

    def __init__(self, out, auto_open):
        super().__init__(daemon=True)
        self.out = out

    def run(self):
        import random
        champs = ["Ahri", "Lux", "Jinx", "Lee Sin", "Thresh", "Garen", "Yasuo", "Ezreal", "Leona"]
        team = [{"name": n, "tag": t, "puuid": str(i)} for i, (n, t) in enumerate(
            [("Robs", "BR1"), ("Miojo Frito", "1234"), ("tilted top", "BR1"),
             ("Jungle Diff", "666"), ("sup gap", "BR2")])]
        self.out.put(("status", "Demo mode: fake champ select"))
        # Players join a few at a time, like a real champ select.
        self.out.put(("team", team[:3], "br"))
        time.sleep(0.5)
        self.out.put(("team", team, "br"))
        for p in team:
            time.sleep(0.3)
            games = [{"champion": random.choice(champs[:5]), "win": random.random() < 0.55,
                      "kills": random.randint(0, 12), "deaths": random.randint(1, 9),
                      "assists": random.randint(0, 15), "cs_min": random.uniform(1, 9),
                      "solo": True, "remake": False, "created": time.time()}
                     for _ in range(random.randint(30, 90))]
            tier = random.choice(["GOLD", "PLATINUM", "EMERALD", "UNRANKED"])
            rank = {"tier": tier, "division": "II", "lp": random.randint(0, 99),
                    "wins": random.randint(20, 80), "losses": 0}
            self.out.put(("player", p["puuid"], {
                "rank": rank_text(rank), "tier": tier,
                "winrate": winrate_text(rank, games, games),
                "recent": last_games_summary(games),
                "champions": top_champions(games, len(games) >= MAX_SEASON_GAMES)}))


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


# ---------------------------------------------------------------- history


def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))[:HISTORY_SIZE]
    except (OSError, ValueError):
        return []


def save_history(history):
    try:
        HISTORY_FILE.write_text(json.dumps(history[:HISTORY_SIZE], ensure_ascii=False),
                                encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- window

BG = "#0f1419"
CARD = "#1a2129"
CARD_HOVER = "#232c36"
TEXT = "#e6e6e6"
MUTED = "#8a96a3"
ACCENT = "#c8aa6e"
WIN = "#2e8b57"
LOSS = "#b03a3a"
TIER_COLORS = {"IRON": "#7a6a62", "BRONZE": "#a5694f", "SILVER": "#a8b2bc", "GOLD": "#d4a84b",
               "PLATINUM": "#4fa3a0", "EMERALD": "#2fa36b", "DIAMOND": "#6b8ff0",
               "MASTER": "#b36ad9", "GRANDMASTER": "#e05555", "CHALLENGER": "#f0d27a",
               "UNRANKED": MUTED}
FONT = "Segoe UI"


def winrate_color(pct):
    return WIN if pct >= 55 else LOSS if pct < 48 else TEXT


class PlayerCard(tk.Frame):
    def __init__(self, parent, player):
        super().__init__(parent, bg=CARD, padx=14, pady=10)
        self.columnconfigure(1, weight=1)

        left = tk.Frame(self, bg=CARD, width=250, height=96)
        left.grid(row=0, column=0, sticky="nw")
        left.grid_propagate(False)
        left.pack_propagate(False)
        tk.Label(left, text=player["name"], bg=CARD, fg=TEXT,
                 font=(FONT, 13, "bold"), anchor="w").pack(fill="x")
        tk.Label(left, text=f"#{player['tag']}", bg=CARD, fg=MUTED,
                 font=(FONT, 9), anchor="w").pack(fill="x")
        self.rank = tk.Label(left, text="Loading...", bg=CARD, fg=MUTED,
                             font=(FONT, 10, "bold"), anchor="w")
        self.rank.pack(fill="x", pady=(6, 0))
        self.winrate = tk.Label(left, text="", bg=CARD, fg=TEXT, font=(FONT, 9), anchor="w")
        self.winrate.pack(fill="x")

        mid = tk.Frame(self, bg=CARD)
        mid.grid(row=0, column=1, sticky="nw", padx=10)
        self.champ_title = tk.Label(mid, text="MOST PLAYED THIS SEASON", bg=CARD, fg=ACCENT,
                                    font=(FONT, 8, "bold"), anchor="w")
        self.champ_title.pack(fill="x")
        self.champ_rows = tk.Frame(mid, bg=CARD)
        self.champ_rows.pack(fill="x")

        right = tk.Frame(self, bg=CARD)
        right.grid(row=0, column=2, sticky="ne")
        tk.Label(right, text="LAST 10 GAMES", bg=CARD, fg=ACCENT, font=(FONT, 8, "bold"),
                 anchor="w").pack(fill="x")
        self.strip = tk.Frame(right, bg=CARD)
        self.strip.pack(fill="x", pady=(2, 4))
        self.recent = tk.Label(right, text="", bg=CARD, fg=TEXT, font=(FONT, 9),
                               justify="left", anchor="w")
        self.recent.pack(fill="x")

    def fill(self, info):
        self.rank.config(text=info["rank"], fg=TIER_COLORS.get(info["tier"], MUTED))
        self.winrate.config(text=f"Win rate: {info['winrate']}")
        self.fill_champions(info.get("champions"))
        self.fill_recent(info.get("recent"))

    def fill_champions(self, champions):
        for child in self.champ_rows.winfo_children():
            child.destroy()
        if champions is None:
            tk.Label(self.champ_rows, text="Reading ranked games...", bg=CARD, fg=MUTED,
                     font=(FONT, 9), anchor="w").pack(fill="x")
            return
        title = "MOST PLAYED THIS SEASON"
        if champions["note"]:
            title += f"  ({champions['note']})"
        self.champ_title.config(text=title)
        if not champions["list"]:
            tk.Label(self.champ_rows, text="No ranked games this season", bg=CARD, fg=MUTED,
                     font=(FONT, 9), anchor="w").pack(fill="x")
            return
        for champ in champions["list"]:
            row = tk.Frame(self.champ_rows, bg=CARD)
            row.pack(fill="x")
            tk.Label(row, text=champ["name"], bg=CARD, fg=TEXT, font=(FONT, 9),
                     width=12, anchor="w").pack(side="left")
            tk.Label(row, text=f"{champ['games']} games", bg=CARD, fg=MUTED, font=(FONT, 9),
                     width=9, anchor="w").pack(side="left")
            tk.Label(row, text=f"{champ['winrate']}%", bg=CARD, font=(FONT, 9, "bold"),
                     fg=winrate_color(champ["winrate"]), anchor="w").pack(side="left")

    def fill_recent(self, recent):
        # Clear first, so drawing the same player again never adds a second set of boxes.
        for child in self.strip.winfo_children():
            child.destroy()
        if not recent:
            self.recent.config(text="No recent games")
            return
        for win, champ in recent["strip"]:
            box = tk.Label(self.strip, text="W" if win else "L", width=2,
                           bg=WIN if win else LOSS, fg="white", font=(FONT, 8, "bold"))
            box.pack(side="left", padx=1)
            Tooltip(box, champ)
        self.recent.config(
            text=f"{recent['record']}    {recent['cs']}\n{recent['kda']}\nFavorite: {recent['favorite']}")


class Tooltip:
    """Shows the champion name when hovering a W/L box."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _):
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + 22
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#000", fg="white", font=(FONT, 8),
                 padx=4, pady=1).pack()

    def hide(self, _):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App(tk.Tk):
    def __init__(self, demo):
        super().__init__()
        self.title(f"Lobby Viewer {VERSION}")
        self.geometry("920x760")
        self.minsize(840, 420)
        self.configure(bg=BG)

        self.demo = demo
        self.updates = queue.Queue()
        self.auto_open = tk.BooleanVar(value=False)
        self.history = load_history()        # newest first; [0] is the current lobby while live
        self.live = None                     # history entry of the champ select happening now
        self.view = ("current", None)

        top = tk.Frame(self, bg=BG, padx=14, pady=10)
        top.pack(fill="x")
        tk.Label(top, text="LOBBY VIEWER", bg=BG, fg=ACCENT,
                 font=(FONT, 14, "bold")).pack(side="left")
        self.tab_current = self.tab_button(top, "Current", lambda: self.show("current"))
        self.tab_history = self.tab_button(top, "History", lambda: self.show("history"))
        tk.Button(top, text="Open op.gg", command=self.open_opgg, bg=ACCENT, fg="#111",
                  relief="flat", font=(FONT, 9, "bold"), padx=10).pack(side="right")
        self.update_button = tk.Button(top, text="Update", command=self.update_app, bg=CARD,
                                       fg=MUTED, relief="flat", font=(FONT, 9), padx=10)
        self.update_button.pack(side="right", padx=(0, 8))
        tk.Checkbutton(top, text="Auto open op.gg", variable=self.auto_open, bg=BG, fg=MUTED,
                       selectcolor=BG, activebackground=BG, activeforeground=TEXT,
                       font=(FONT, 9)).pack(side="right", padx=10)

        self.status = tk.Label(self, text="Starting...", bg=BG, fg=MUTED, font=(FONT, 9),
                               anchor="w", padx=14)
        self.status.pack(fill="x")

        self.body = tk.Frame(self, bg=BG, padx=14, pady=8)
        self.body.pack(fill="both", expand=True)
        self.show("current")

        worker = DemoWorker if demo else Worker
        worker(self.updates, self.auto_open).start()
        self.after(100, self.process_updates)
        self.latest = None
        threading.Thread(target=self.check_update, daemon=True).start()

    def tab_button(self, parent, text, command):
        button = tk.Button(parent, text=text, command=command, relief="flat", bg=BG, fg=MUTED,
                           activebackground=CARD, activeforeground=TEXT, font=(FONT, 10),
                           padx=10, bd=0)
        button.pack(side="left", padx=(16 if text == "Current" else 2, 0))
        return button

    # ---- updates from the worker

    def process_updates(self):
        while not self.updates.empty():
            kind, *data = self.updates.get()
            if kind == "status":
                self.status.config(text=data[0])
            elif kind == "team":
                self.on_team(*data)
            elif kind == "player":
                self.on_player(*data)
            elif kind == "ended":
                self.live = None
            elif kind == "update_available":
                self.update_button.config(text="Update available", bg=ACCENT, fg="#111")
            elif kind == "restart":
                subprocess.Popen([sys.executable, str(Path(__file__))])
                self.destroy()
                return
        self.after(100, self.process_updates)

    def on_team(self, team, region):
        if self.live is None:
            # A new champ select: it goes to the top of the history right away, so it is
            # there even if someone dodges a second later.
            self.live = {"time": datetime.now().strftime("%d/%m %H:%M"), "region": region,
                         "players": [], "info": {}}
            self.history = [self.live] + self.history[:HISTORY_SIZE - 1]
        self.live["players"] = team
        self.live["region"] = region
        self.save()
        self.show("current")

    def on_player(self, puuid, info):
        if self.live is None:
            return
        self.live["info"][puuid] = info
        self.save()
        if self.view[0] == "current":
            card = self.cards.get(puuid)
            if card:
                card.fill(info)

    # ---- updates

    def check_update(self):
        """Quietly looks for a new version when the app opens."""
        try:
            text = fetch_latest()
        except Exception:
            return
        if version_of(text) > version_of(f'VERSION = "{VERSION}"'):
            self.latest = text
            self.updates.put(("update_available",))

    def update_app(self):
        self.status.config(text="Checking for updates...")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        try:
            text = self.latest or fetch_latest()
        except Exception:
            self.updates.put(("status", "Couldn't reach GitHub. Try again later."))
            return
        if version_of(text) <= version_of(f'VERSION = "{VERSION}"'):
            self.updates.put(("status", f"You already have the latest version ({VERSION})."))
            return
        if self.demo:
            self.updates.put(("status", "Demo mode: an update was found but not installed."))
            return
        try:
            install_update(text)
        except OSError:
            self.updates.put(("status", "Couldn't save the update. Is the folder read-only?"))
            return
        self.updates.put(("status", "Updated! Restarting..."))
        self.updates.put(("restart",))

    def save(self):
        if not self.demo:            # demo lobbies are fake, keep them out of the history file
            save_history(self.history)

    # ---- views

    def clear_body(self):
        self.cards = {}
        for child in self.body.winfo_children():
            child.destroy()

    def show(self, view, lobby=None):
        self.view = (view, lobby)
        self.tab_current.config(fg=ACCENT if view == "current" else MUTED)
        self.tab_history.config(fg=ACCENT if view != "current" else MUTED)
        self.clear_body()
        if view == "current":
            if self.live and self.live["players"]:
                self.draw_lobby(self.live)
            else:
                tk.Label(self.body, text="Your teammates will show up here in champ select.",
                         bg=BG, fg=MUTED, font=(FONT, 11)).pack(pady=60)
        elif view == "history":
            self.draw_history_list()
        else:
            tk.Button(self.body, text="< Back to history", command=lambda: self.show("history"),
                      relief="flat", bg=BG, fg=MUTED, activebackground=BG, bd=0,
                      font=(FONT, 9)).pack(anchor="w", pady=(0, 6))
            self.draw_lobby(lobby)

    def draw_lobby(self, lobby):
        for p in lobby["players"]:
            card = PlayerCard(self.body, p)
            card.pack(fill="x", pady=4)
            self.cards[p["puuid"]] = card
            if p["puuid"] in lobby["info"]:
                card.fill(lobby["info"][p["puuid"]])

    def draw_history_list(self):
        if not self.history:
            tk.Label(self.body, text="Your last 5 champ selects will show up here.",
                     bg=BG, fg=MUTED, font=(FONT, 11)).pack(pady=60)
            return
        for lobby in self.history:
            row = tk.Frame(self.body, bg=CARD, padx=14, pady=10, cursor="hand2")
            row.pack(fill="x", pady=4)
            label = "Now" if lobby is self.live else lobby["time"]
            when = tk.Label(row, text=label, bg=CARD, fg=ACCENT, font=(FONT, 10, "bold"),
                            width=12, anchor="w")
            when.pack(side="left")
            names = ",  ".join(f"{p['name']}#{p['tag']}" for p in lobby["players"])
            text = tk.Label(row, text=names, bg=CARD, fg=TEXT, font=(FONT, 9), anchor="w",
                            justify="left", wraplength=560)
            text.pack(side="left", fill="x", expand=True)
            tk.Button(row, text="op.gg", relief="flat", bg=CARD_HOVER, fg=TEXT, bd=0, padx=8,
                      font=(FONT, 9), command=lambda l=lobby: self.open_opgg(l)).pack(side="right")
            for widget in (row, when, text):
                widget.bind("<Button-1>", lambda _, l=lobby: self.show("lobby", l))

    def open_opgg(self, lobby=None):
        if lobby is None:
            lobby = self.view[1] if self.view[0] == "lobby" else self.live
        if lobby and lobby["players"]:
            webbrowser.open(opgg_link(lobby["players"], lobby["region"]))


if __name__ == "__main__":
    App(demo="--demo" in sys.argv).mainloop()
