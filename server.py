import os
import asyncio
import json
import random

import tornado.web
import tornado.websocket


#trasforma players da dizionario di ruoli a lista unica
# così poi pick_player può scegliere un nome con random.choice.

def flatten_players(team: dict) -> dict:
    # Se "players" è un dizionario (ruoli -> lista nomi),
    # lo appiattisco in una singola lista per semplificare la scelta casuale.
    players = team.get("players")
    if isinstance(players, dict):
        out = []
        for _, lst in players.items():
            out.extend(lst)
        team["players"] = out
    elif not isinstance(players, list):
        # Se non è né dict né list, imposto lista vuota.
        team["players"] = []
    return team


#legge teams.json e restituisce lista di squadre
def load_teams(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:  
        raw = f.read().strip()  
    if not raw:
        raise ValueError(f"{path} è vuoto.")
    data = json.loads(raw)
    return [flatten_players(t) for t in data["teams"]]

#costruisco i percorsi assoluti a partire dalla cartella dei file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEAMS_PATH = os.path.join(BASE_DIR, "teams.json")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

teams_db = load_teams(TEAMS_PATH)

if len(teams_db) != 20:
    raise ValueError("Il file teams.json deve contenere esattamente 20 squadre.")

teams_by_id = {t["id"]: t for t in teams_db}


# Calendario round-robin, ossia andata e ritorno "tutti contro tutti" (38 giornate)

def round_robin_pairings(team_ids: list[str]) -> list[list[tuple[str, str]]]:
    n = len(team_ids)
    arr = team_ids[:]
    rounds = []
    for _ in range(n - 1):
        pairs = []
        for i in range(n // 2):
            home = arr[i]
            #crea il pairing invertendo casa/trasferta a ogni giornata
            away = arr[n - 1 - i]
            pairs.append((home, away))
        rounds.append(pairs)

        fixed = arr[0]
        rest = arr[1:]
        rest = [rest[-1]] + rest[:-1]
        arr = [fixed] + rest
    return rounds


team_ids = [t["id"] for t in teams_db]
# crea calendario andata
first_leg = round_robin_pairings(team_ids)
# crea calendario ritorno invertendo casa/trasferta
second_leg = [[(away, home) for (home, away) in md] for md in first_leg]
calendar = first_leg + second_leg  # 38 giornate



# Stato match (creo un database in memoria con solo strutture python)

#set di connessioni websocket attive
clients: set[tornado.websocket.WebSocketHandler] = set()

current_matchday = 1
match_id_counter = 1

# struttura dati: matchday -> lista di match_id
match_ids_by_matchday: dict[int, list[str]] = {}
matches: dict[str, dict] = {}

# crea un dizionario match con le info squadre, stato della partita.
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
        "_standings_applied": False,# internal flag per evitare di applicare più volte lo stesso match alla classifica
    }

    match_id_counter += 1
    return m

#crea tutti i match e li memorizza in matches
for md_index, pairs in enumerate(calendar, start=1):
    match_ids_by_matchday[md_index] = []
    for (h, a) in pairs:
        m = create_match(md_index, h, a)
        matches[m["id"]] = m
        match_ids_by_matchday[md_index].append(m["id"])


#parte la giornata: imposta tutti i match a "live", minuto 0, punteggio 0-0
def start_matchday(md: int) -> None:
    for mid in match_ids_by_matchday[md]:
        m = matches[mid]
        m["status"] = "live"
        m["minute"] = 0
        m["score"] = {"home": 0, "away": 0}
        m["events"] = []
        m["_standings_applied"] = False


def match_public(m: dict) -> dict:#return type: dict
    # include events così match.html può mostrarli
    return {
        "id": m["id"],
        "matchday": m["matchday"],
        "home_id": m["home_id"],
        "away_id": m["away_id"],
        "home": m["home"],
        "away": m["away"],
        "status": m["status"],
        "minute": m["minute"],
        "score": m["score"],
        "events": m["events"],
    }

#restituisce la lista dei match pubblici da mandare al frontend
#include events così match.html può mostrarli
def match_public_list(md: int) -> list[dict]:
    return [match_public(matches[mid]) for mid in match_ids_by_matchday[md]]


# parte giornata 1
start_matchday(current_matchday)



# Classifica (aggiornata a fine giornata)


#crea dizionario iniziale della classifica
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

#si aggiorna a fine giornata

#applica il risultato del match m alla classifica
def apply_match_to_standings(m: dict) -> None:
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

#restituisce la classifica ordinata come lista
def standings_table() -> list[dict]:
    def key(r: dict):
        gd = r["goals_for"] - r["goals_against"]
        return (r["points"], gd, r["goals_for"], -r["goals_against"], r["name"])#ordinamento per punti, differenza reti, gol fatti, gol subiti (meno è meglio), nome squadra

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


# Simulazione eventi

# probabilità di eventi per minuto
EVENT_PROB = {"goal": 0.030, "yellow": 0.020, "red": 0.003}

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

# bias di squadra in base alla forza
def team_bias(home_strength: int, away_strength: int) -> float:
    b = 0.5 + (home_strength - away_strength) / 200
    return clamp(b, 0.35, 0.65)

#sceglie un giocatore casuale dalla squadra
def pick_player(team_id: str) -> str:
    plist = teams_by_id[team_id].get("players") or []
    return random.choice(plist) if plist else "Giocatore"

#aggiunge un evento alla lista eventi del match
def add_event(m: dict, ev_type: str, side: str, text: str) -> None:
    m["events"].append({"minute": m["minute"], "type": ev_type, "team": side, "text": text})

#simula un minuto di partita
def simulate_minute(m: dict) -> None:
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


# Broadcast aggiornamenti ai client websocket
async def broadcast(payload: dict) -> None:
    #clients=websocket connections
    if not clients:
        return
    msg = json.dumps(payload)
    for c in list(clients):
        try:
            await c.write_message(msg)
        except Exception:
        #ad ogni client se fallisce la connessione lo rimuovo
            clients.discard(c)


# Loop di simulazione principale che gira sempre
async def simulate_loop() -> None:
    global current_matchday

    while True:
        await asyncio.sleep(1)  # 1 secondo = 1 minuto
        standings_changed = False
        switched_from = None

        # prendo gli id dei match della giornata corrente
        live_ids = match_ids_by_matchday[current_matchday]

        for mid in live_ids:
            m = matches[mid]
            if m["status"] != "live":
                continue
            # simula un minuto e incrementa il minuto
            m["minute"] += 1
            simulate_minute(m)
            if m["minute"] >= 90:
                # termina il match
                m["status"] = "finished"

        if all(matches[mid]["status"] == "finished" for mid in live_ids):
            for mid in live_ids:
                apply_match_to_standings(matches[mid])
                # se la classifica è cambiata, lo segnalo a ogni fine giornata
            standings_changed = True

            if current_matchday < 38:
                #se non sei all'ultima giornata, passa alla successiva
                switched_from = current_matchday
                current_matchday += 1
                start_matchday(current_matchday)

        await broadcast({
            # invio aggiornamento match a tutti i client
            "type": "match_update",
            "current_matchday": current_matchday,
            "matches": match_public_list(current_matchday),
            "matchday_switched": switched_from is not None,
            "finished_matchday": switched_from,
            "standings": standings_table() if standings_changed else None,
        })


# Tornado Handlers

#renderizza pagina index.html
class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("index.html")

#renderizza pagina match.html
class MatchHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("match.html")

#interfaccia websocket per inviare aggiornamenti dinamici ai client
class MatchesWebSocket(tornado.websocket.WebSocketHandler):
    # permette connessioni da qualsiasi origine
    def check_origin(self, origin: str) -> bool:
        return True

    def open(self):
        # aggiunge il client al set di connessioni attive quando si connette
        clients.add(self)
        asyncio.create_task(self.send_initial_state())

#invia lo stato iniziale al client appena connesso cosi che ha la pagina completa appena si connette
    async def send_initial_state(self):
        await self.write_message(json.dumps({
            "type": "initial_state",
            "current_matchday": current_matchday,
            "matches": match_public_list(current_matchday),
            "standings": standings_table(),
            "server_time": 0,
        }))

    def on_close(self):
        # rimuove il client dal set di connessioni attive quando si disconnette
        clients.discard(self)


async def main():
    app = tornado.web.Application(
        [
            (r"/", MainHandler),
            (r"/match", MatchHandler),
            (r"/match.html", MatchHandler),  
            (r"/ws", MatchesWebSocket),
            (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": STATIC_DIR}),
        ],
        template_path=TEMPLATES_DIR,
        debug=True,
    )

    app.listen(8888)
    print(" http://localhost:8888 | WS: ws://localhost:8888/ws")

    asyncio.create_task(simulate_loop())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
