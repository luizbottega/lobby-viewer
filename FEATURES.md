# Features: Reveal vs Lobby Viewer

What the original Reveal (v1.7.0) does, and where our version stands.
Fill in the last column together: **Yes** (build it), **No** (skip it) or **Later**.

## Champ select

| Feature | What it does | Ours | Worth it? |
|---|---|---|---|
| Reveal names | Shows your teammates' Riot IDs in ranked | ✅ Done | |
| Multi-search link | Opens the team on OP.GG | ✅ Done (OP.GG only) | |
| More sites | U.GG, FOW, Porofessor and LadderEye as options | ❌ | |
| Auto open with delay | Opens the link by itself, waiting up to X seconds for the whole team | ✅ Partly (opens at 5 players, no delay setting) | |
| Rank | Solo/Duo tier and LP | ✅ Done | |
| Main champions | Top 3 by mastery points | ✅ Better: games and win rate this season | |
| Recent form | Win/loss in recent games | ✅ Better: last 10 games with KDA, CS and favorite | |
| Role guessing | Figures out who plays which role when Riot hides it, from history and spells | ❌ | |
| Off-role and autofill warning | Flags players who are autofilled or not on their usual role | ❌ | |
| Enemy team | Shows the enemy team when the client reveals it (like 5-stacks) | ❌ | |
| Counters | Best counters to each enemy pick (Master+ stats from U.GG, Lolalytics, LadderEye), with gold/CS/kill leads at 15 min | ❌ | |
| Lobby history | Past champ selects, to check after a game or dodge | ✅ Done (last 5) | |

## Our own ideas

| Feature | What it does | Ours | Worth it? |
|---|---|---|---|
| Full season stats | Most played and win rate per champion for the whole season. The League Client only gives about 20 recent games for other players, so this needs another source (see below) | ❌ | Later |

**Sources for full season stats:**
- **Riot personal API key** (official): free, register an app at developer.riotgames.com. Reading a whole season means many requests per player, so it fills in slowly.
- **OP.GG MCP server** (official, free, no key): `https://mcp-api.op.gg/mcp`. Has summoner search, game history and a "renewal" tool. Made for AI assistants; need to check if an app may use it and if it gives per-champion season stats.
- **Paid scrapers** (Apify, parse.bot): cost money per request, not worth it.

## Automation

| Feature | What it does | Ours | Worth it? |
|---|---|---|---|
| Auto accept | Accepts the match for you, with an optional delay | ❌ | |
| Auto ban | Bans a champion from your list in the last seconds of your turn | ❌ | |
| Auto pick | Locks in your hover or a champion from your list per role | ❌ | |
| Hover from the app | Shows a champion as your pick without locking it | ❌ | |
| Auto requeue | Starts a new queue after the game ends | ❌ | |
| Restart long queues | Leaves and requeues when the wait goes past the estimate | ❌ | |
| Pause after dodge | Waits a few minutes after a dodge before queueing again | ❌ | |
| Dodge button | The README mentions it, but the current code has no dodge action | ❌ | |

## In game

| Feature | What it does | Ours | Worth it? |
|---|---|---|---|
| Live scoreboard | Ranks and info for all 10 players during the game | ❌ | |

## Notifications

| Feature | What it does | Ours | Worth it? |
|---|---|---|---|
| Windows alerts | Match found, your turn to pick, team revealed... | ❌ | |
| Phone alerts | Same alerts on your phone through the ntfy app | ❌ | |
| Discord alerts | Same alerts in a Discord channel | ❌ | |

## App

| Feature | What it does | Ours | Worth it? |
|---|---|---|---|
| Your own profile | Your rank and info on the start screen | ❌ | |
| Tray icon | Closing the window keeps it running near the clock | ❌ | |
| Remembers window size | Opens at the same size and place | ❌ | |
| Auto update | Updates itself | ✅ Done (Update button) | |
| Analytics | Sends anonymous usage stats to the developer | ❌ On purpose | |
