from flask import Flask, request, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
import os
import io
from datetime import datetime

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
    result = db.Column(db.String(10))  # e.g. "2-0", "1-2", "1-1", "bye"

# --- Elo update ---
def update_elo(player_a, player_b, score_a, score_b, k=32):
    # score_a/score_b are match game wins used to determine W/D/L for Elo:
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
    # Return game scores (a, b) for Elo decision
    mapping = {
        "2-0": (2, 0),
        "2-1": (2, 1),
        "1-2": (1, 2),
        "0-2": (0, 2),
        "1-1": (1, 1),
        "1-0": (1, 0),
        "0-1": (0, 1),
        "bye": (2, 0),  # treat as a 2-0 win for Elo decision, but no opponent
    }
    return mapping.get(result)

def points_from_result(result: str):
    # Your match points:
    # Win (2-0 or 2-1) -> 3, Loss (0-2 or 0-1) -> 0, Tie (1-1) -> 1, Bye -> 3
    if result == "bye":
        return (3, 0)
    mapping = {
        "2-0": (3, 0),
        "2-1": (3, 0),
        "0-2": (0, 3),
        "1-2": (0, 3),
        "1-1": (1, 1),
        "1-0": (3, 0),
        "0-1": (0, 3),
    }
    return mapping.get(result)

def parse_eventlink_pdf(file_stream: io.BytesIO):
    """
    Parse EventLink 'Pairings by Table' PDF and yield rows: (player, opponent, points_raw)
    - Normal rows: points_raw like '3-0', '0-3', '1-1', etc.
    - Bye rows: opponent is '*** Bye ***', points_raw is '6'
    """
    if not PDF_AVAILABLE:
        raise RuntimeError("PDF parsing library not available. Please install pdfplumber.")

    rows = []
    with pdfplumber.open(file_stream) as pdf:
        for page in pdf.pages:
            # Try to find a table. EventLink PDFs often have a single table per page.
            table = page.extract_table()
            if not table or len(table) < 2:
                # Fallback: try extract tables API
                for tbl in page.extract_tables():
                    if tbl and len(tbl) > 1:
                        table = tbl
                        break
            if not table or len(table) < 2:
                continue
            # Expect header: Table | Player | Opponent | Points
            header = [h.strip().lower() if h else "" for h in table[0]]
            # Find indices robustly
            try:
                player_idx = header.index("player")
                opponent_idx = header.index("opponent")
                points_idx = header.index("points")
            except ValueError:
                # If headers are not exactly found, try best effort based on known layout
                player_idx, opponent_idx, points_idx = 1, 2, 3

            for row in table[1:]:
                if not row or len(row) < max(player_idx, opponent_idx, points_idx) + 1:
                    continue
                player = (row[player_idx] or "").strip()
                opponent = (row[opponent_idx] or "").strip()
                points_raw = (row[points_idx] or "").strip()
                if not player:
                    continue
                rows.append((player, opponent, points_raw))
    return rows

def normalize_pdf_row(player: str, opponent: str, points_raw: str):
    """
    Convert PDF row into our app's result token.
    - If opponent is Bye and points_raw is '6', treat as 'bye'
    - If points_raw has '-', interpret as game score in EventLink's perspective:
        EventLink points like '3-0' represent MATCH POINTS, not game scores.
        However, your system expects game scores like '2-0', '2-1', etc.
        We'll map:
          '3-0' => player got 3 match points, opponent 0 -> choose '2-0'
          '0-3' => '0-2'
          '6-3','9-6', etc appearing in later rounds are cumulative match points shown in some views.
          For "Pairings by Table", earlier rounds used '3-0'. If cumulative points are shown (e.g., '6-9'),
          we'll infer winner by comparing left/right numbers: left>right => win -> '2-0'; left<right => loss -> '0-2'; equal => tie -> '1-1'.
    """
    # Bye
    if opponent == "*** Bye ***":
        # EventLink shows '6' which is their match-points representation for a Bye row.
        # In our system, we treat Bye as 3 match points, and result token 'bye'.
        return "bye"

    # If points_raw is like '3-0', '0-3', '1-1'
    if "-" in points_raw:
        left, right = points_raw.split("-", 1)
        left = left.strip()
        right = right.strip()
        # Simple known mappings for round 1 PDFs using match point format
        known = {
            ("3", "0"): "2-0",
            ("0", "3"): "0-2",
            ("1", "1"): "1-1",
            ("4", "0"): "2-0",  # some anomalies like 4-0 in your sample; treat as an A win
        }
        if (left, right) in known:
            return known[(left, right)]

        # For later rounds where the PDF shows cumulative points like '6-9', infer outcome:
        try:
            lnum = int(left)
            rnum = int(right)
            if lnum > rnum:
                return "2-0"  # winner A
            elif lnum < rnum:
                return "0-2"  # winner B
            else:
                return "1-1"
        except ValueError:
            pass

    # Fallback: if points_raw is a single number like '6' but not a Bye (rare), assume winner A
    if points_raw.isdigit():
        return "2-0"

    # Default conservative fallback
    return "1-1"

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
    if request.method == 'POST':
        date_str = request.form.get('date')
        player_names = request.form.getlist('players')  # collect all dynamic fields
        player_names = [name.strip() for name in player_names if name.strip()]

        player_objs = [ensure_player(name) for name in player_names]

        # Calculate rounds (simple heuristic)
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
            tp = TournamentPlayer(tournament_id=tournament.id, player_id=p.id)
            db.session.add(tp)
        db.session.commit()

        return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

    all_players = Player.query.order_by(Player.name).all()
    return render_template('new_tournament.html', players=all_players)

@app.route('/tournament/<int:tid>/round/<int:round_num>', methods=['GET', 'POST'])
def tournament_round(tid, round_num):
    tournament = Tournament.query.get_or_404(tid)
    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id)\
                          .filter(TournamentPlayer.tournament_id == tid).all()

    if request.method == 'POST':
        for i in range(1, (len(players) + 1)//2 + 1):  # handle odd count
            p1_val = request.form.get(f'player1_{i}')
            p2_val = request.form.get(f'player2_{i}')
            result = request.form.get(f'result_{i}')

            if not p1_val or not p2_val or p1_val == "" or p2_val == "":
                flash("Error: You must select a player for every match.", "error")
                return redirect(url_for('tournament_round', tid=tid, round_num=round_num))

            if p1_val == p2_val and p1_val not in ("bye", ""):
                flash("Error: A player cannot face themselves.", "error")
                return redirect(url_for('tournament_round', tid=tid, round_num=round_num))

            if p1_val == "bye" or p2_val == "bye":
                match = Match(tournament_id=tid, round_num=round_num,
                              player1_id=None if p1_val == "bye" else int(p1_val),
                              player2_id=None if p2_val == "bye" else int(p2_val),
                              result="bye")
                db.session.add(match)
                db.session.commit()
                continue

            score_map = {
                "2-0": (2, 0),
                "2-1": (2, 1),
                "1-2": (1, 2),
                "0-2": (0, 2),
                "1-1": (1, 1),
                "1-0": (1, 0),
                "0-1": (0, 1),
            }
            if result not in score_map:
                flash("Error: Invalid result selected.", "error")
                return redirect(url_for('tournament_round', tid=tid, round_num=round_num))
            games_a, games_b = score_map[result]

            p1_id, p2_id = int(p1_val), int(p2_val)
            match = Match(tournament_id=tid, round_num=round_num,
                          player1_id=p1_id, player2_id=p2_id, result=result)
            db.session.add(match)

            player1 = Player.query.get(p1_id)
            player2 = Player.query.get(p2_id)
            update_elo(player1, player2, games_a, games_b)

        db.session.commit()

        if round_num < tournament.rounds:
            return redirect(url_for('tournament_round', tid=tid, round_num=round_num+1))
        else:
            return redirect(url_for('players'))

    return render_template('round.html', players=players, round_num=round_num, tid=tid)

# --- Import round from PDF workflow ---
@app.route('/tournament/<int:tid>/import_round', methods=['GET', 'POST'])
def import_round(tid):
    tournament = Tournament.query.get_or_404(tid)

    if request.method == 'POST':
        round_num_str = request.form.get('round_num')
        if not round_num_str or not round_num_str.isdigit():
            flash("Please enter a valid round number.", "error")
            return redirect(url_for('import_round', tid=tid))
        round_num = int(round_num_str)

        file = request.files.get('pdf')
        if not file or file.filename == "":
            flash("Please upload a round PDF.", "error")
            return redirect(url_for('import_round', tid=tid))

        if not PDF_AVAILABLE:
            flash("PDF parsing is not available on this server. Please install pdfplumber.", "error")
            return redirect(url_for('import_round', tid=tid))

        # Read file into memory
        data = file.read()
        stream = io.BytesIO(data)

        try:
            raw_rows = parse_eventlink_pdf(stream)
        except Exception as e:
            flash(f"Failed to parse PDF: {e}", "error")
            return redirect(url_for('import_round', tid=tid))

        # Normalize rows to our result tokens
        normalized = []
        for player_name, opponent_name, points_raw in raw_rows:
            if not player_name:
                continue
            # Only consider rows with actual match/bye entries
            result_token = normalize_pdf_row(player_name, opponent_name, points_raw)
            normalized.append({
                "player": player_name,
                "opponent": None if opponent_name == "*** Bye ***" else opponent_name,
                "result": result_token
            })

        if not normalized:
            flash("No matches found in the uploaded PDF.", "error")
            return redirect(url_for('import_round', tid=tid))

        # Store preview in session-like structure via hidden form re-render (no server session used here)
        return render_template('import_round.html',
                               tournament=tournament,
                               round_num=round_num,
                               matches=normalized,
                               preview=True)

    return render_template('import_round.html', tournament=tournament, preview=False)

@app.route('/tournament/<int:tid>/confirm_import_round', methods=['POST'])
def confirm_import_round(tid):
    tournament = Tournament.query.get_or_404(tid)
    round_num = int(request.form.get('round_num'))
    count = int(request.form.get('count'))

    # Reconstruct matches from posted preview data
    matches = []
    for i in range(count):
        p = request.form.get(f'player_{i}')
        o = request.form.get(f'opponent_{i}')
        r = request.form.get(f'result_{i}')
        matches.append({"player": p, "opponent": o if o != "" else None, "result": r})

    # Insert into DB
    for m in matches:
        player = ensure_player(m["player"])
        if m["opponent"] is None:  # bye
            match = Match(tournament_id=tid, round_num=round_num,
                          player1_id=player.id, player2_id=None, result="bye")
            db.session.add(match)
            continue

        opponent = ensure_player(m["opponent"])
        result = m["result"]
        scores = result_to_scores(result)
        if not scores:
            # Fallback to tie if unknown
            scores = (1, 1)
        games_a, games_b = scores

        match = Match(tournament_id=tid, round_num=round_num,
                      player1_id=player.id, player2_id=opponent.id, result=result)
        db.session.add(match)

        # Elo update based on game scores
        update_elo(player, opponent, games_a, games_b)

    db.session.commit()
    flash(f"Round {round_num} imported successfully.", "success")
    return redirect(url_for('tournament_round', tid=tid, round_num=round_num))

# --- Ensure DB tables exist ---
with app.app_context():
    db.create_all()

# --- Run locally ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
