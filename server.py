import asyncio
import json
import random
import tornado.web
import tornado.websocket

# =============================================================================
# teams.json (robusto: BOM + vuoto)
# =============================================================================

def flatten_players(team: dict) -> dict:
    players = team.get("players")
    if isinstance(players, dict):
        out = []
        for _, lst in players.items():
            out.extend(lst)
        team["players"] = out  # non taglio: utile se poi vuoi eventi più vari
    elif not isinstance(players, list):
        team["players"] = []
    return team

def load_teams(path="teams.json") -> list[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:  # utf-8-sig rimuove BOM
        raw = f.read().strip()
    if not raw:
        raise ValueError(f"{path} è vuoto.")
    data = json.loads(raw)
    return [flatten_players(t) for t in data["teams"]]

teams_db = load_teams("teams.json")  # id, name, strength, players... [file:5]
if len(teams_db) != 20:
    raise ValueError(f"Attese 20 squadre, trovate: {len(teams_db)}")

teams_by_id = {t["id"]: t for t in teams_db}

# =============================================================================
# Calendario round-robin (38 giornate)
# =============================================================================

def round_robin_pairings(team_ids: list[str]) -> list[list[tuple[str, str]]]:
    n = len(team_ids)
    arr = team_ids[:]
    rounds = []
    for _ in range(n - 1):
        pairs = []
        for i in range(n // 2):
            home = arr[i]
            away = arr[n - 1 - i]
            pairs.append((home, away))
        rounds.append(pairs)

        fixed = arr[0]
        rest = arr[1:]
        rest = [rest[-1]] + rest[:-1]
        arr = [fixed] + rest
    return rounds

team_ids = [t["id"] for t in teams_db]
first_leg = round_robin_pairings(team_ids)
second_leg = [[(away, home) for (home, away) in md] for md in first_leg]
calendar = first_leg + second_leg  # 38 giornate

# =============================================================================
# Stato match (in memoria)
# =============================================================================

clients = set()

current_matchday = 1
match_id_counter = 1

match_ids_by_matchday: dict[int, list[str]] = {}
matches: dict[str, dict] = {}

def create_match(matchday: int, home_id: str, away_id: str) -> dict:
    global match_id_counter
    home = teams_by_id[home_id]
    away = teams_by_id[away_id]

    m = {
        "id": str(match_id_counter),
        "matchday": matchday,
        "home_id": home_id,
        "away_id": away_id,
        "home": home["name"],
        "away": away["name"],
        "home_strength": int(home.get("strength", 70)),
        "away_strength": int(away.get("strength", 70)),
        "status": "scheduled",  # scheduled | live | finished
        "minute": 0,
        "score": {"home": 0, "away": 0},
        "events": [],
        "_standings_applied": False,
    }
    match_id_counter += 1
    return m

for md_index, pairs in enumerate(calendar, start=1):
    match_ids_by_matchday[md_index] = []
    for (h, a) in pairs:
        m = create_match(md_index, h, a)
        matches[m["id"]] = m
        match_ids_by_matchday[md_index].append(m["id"])

def start_matchday(md: int):
    for mid in match_ids_by_matchday[md]:
        m = matches[mid]
        m["status"] = "live"
        m["minute"] = 0
        m["score"] = {"home": 0, "away": 0}
        m["events"] = []
        m["_standings_applied"] = False

def get_matchday_matches(md: int) -> list[dict]:
    return [matches[mid] for mid in match_ids_by_matchday[md]]

def match_public(m: dict) -> dict:
    # payload leggero (sufficiente per index)
    return {
        "id": m["id"],
        "matchday": m["matchday"],
        "home": m["home"],
        "away": m["away"],
        "status": m["status"],
        "minute": m["minute"],
        "score": m["score"],
    }

def match_public_list(md: int) -> list[dict]:
    return [match_public(matches[mid]) for mid in match_ids_by_matchday[md]]

# parte giornata 1
start_matchday(current_matchday)

# =============================================================================
# Classifica (aggiornata a fine giornata)
# =============================================================================

standings = {
    tid: {
        "team_id": tid,
        "name": teams_by_id[tid]["name"],
        "played": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "goals_for": 0,
        "goals_against": 0,
        "points": 0,
    }
    for tid in teams_by_id
}

def apply_match_to_standings(m: dict):
    if m.get("_standings_applied"):
        return

    hid = m["home_id"]
    aid = m["away_id"]
    hs = m["score"]["home"]
    as_ = m["score"]["away"]

    h = standings[hid]
    a = standings[aid]

    h["played"] += 1
    a["played"] += 1

    h["goals_for"] += hs
    h["goals_against"] += as_
    a["goals_for"] += as_
    a["goals_against"] += hs

    if hs > as_:
        h["wins"] += 1
        a["losses"] += 1
        h["points"] += 3
    elif hs < as_:
        a["wins"] += 1
        h["losses"] += 1
        a["points"] += 3
    else:
        h["draws"] += 1
        a["draws"] += 1
        h["points"] += 1
        a["points"] += 1

    m["_standings_applied"] = True

def standings_table() -> list[dict]:
    def key(r):
        gd = r["goals_for"] - r["goals_against"]
        return (r["points"], gd, r["goals_for"], -r["goals_against"], r["name"])

    ordered = sorted(standings.values(), key=key, reverse=True)
    out = []
    for i, r in enumerate(ordered, start=1):
        gd = r["goals_for"] - r["goals_against"]
        out.append({
            "pos": i,
            "team_id": r["team_id"],
            "name": r["name"],
            "played": r["played"],
            "wins": r["wins"],
            "draws": r["draws"],
            "losses": r["losses"],
            "gf": r["goals_for"],
            "ga": r["goals_against"],
            "gd": gd,
            "pts": r["points"],
        })
    return out

# =============================================================================
# Simulazione (backend gestisce tempo/punteggio/eventi) [file:1]
# =============================================================================

EVENT_PROB = {"goal": 0.030, "yellow": 0.020, "red": 0.003}

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def team_bias(home_strength: int, away_strength: int) -> float:
    b = 0.5 + (home_strength - away_strength) / 200
    return clamp(b, 0.35, 0.65)

def pick_player(team_id: str) -> str:
    plist = teams_by_id[team_id].get("players") or []
    return random.choice(plist) if plist else "Giocatore"

def add_event(m: dict, ev_type: str, side: str, text: str):
    m["events"].append({"minute": m["minute"], "type": ev_type, "team": side, "text": text})

def simulate_minute(m: dict):
    bias = team_bias(m["home_strength"], m["away_strength"])

    if random.random() < EVENT_PROB["goal"]:
        side = "home" if random.random() < bias else "away"
        tid = m["home_id"] if side == "home" else m["away_id"]
        scorer = pick_player(tid)
        m["score"][side] += 1
        add_event(m, "goal", side, f"⚽ Gol di {scorer}")

    if random.random() < EVENT_PROB["yellow"]:
        side = "home" if random.random() < bias else "away"
        tid = m["home_id"] if side == "home" else m["away_id"]
        player = pick_player(tid)
        add_event(m, "yellow", side, f"🟨 Ammonizione: {player}")

    if random.random() < EVENT_PROB["red"]:
        side = "home" if random.random() < bias else "away"
        tid = m["home_id"] if side == "home" else m["away_id"]
        player = pick_player(tid)
        add_event(m, "red", side, f"🟥 Espulsione: {player}")

async def broadcast(payload: dict):
    if not clients:
        return
    msg = json.dumps(payload)
    for c in list(clients):
        try:
            await c.write_message(msg)
        except Exception:
            clients.discard(c)

async def simulate_loop():
    global current_matchday

    while True:
        await asyncio.sleep(1)  # 1 secondo = 1 minuto
        standings_changed = False
        switched_from = None

        live_ids = match_ids_by_matchday[current_matchday]

        # simula 1 minuto per tutte le 10 partite della giornata corrente
        for mid in live_ids:
            m = matches[mid]
            if m["status"] != "live":
                continue
            m["minute"] += 1
            simulate_minute(m)
            if m["minute"] >= 90:
                m["status"] = "finished"

        # fine giornata -> aggiorna classifica e passa alla successiva
        if all(matches[mid]["status"] == "finished" for mid in live_ids):
            for mid in live_ids:
                apply_match_to_standings(matches[mid])
            standings_changed = True

            if current_matchday < 38:
                switched_from = current_matchday
                current_matchday += 1
                start_matchday(current_matchday)

        await broadcast({
            "type": "match_update",
            "current_matchday": current_matchday,
            "matches": match_public_list(current_matchday),  # SEMPRE solo la giornata corrente
            "matchday_switched": switched_from is not None,
            "finished_matchday": switched_from,
            "standings": standings_table() if standings_changed else None
        })

# =============================================================================
# Tornado Handlers
# =============================================================================

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("index.html")

class MatchesWebSocket(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        clients.add(self)
        asyncio.create_task(self.send_initial_state())

    async def send_initial_state(self):
        await self.write_message(json.dumps({
            "type": "initial_state",
            "current_matchday": current_matchday,
            "matches": match_public_list(current_matchday),
            "standings": standings_table()
        }))

    def on_close(self):
        clients.discard(self)

async def main():
    app = tornado.web.Application(
        [
            (r"/", MainHandler),
            (r"/ws", MatchesWebSocket),
            (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": "static"}),
        ],
        template_path="templates",
        debug=True
    )
    app.listen(8888)
    print("✅ http://localhost:8888  |  WS: ws://localhost:8888/ws")
    asyncio.create_task(simulate_loop())
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
