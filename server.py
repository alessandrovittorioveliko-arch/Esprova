import asyncio
import json
import random
import tornado.web
import tornado.websocket

# -----------------------------
# CARICAMENTO TEAMS.JSON
# -----------------------------
def flatten_players(team: dict) -> dict:
    """
    Converte players da dict (ruoli) a lista piatta.
    Prendiamo i primi 11 per semplicità.
    """
    players = team.get("players", [])
    if isinstance(players, dict):
        out = []
        for _, lst in players.items():
            out.extend(lst)
        team["players"] = out[:11]
    return team

def load_teams(path="teams.json") -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    teams = [flatten_players(t) for t in data["teams"]]
    return teams

teams_db = load_teams("teams.json")

# -----------------------------
# STATO IN MEMORIA
# -----------------------------
clients = set()
sim_time = 0  # secondi dall'avvio (1s = 1 minuto di gioco per i live)

def make_match(match_id: int, home: dict, away: dict, start_at: int) -> dict:
    return {
        "id": str(match_id),
        "home_id": home["id"],
        "away_id": away["id"],
        "home": home["name"],
        "away": away["name"],
        "home_data": home,
        "away_data": away,
        "status": "scheduled",   # scheduled | live | finished
        "start_at": start_at,    # quando passa a live (in secondi sim_time)
        "minute": 0,
        "score": {"home": 0, "away": 0},
        "events": []
    }

# Creiamo 5 match usando le prime 10 squadre (5 coppie)
base_teams = teams_db[:10] if len(teams_db) >= 10 else teams_db
pairs = []
for i in range(0, min(len(base_teams), 10), 2):
    if i + 1 < len(base_teams):
        pairs.append((base_teams[i], base_teams[i + 1]))

# Se teams.json avesse meno di 10 squadre, ricicla a caso (fallback)
while len(pairs) < 5 and len(teams_db) >= 2:
    h, a = random.sample(teams_db, 2)
    pairs.append((h, a))

matches = {}
for idx, (h, a) in enumerate(pairs[:5], start=1):
    # scaglioniamo le partenze: 0s, 10s, 20s, 30s, 40s
    matches[str(idx)] = make_match(idx, h, a, start_at=(idx - 1) * 10)

EVENT_PROB = {
    "goal": 0.030,
    "yellow": 0.020,
    "red": 0.003
}

def team_bias(home_strength: int, away_strength: int) -> float:
    # bias semplice in base alla strength (clamp tra 0.35 e 0.65)
    b = 0.5 + (home_strength - away_strength) / 200
    return max(0.35, min(0.65, b))

def pick_player(team: dict) -> str:
    plist = team.get("players") or []
    return random.choice(plist) if plist else "Giocatore"

def add_event(match: dict, ev_type: str, team_side: str, text: str):
    match["events"].append({
        "minute": match["minute"],
        "type": ev_type,
        "team": team_side,
        "text": text
    })

def simulate_one_minute(match: dict) -> bool:
    """
    Ritorna True se il match è cambiato (evento/goal/cartellino).
    """
    home_team = match["home_data"]
    away_team = match["away_data"]

    bias = team_bias(home_team.get("strength", 70), away_team.get("strength", 70))
    changed = False

    # Goal
    if random.random() < EVENT_PROB["goal"]:
        side = "home" if random.random() < bias else "away"
        t = home_team if side == "home" else away_team
        scorer = pick_player(t)
        match["score"][side] += 1
        add_event(match, "goal", side, f"⚽ Gol di {scorer} ({t['name']})")
        changed = True

    # Giallo
    if random.random() < EVENT_PROB["yellow"]:
        side = "home" if random.random() < bias else "away"
        t = home_team if side == "home" else away_team
        player = pick_player(t)
        add_event(match, "yellow", side, f"🟨 Ammonizione: {player} ({t['name']})")
        changed = True

    # Rosso
    if random.random() < EVENT_PROB["red"]:
        side = "home" if random.random() < bias else "away"
        t = home_team if side == "home" else away_team
        player = pick_player(t)
        add_event(match, "red", side, f"🟥 Espulsione: {player} ({t['name']})")
        changed = True

    return changed


# -----------------------------
# HANDLERS
# -----------------------------
class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("index.html")

class MatchHandler(tornado.web.RequestHandler):
    def get(self, match_id):
        if match_id not in matches:
            self.set_status(404)
            self.write("Match non trovato")
            return
        self.render("match.html", match_id=match_id)

class MatchesWebSocket(tornado.websocket.WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        clients.add(self)
        asyncio.create_task(self.send_initial_state())

    async def send_initial_state(self):
        await self.write_message(json.dumps({
            "type": "initial_state",
            "server_time": sim_time,
            "matches": matches
        }))

    def on_close(self):
        clients.discard(self)


# -----------------------------
# SIMULAZIONE + BROADCAST
# -----------------------------
async def simulate_loop():
    global sim_time

    while True:
        await asyncio.sleep(1)
        sim_time += 1

        # Per semplicità inviamo sempre tutti i match (5 match = ok)
        for m in matches.values():
            if m["status"] == "scheduled":
                if sim_time >= m["start_at"]:
                    m["status"] = "live"
                    m["minute"] = 0
                    m["events"] = []
                    m["score"] = {"home": 0, "away": 0}

            elif m["status"] == "live":
                m["minute"] += 1
                simulate_one_minute(m)

                if m["minute"] >= 90:
                    m["status"] = "finished"

        if clients:
            msg = json.dumps({
                "type": "match_update",
                "server_time": sim_time,
                "matches": list(matches.values())
            })

            for c in list(clients):
                try:
                    await c.write_message(msg)
                except Exception:
                    clients.discard(c)


async def main():
    app = tornado.web.Application(
        [
            (r"/", MainHandler),
            (r"/match/([^/]+)", MatchHandler),
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
