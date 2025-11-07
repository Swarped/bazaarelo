import os
import re
from datetime import datetime

from flask import Flask, request, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask import jsonify

app = Flask(__name__)
app.secret_key = "supersecret"

# --- Persistent SQLite path ---
BASEDIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASEDIR, "data", "tournament.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{DB_PATH}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# app.py, near your imports
DEFAULT_ELO = 1400  # or whatever starting rating you wan

@app.route('/players/search')
def player_search():
    q = request.args.get('q', '')
    if not q:
        return jsonify([])

    results = Player.query.filter(Player.name.ilike(f"%{q}%")).limit(10).all()
    return jsonify([p.name for p in results])

# --- Models ---
class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    elo = db.Column(db.Integer, default=1400)

class Tournament(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    rounds = db.Column(db.Integer, nullable=False)
    imported_from_text = db.Column(db.Boolean, default=False)

class TournamentPlayer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)

class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    round_num = db.Column(db.Integer, nullable=False)
    player1_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=True)
    player2_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=True)
    result = db.Column(db.String(10))  # "2-0","1-1","0-2","bye"


def ensure_player(name):
    player = Player.query.filter_by(name=name).first()
    if not player:
        player = Player(name=name, elo=1400)
        db.session.add(player)
        db.session.commit()
    return player


# --- Elo update ---
def update_elo(player_a, player_b, score_a, score_b, k=32):
    if score_a > score_b:
        result_a, result_b = 1, 0
    elif score_a < score_b:
        result_a, result_b = 0, 1
    else:
        result_a = result_b = 0.5
    expected_a = 1 / (1 + 10 ** ((player_b.elo - player_a.elo) / 400))
    expected_b = 1 / (1 + 10 ** ((player_a.elo - player_b.elo) / 400))
    player_a.elo += int(k * (result_a - expected_a))
    player_b.elo += int(k * (result_b - expected_b))


# --- Helpers ---
def ensure_player(name: str) -> Player:
    name = name.strip()
    player = Player.query.filter_by(name=name).first()
    if not player:
        player = Player(name=name, elo=1400)
        db.session.add(player)
        db.session.commit()
    return player

def result_to_scores(result: str):
    mapping = {
        "2-0": (2, 0),
        "0-2": (0, 2),
        "1-1": (1, 1),
        "bye": (2, 0),  # treat bye as 2-0 for Elo weighting
    }
    return mapping.get(result)


# --- Helpers update ---
def normalize_points(points_raw: str):
    """
    Normalize EventLink and Arena result tokens into standard forms:
    "2-0", "0-2", "1-1", "bye"
    """
    points_raw = points_raw.strip().lower()

    if "***bye***" in points_raw or points_raw == "bye":
        return "bye"

    if "-" in points_raw:
        left, right = points_raw.split("-", 1)
        left, right = left.strip(), right.strip()

        # Arena-style scores
        if (left, right) == ("2", "1"):
            return "2-0"
        if (left, right) == ("1", "2"):
            return "0-2"
        if (left, right) in [("1", "1"), ("1", "1-1")]:
            return "1-1"

        # EventLink-style scores
        if (left, right) == ("3", "0"):
            return "2-0"
        if (left, right) == ("0", "3"):
            return "0-2"

        try:
            lnum, rnum = int(left), int(right)
            if lnum > rnum:
                return "2-0"
            elif lnum < rnum:
                return "0-2"
            else:
                return "1-1"
        except ValueError:
            return "1-1"

    if points_raw.isdigit():
        val = int(points_raw)
        if val >= 3:  # 3 or 6 both mean win
            return "2-0"
        elif val == 1:
            return "1-1"
        elif val == 0:
            return "0-2"

    return "1-1"


def clean_name(name: str) -> str:
    return re.sub(r"\(\s*\d+\s*pts?\s*\)", "", name).strip()

def parse_arena_text(all_text: str):
    matches = []
    current_round = None

    for raw_line in all_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Round header
        if line.lower().startswith("round "):
            try:
                current_round = int(line.split()[1])
            except Exception:
                current_round = None
            continue

        # Bye line
        if "--- Bye ---" in line:
            parts = line.split("vs")
            player = clean_name(parts[0].strip())
            matches.append({
                "round": current_round,
                "player": player,
                "opponent": None,
                "result": "bye"
            })
            continue

        # Pairing line
        if "vs" in line:
            parts = line.split("vs")
            if len(parts) == 2:
                player, opponent = clean_name(parts[0].strip()), clean_name(parts[1].strip())
                matches.append({
                    "round": current_round,
                    "player": player,
                    "opponent": opponent,
                    "result": None
                })
            continue

        # Result line
        if "wins" in line.lower():
            winner = clean_name(line.split("wins")[0].strip())
            if matches and matches[-1]["round"] == current_round:
                m = matches[-1]
                if m["player"] == winner:
                    m["result"] = "2-0"
                elif m["opponent"] == winner:
                    m["result"] = "0-2"
            continue

        if "draw" in line.lower():
            if matches and matches[-1]["round"] == current_round:
                matches[-1]["result"] = "1-1"
            continue

    # Fallback
    for m in matches:
        if m["result"] is None:
            m["result"] = "1-1"

    return matches



def parse_eventlink_text(all_text: str):
    """
    Parse EventLink 'Pairings by Table' plain text.
    Returns list of dicts: {round, player, opponent, result}
    """
    matches = []
    current_round = None

    for raw_line in all_text.splitlines():
        line = raw_line.strip()

        # Detect round header
        if line.startswith("Round "):
            try:
                current_round = int(line.split()[1])
            except Exception:
                current_round = None
            continue

        # Skip headers/separators
        if not line or line.startswith("Table") or set(line) == set("-"):
            continue
        if line.startswith("EventLink") or line.startswith("Report:") \
           or line.startswith("Event:") or line.startswith("Event Date:") \
           or line.startswith("Event Information:") or "Copyright" in line:
            continue

        # Bye row
        if "***Bye***" in line:
            parts = re.split(r"\s{2,}", line)
            if len(parts) >= 2:
                player = parts[0].strip()
                matches.append({
                    "round": current_round,
                    "player": player,
                    "opponent": None,
                    "result": "bye"
                })
            continue

        # Normal row
        parts = re.split(r"\s{2,}", line)
        if len(parts) < 3:
            continue

        if len(parts) >= 4 and parts[0].strip().isdigit():
            _, player, opponent, points_raw = parts[:4]
        else:
            player, opponent, points_raw = parts[:3]

        result_token = normalize_points(points_raw)

        if current_round and player:
            matches.append({
                "round": current_round,
                "player": player.strip(),
                "opponent": opponent.strip() if opponent else None,
                "result": result_token
            })

    return matches
# --- Routes ---

def clean_name(name: str) -> str:
    return re.sub(r"\(\s*\d+\s*pts?\s*\)", "", name).strip()

def parse_arena_text(all_text: str):
    matches = []
    current_round = None

    for raw_line in all_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Round header
        if line.lower().startswith("round "):
            try:
                current_round = int(line.split()[1])
            except Exception:
                current_round = None
            continue

        # Bye line
        if "--- Bye ---" in line:
            parts = line.split("vs")
            player = clean_name(parts[0].strip())
            matches.append({
                "round": current_round,
                "player": player,
                "opponent": None,
                "result": "bye"
            })
            continue

        # Pairing line
        if "vs" in line:
            parts = line.split("vs")
            if len(parts) == 2:
                player, opponent = clean_name(parts[0].strip()), clean_name(parts[1].strip())
                matches.append({
                    "round": current_round,
                    "player": player,
                    "opponent": opponent,
                    "result": None
                })
            continue

        # Result line
        if "wins" in line.lower():
            winner = clean_name(line.split("wins")[0].strip())
            if matches and matches[-1]["round"] == current_round:
                m = matches[-1]
                if m["player"] == winner:
                    m["result"] = "2-0"
                elif m["opponent"] == winner:
                    m["result"] = "0-2"
            continue

        if "draw" in line.lower():
            if matches and matches[-1]["round"] == current_round:
                matches[-1]["result"] = "1-1"
            continue

    # Fallback
    for m in matches:
        if m["result"] is None:
            m["result"] = "1-1"

    return matches


@app.route('/')
def home():
    return redirect(url_for('players'))


@app.route('/players', methods=['GET', 'POST'])
def players():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if name:
            new_player = Player(name=name)
            db.session.add(new_player)
            db.session.commit()
        return redirect(url_for('players'))

    all_players = Player.query.order_by(Player.elo.desc()).all()
    return render_template('players.html', players=all_players)

@app.route('/reset_db', methods=['POST'])
def reset_db():
    # Danger: this deletes all data!
    db.drop_all()
    db.create_all()
    flash("Database has been reset.", "success")
    return redirect(url_for('players'))



@app.route('/tournament/new', methods=['GET', 'POST'])
def new_tournament():
    if request.method == 'POST':
        workflow = request.form.get('workflow')

        # --- Manual workflow ---
        if workflow == 'manual':
            tournament_name = request.form.get('tournament_name')
            date_str = request.form.get('date')

            # Gather players from multiple <input name="players">
            player_names = request.form.getlist('players')
            player_names = [name.strip() for name in player_names if name.strip()]
            player_objs = [ensure_player(name) for name in player_names]

            # Round count heuristic
            num_players = len(player_objs)
            if num_players <= 8:
                rounds = 3
            elif num_players <= 16:
                rounds = 4
            elif num_players <= 32:
                rounds = 5
            elif num_players <= 64:
                rounds = 6
            else:
                rounds = 7

            tournament = Tournament(
                name=tournament_name,
                date=datetime.strptime(date_str, "%Y-%m-%d"),
                rounds=rounds,
                imported_from_text=False
            )
            db.session.add(tournament)
            db.session.commit()

            for p in player_objs:
                db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=p.id))
            db.session.commit()

            return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

        # --- Import workflow ---
        elif workflow == 'import':
            import_format = request.form.get('import_format')
            raw_text = request.form.get('import_text')

            if import_format == 'eventlink':
                parsed_matches = parse_eventlink_text(raw_text)
            elif import_format == 'arena':
                parsed_matches = parse_arena_text(raw_text)

                # collect all player names
                parsed_names = {m["player"] for m in parsed_matches if m["player"]}
                parsed_names |= {m["opponent"] for m in parsed_matches if m["opponent"]}

                # check against DB
                existing_names = {p.name for p in Player.query.all()}
                unknown_names = parsed_names - existing_names

                if unknown_names:
                     # render confirmation page instead of proceeding
                         return render_template(
                         "confirm_players.html",
                         unknown_names=sorted(list(unknown_names)),
                         existing_players=Player.query.all(),
                         raw_text=raw_text
                    )

    # if no unknowns, continue as normal
    # ... your existing code to build rounds/standings ...

            else:
                flash("Unknown import format selected.", "error")
                return redirect(url_for('new_tournament'))

            if not parsed_matches:
                flash("No matches found in the pasted text.", "error")
                return redirect(url_for('new_tournament'))

            # Collect players
            player_names = set()
            for m in parsed_matches:
                player_names.add(m["player"])
                if m["opponent"]:
                    player_names.add(m["opponent"])
            player_objs = [ensure_player(name) for name in sorted(player_names)]

            # Round count heuristic
            num_players = len(player_objs)
            if num_players <= 8:
                rounds = 3
            elif num_players <= 16:
                rounds = 4
            elif num_players <= 32:
                rounds = 5
            elif num_players <= 64:
                rounds = 6
            else:
                rounds = 7

            tournament = Tournament(date=datetime.today().date(), rounds=rounds, imported_from_text=True)
            db.session.add(tournament)
            db.session.commit()

            for p in player_objs:
                db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=p.id))
            db.session.commit()

            # Insert matches (no Elo yet)
            for m in parsed_matches:
                player = ensure_player(m["player"])
                if m["opponent"] is None:
                    db.session.add(Match(
                        tournament_id=tournament.id,
                        round_num=m["round"],
                        player1_id=player.id,
                        player2_id=None,
                        result="bye"
                    ))
                else:
                    opponent = ensure_player(m["opponent"])
                    db.session.add(Match(
                        tournament_id=tournament.id,
                        round_num=m["round"],
                        player1_id=player.id,
                        player2_id=opponent.id,
                        result=m["result"]
                    ))

            db.session.commit()
            flash(f"Tournament created from {import_format.capitalize()} text and all rounds imported!", "success")
            return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

    return render_template('new_tournament.html')


@app.route('/confirm_players', methods=['POST'])
def confirm_players():
    raw_text = request.form.get("raw_text")
    parsed_matches = parse_arena_text(raw_text)

    # resolve unknown players based on form choices
    for m in parsed_matches:
        for role in ["player", "opponent"]:
            name = m[role]
            if not name:
                continue

            action = request.form.get(f"action_{name}")
            if action == "create":
                player = Player.query.filter_by(name=name).first()
                if not player:
                    player = Player(name=name, elo=DEFAULT_ELO)
                    db.session.add(player)
                    db.session.commit()
                m[role] = player.id
            elif action == "replace":
                replace_id = request.form.get(f"replace_{name}")
                if replace_id:
                    m[role] = int(replace_id)
            else:
                player = Player.query.filter_by(name=name).first()
                if player:
                    m[role] = player.id

    # collect all unique player IDs
    player_ids = set()
    for m in parsed_matches:
        if isinstance(m["player"], int):
            player_ids.add(m["player"])
        if isinstance(m["opponent"], int):
            player_ids.add(m["opponent"])

    # round count heuristic
    num_players = len(player_ids)
    if num_players <= 8:
        rounds = 3
    elif num_players <= 16:
        rounds = 4
    elif num_players <= 32:
        rounds = 5
    elif num_players <= 64:
        rounds = 6
    else:
        rounds = 7

    # create tournament now
    tournament = Tournament(date=datetime.today().date(),
                            rounds=rounds,
                            imported_from_text=True)
    db.session.add(tournament)
    db.session.commit()

    # link players to tournament
    for pid in player_ids:
        db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=pid))
    db.session.commit()

    # insert matches
    for m in parsed_matches:
        match = Match(
            tournament_id=tournament.id,
            round_num=m["round"],
            player1_id=m["player"] if isinstance(m["player"], int) else None,
            player2_id=m["opponent"] if isinstance(m["opponent"], int) else None,
            result=m["result"]
        )
        db.session.add(match)

    db.session.commit()

    # redirect into your existing standings/confirmation screen
    return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

@app.route('/tournament/<int:tid>/round/<int:round_num>', methods=['GET', 'POST'])
def tournament_round(tid, round_num):
    tournament = Tournament.query.get_or_404(tid)
    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id) \
                          .filter(TournamentPlayer.tournament_id == tid).all()
    existing_matches = Match.query.filter_by(tournament_id=tid, round_num=round_num).all()

    standings = None

    # --- Imported from text workflow ---
    if tournament.imported_from_text:
        all_matches = Match.query.filter_by(tournament_id=tid).all()

        # Snapshot original Elos
        original_elos = {p.id: p.elo for p in players}
        elo_changes = {p.id: 0 for p in players}

        # Simulate Elo updates
        for m in all_matches:
            if m.player2_id:
                p1 = Player.query.get(m.player1_id)
                p2 = Player.query.get(m.player2_id)
                scores = result_to_scores(m.result) or (1,1)
                old1, old2 = p1.elo, p2.elo
                update_elo(p1, p2, *scores)
                elo_changes[p1.id] += p1.elo - old1
                elo_changes[p2.id] += p2.elo - old2
                # reset
                p1.elo, p2.elo = old1, old2

        # Build standings
        standings = []
        for p in players:
            wins = draws = losses = points = 0
            for m in all_matches:
                if m.player1_id == p.id or m.player2_id == p.id:
                    if m.result == "bye" and m.player1_id == p.id:
                        wins += 1; points += 3
                    elif m.result in ["2-0","2-1"] and m.player1_id == p.id:
                        wins += 1; points += 3
                    elif m.result in ["0-2","1-2"] and m.player2_id == p.id:
                        wins += 1; points += 3
                    elif m.result == "1-1":
                        draws += 1; points += 1
                    else:
                        losses += 1
            standings.append({
                "player": p,
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "points": points,
                "elo_delta": elo_changes[p.id]
            })

        standings.sort(key=lambda s: (s["points"], s["wins"]), reverse=True)

        # Render with standings preview
        return render_template(
            'round.html',
            players=players,
            round_num=round_num,
            tid=tid,
            matches=existing_matches,
            tournament=tournament,
            standings=standings
        )

    # --- Manual entry workflow ---
    if request.method == 'POST':
        for i in range(1, (len(players) + 1)//2 + 1):
            p1_val = request.form.get(f'player1_{i}')
            p2_val = request.form.get(f'player2_{i}')
            result = request.form.get(f'result_{i}')

            if not p1_val or not p2_val:
                continue

            if p1_val == p2_val and p1_val not in ("bye", ""):
                flash("Error: A player cannot face themselves.", "error")
                return redirect(url_for('tournament_round', tid=tid, round_num=round_num))

            if p1_val == "bye" or p2_val == "bye":
                match = Match(
                    tournament_id=tid,
                    round_num=round_num,
                    player1_id=None if p1_val == "bye" else int(p1_val),
                    player2_id=None if p2_val == "bye" else int(p2_val),
                    result="bye"
                )
                db.session.add(match)
                continue

            score_map = result_to_scores(result)
            if not score_map:
                flash("Error: Invalid result selected.", "error")
                return redirect(url_for('tournament_round', tid=tid, round_num=round_num))

            p1_id, p2_id = int(p1_val), int(p2_val)
            db.session.add(Match(
                tournament_id=tid,
                round_num=round_num,
                player1_id=p1_id,
                player2_id=p2_id,
                result=result
            ))

            player1 = Player.query.get(p1_id)
            player2 = Player.query.get(p2_id)
            games_a, games_b = score_map
            update_elo(player1, player2, games_a, games_b)

        db.session.commit()

        if round_num < tournament.rounds:
            return redirect(url_for('tournament_round', tid=tournament.id, round_num=round_num + 1))
        else:
            return redirect(url_for('players'))

    return render_template(
        'round.html',
        players=players,
        round_num=round_num,
        tid=tid,
        matches=existing_matches,
        tournament=tournament,
        standings=standings
    )


@app.route('/tournament/<int:tid>/confirm_import', methods=['GET','POST'])
def confirm_import(tid):
    tournament = Tournament.query.get_or_404(tid)
    matches = Match.query.filter_by(tournament_id=tournament.id).all()
    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id) \
                          .filter(TournamentPlayer.tournament_id == tid).all()

    # Snapshot original Elos
    original_elos = {p.id: p.elo for p in players}
    elo_changes = {p.id: 0 for p in players}

    # Simulate Elo updates
    for m in matches:
        if m.player2_id:
            p1 = Player.query.get(m.player1_id)
            p2 = Player.query.get(m.player2_id)
            scores = result_to_scores(m.result) or (1,1)
            old1, old2 = p1.elo, p2.elo
            update_elo(p1, p2, *scores)
            elo_changes[p1.id] += p1.elo - old1
            elo_changes[p2.id] += p2.elo - old2
            # reset
            p1.elo, p2.elo = old1, old2

    # Build standings
    standings = []
    for p in players:
        wins = draws = losses = points = 0
        for m in matches:
            if m.player1_id == p.id or m.player2_id == p.id:
                if m.result == "bye" and m.player1_id == p.id:
                    wins += 1; points += 3
                elif m.result in ["2-0","2-1"] and m.player1_id == p.id:
                    wins += 1; points += 3
                elif m.result in ["0-2","1-2"] and m.player2_id == p.id:
                    wins += 1; points += 3
                elif m.result == "1-1":
                    draws += 1; points += 1
                else:
                    losses += 1
        standings.append({
            "player": p,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "points": points,
            "elo_delta": elo_changes[p.id]
        })

    standings.sort(key=lambda s: (s["points"], s["wins"]), reverse=True)

    if request.method == 'POST':
        # Apply real Elo updates
        for m in matches:
            if m.player2_id:
                p1 = Player.query.get(m.player1_id)
                p2 = Player.query.get(m.player2_id)
                scores = result_to_scores(m.result) or (1,1)
                update_elo(p1, p2, *scores)
        db.session.commit()
        flash("Tournament confirmed and Elo updated!", "success")
        return redirect(url_for('players'))

    return render_template('confirm_import.html',
                           tournament=tournament,
                           standings=standings)



@app.route('/tournament/<int:tid>/import_text', methods=['GET', 'POST'])
def import_text(tid):
    """
    Import matches from pasted EventLink text.
    """
    tournament = Tournament.query.get_or_404(tid)

    if request.method == 'POST':
        pasted_text = request.form.get('eventlink_text')
        if not pasted_text.strip():
            flash("Please paste EventLink text.", "error")
            return redirect(url_for('import_text', tid=tid))

        parsed_matches = parse_eventlink_text(pasted_text)

        for m in parsed_matches:
            player = ensure_player(m["player"])
            if m["opponent"] is None:  # bye
                db.session.add(Match(tournament_id=tid,
                                     round_num=m["round"],
                                     player1_id=player.id,
                                     player2_id=None,
                                     result="bye"))
                continue

            opponent = ensure_player(m["opponent"])
            result = m["result"]
            scores = result_to_scores(result) or (1, 1)
            games_a, games_b = scores

            db.session.add(Match(tournament_id=tid,
                                 round_num=m["round"],
                                 player1_id=player.id,
                                 player2_id=opponent.id,
                                 result=result))
            update_elo(player, opponent, games_a, games_b)

        db.session.commit()
        flash("EventLink text imported successfully.", "success")
        return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

    return render_template('import_text.html', tournament=tournament)
# --- Ensure DB tables exist ---
with app.app_context():
    db.create_all()


# --- Run locally ---
if __name__ == '__main__':
    # For local development; Render will use gunicorn in production
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
