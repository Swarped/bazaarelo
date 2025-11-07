import os
import re
from datetime import datetime

from flask import Flask, request, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.secret_key = "supersecret"

# --- Persistent SQLite path ---
BASEDIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASEDIR, "data", "tournament.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{DB_PATH}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


# --- Models ---
class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    elo = db.Column(db.Integer, default=1400)

class Tournament(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    rounds = db.Column(db.Integer, nullable=False)

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


# --- EventLink text parsing ---
def normalize_points(points_raw: str):
    """
    Convert EventLink 'Points' into result tokens.
    - 3 or 6 → win ("2-0")
    - 1 → tie ("1-1")
    - 0 → loss ("0-2")
    - Hyphen values like "3-0", "0-3", "1-1" → map directly
    """
    points_raw = points_raw.strip().lower()

    if "***bye***" in points_raw or points_raw == "bye":
        return "bye"

    if "-" in points_raw:
        left, right = points_raw.split("-", 1)
        left, right = left.strip(), right.strip()
        if (left, right) == ("3", "0"):
            return "2-0"
        if (left, right) == ("0", "3"):
            return "0-2"
        if (left, right) == ("1", "1"):
            return "1-1"
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


@app.route('/tournament/new', methods=['GET', 'POST'])
def new_tournament():
    """
    Create a new tournament manually (enter date + players).
    """
    if request.method == 'POST':
        date_str = request.form.get('date')
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

        tournament = Tournament(date=datetime.strptime(date_str, "%Y-%m-%d"), rounds=rounds)
        db.session.add(tournament)
        db.session.commit()

        for p in player_objs:
            db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=p.id))
        db.session.commit()

        return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

    all_players = Player.query.order_by(Player.name).all()
    return render_template('new_tournament.html', players=all_players)


@app.route('/tournament/<int:tid>/round/<int:round_num>', methods=['GET', 'POST'])
def tournament_round(tid, round_num):
    tournament = Tournament.query.get_or_404(tid)
    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id)\
                          .filter(TournamentPlayer.tournament_id == tid).all()
    existing_matches = Match.query.filter_by(tournament_id=tid, round_num=round_num).all()

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
                match = Match(tournament_id=tid, round_num=round_num,
                              player1_id=None if p1_val == "bye" else int(p1_val),
                              player2_id=None if p2_val == "bye" else int(p2_val),
                              result="bye")
                db.session.add(match)
                continue

            score_map = result_to_scores(result)
            if not score_map:
                flash("Error: Invalid result selected.", "error")
                return redirect(url_for('tournament_round', tid=tid, round_num=round_num))

            p1_id, p2_id = int(p1_val), int(p2_val)
            db.session.add(Match(tournament_id=tid, round_num=round_num,
                                 player1_id=p1_id, player2_id=p2_id, result=result))

            player1 = Player.query.get(p1_id)
            player2 = Player.query.get(p2_id)
            games_a, games_b = score_map
            update_elo(player1, player2, games_a, games_b)

        db.session.commit()

        if round_num < tournament.rounds:
            return redirect(url_for('tournament_round', tid=tournament.id, round_num=round_num + 1))
        else:
            return redirect(url_for('players'))

    return render_template('round.html',
                           players=players,
                           round_num=round_num,
                           tid=tid,
                           matches=existing_matches)


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
