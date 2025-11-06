from flask import Flask, request, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
import os, io
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# Optional: pdfplumber for PDF parsing
try:
    import pdfplumber
    PDF_AVAILABLE = True
except Exception:
    PDF_AVAILABLE = False

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
    result = db.Column(db.String(10))  # "2-0","2-1","1-2","0-2","1-1","1-0","0-1","bye"

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
        "2-1": (2, 1),
        "1-2": (1, 2),
        "0-2": (0, 2),
        "1-1": (1, 1),
        "1-0": (1, 0),
        "0-1": (0, 1),
        "bye": (2, 0),
    }
    return mapping.get(result)

def parse_eventlink_pdf(file_stream: io.BytesIO):
    """
    Parse EventLink 'Pairings by Table' PDF and yield rows: (player, opponent, points_raw)
    Uses positional columns: Player=1, Opponent=2, Points=3. Logs table presence.
    """
    if not PDF_AVAILABLE:
        raise RuntimeError("PDF parsing library not available. Please install pdfplumber.")

    rows = []
    with pdfplumber.open(file_stream) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            table = page.extract_table()
            if not table or len(table) < 2:
                for tbl in page.extract_tables():
                    if tbl and len(tbl) > 1:
                        table = tbl
                        break
            app.logger.debug(f"PDF page {page_idx}: table found={bool(table)} rows={len(table) if table else 0}")
            if not table or len(table) < 2:
                continue

            # Skip header; read columns [1,2,3]
            for row_idx, row in enumerate(table[1:], start=1):
                if not row or len(row) < 4:
                    app.logger.debug(f"Row {row_idx} skipped (len<{4}): {row}")
                    continue
                player = (row[1] or "").strip()
                opponent = (row[2] or "").strip()
                points_raw = (row[3] or "").strip()
                app.logger.debug(f"Row {row_idx} -> player={player}, opponent={opponent}, points={points_raw}")
                if player:
                    rows.append((player, opponent, points_raw))
    return rows

def extract_event_date(file_stream: io.BytesIO):
    """
    Extract 'Event Date: mm/dd/yyyy' or 'dd/mm/yyyy' from first page text; fallback to today.
    """
    if not PDF_AVAILABLE:
        return datetime.today().date()
    try:
        with pdfplumber.open(file_stream) as pdf:
            text = pdf.pages[0].extract_text() or ""
            app.logger.debug(f"Header text (first 200 chars): {text[:200]}")
            for line in text.splitlines():
                if "Event Date:" in line:
                    date_str = line.split("Event Date:", 1)[1].strip()
                    for fmt in ("%m/%d/%Y", "%d/%m/%Y"):
                        try:
                            parsed = datetime.strptime(date_str, fmt).date()
                            app.logger.debug(f"Parsed event date '{date_str}' with fmt '{fmt}' -> {parsed}")
                            return parsed
                        except ValueError:
                            continue
    except Exception as e:
        app.logger.error(f"Date extraction error: {e}")
    return datetime.today().date()

def normalize_pdf_row(player: str, opponent: str, points_raw: str):
    """
    Convert EventLink 'Points' into our result token.
    Bye -> 'bye'. Hyphen values compare left/right; single number -> '2-0' fallback.
    """
    if opponent == "*** Bye ***":
        return "bye"

    if "-" in points_raw:
        left, right = points_raw.split("-", 1)
        left, right = left.strip(), right.strip()
        known = {("3", "0"): "2-0", ("0", "3"): "0-2", ("1", "1"): "1-1"}
        if (left, right) in known:
            return known[(left, right)]
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
        return "2-0"

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

# Create new tournament: manual OR from Round 1 PDF in the same form
@app.route('/tournament/new', methods=['GET', 'POST'])
def new_tournament():
    if request.method == 'POST':
        # Prefer PDF workflow if a PDF was uploaded
        file = request.files.get('pdf')
        if file and file.filename.lower().endswith(".pdf"):
            if not PDF_AVAILABLE:
                flash("PDF parsing is not available on this server. Please install pdfplumber.", "error")
                return redirect(url_for('new_tournament'))

            data = file.read()
            try:
                raw_rows = parse_eventlink_pdf(io.BytesIO(data))
                app.logger.debug(f"Parsed rows from PDF: {raw_rows}")
            except Exception as e:
                app.logger.error(f"PDF parse error: {e}")
                flash(f"Failed to parse PDF: {e}", "error")
                return redirect(url_for('new_tournament'))

            if not raw_rows:
                flash("No matches found in the uploaded PDF.", "error")
                return redirect(url_for('new_tournament'))

            event_date = extract_event_date(io.BytesIO(data))
            app.logger.debug(f"Extracted event date: {event_date}")

            # Collect players from Round 1
            player_names = set()
            for p, o, _ in raw_rows:
                player_names.add(p)
                if o and o != "*** Bye ***":
                    player_names.add(o)

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

            # Create tournament
            tournament = Tournament(date=event_date, rounds=rounds)
            db.session.add(tournament)
            db.session.commit()

            # Attach players
            for p in player_objs:
                db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=p.id))
            db.session.commit()

            # Insert Round 1 matches
            for p, o, pts in raw_rows:
                result_token = normalize_pdf_row(p, o, pts)
                app.logger.debug(f"Row parsed -> Player: {p}, Opponent: {o}, Points: {pts}, Normalized: {result_token}")
                p1 = ensure_player(p)
                if o == "*** Bye ***":
                    db.session.add(Match(tournament_id=tournament.id, round_num=1,
                                         player1_id=p1.id, player2_id=None, result="bye"))
                else:
                    p2 = ensure_player(o)
                    db.session.add(Match(tournament_id=tournament.id, round_num=1,
                                         player1_id=p1.id, player2_id=p2.id, result=result_token))
            db.session.commit()

            flash("Tournament created from PDF and Round 1 imported!", "success")
            # Redirect to Round 2 (continue manually or import via PDF)
            return redirect(url_for('tournament_round', tid=tournament.id, round_num=2))

        # Otherwise, manual workflow
        date_str = request.form.get('date')
        player_names = request.form.getlist('players')  # dynamic fields
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

        # Create tournament
        tournament = Tournament(date=datetime.strptime(date_str, "%Y-%m-%d"), rounds=rounds)
        db.session.add(tournament)
        db.session.commit()

        # Attach players
        for p in player_objs:
            db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=p.id))
        db.session.commit()

        return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

    # GET: render form
    all_players = Player.query.order_by(Player.name).all()
    return render_template('new_tournament.html', players=all_players)

# Round entry and view
@app.route('/tournament/<int:tid>/round/<int:round_num>', methods=['GET', 'POST'])
def tournament_round(tid, round_num):
    tournament = Tournament.query.get_or_404(tid)
    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id)\
                          .filter(TournamentPlayer.tournament_id == tid).all()
    existing_matches = Match.query.filter_by(tournament_id=tid, round_num=round_num).all()

    if request.method == 'POST':
        # Manual entry for new matches in this round
        for i in range(1, (len(players) + 1)//2 + 1):
            p1_val = request.form.get(f'player1_{i}')
            p2_val = request.form.get(f'player2_{i}')
            result = request.form.get(f'result_{i}')

            if not p1_val or not p2_val:
                # Skip incomplete rows silently
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

            # Elo update
            player1 = Player.query.get(p1_id)
            player2 = Player.query.get(p2_id)
            games_a, games_b = score_map
            update_elo(player1, player2, games_a, games_b)

        db.session.commit()

        # Move to next round or finish
        if round_num < tournament.rounds:
            return redirect(url_for('tournament_round', tid=tid, round_num=round_num + 1))
        else:
            return redirect(url_for('players'))

    return render_template('round.html',
                           players=players,
                           round_num=round_num,
                           tid=tid,
                           matches=existing_matches)

# Import a specific round from EventLink PDF: Upload -> Preview -> Confirm
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

        data = file.read()
        try:
            raw_rows = parse_eventlink_pdf(io.BytesIO(data))
            app.logger.debug(f"Parsed rows for round import: {raw_rows}")
        except Exception as e:
            app.logger.error(f"PDF parse error: {e}")
            flash(f"Failed to parse PDF: {e}", "error")
            return redirect(url_for('import_round', tid=tid))

        normalized = []
        for player_name, opponent_name, points_raw in raw_rows:
            if not player_name:
                continue
            result_token = normalize_pdf_row(player_name, opponent_name, points_raw)
            app.logger.debug(f"Import round row -> {player_name} vs {opponent_name} [{points_raw}] => {result_token}")
            normalized.append({
                "player": player_name,
                "opponent": None if opponent_name == "*** Bye ***" else opponent_name,
                "result": result_token
            })

        if not normalized:
            flash("No matches found in the uploaded PDF.", "error")
            return redirect(url_for('import_round', tid=tid))

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

    # Insert into DB (create players if needed)
    for m in matches:
        player = ensure_player(m["player"])
        if m["opponent"] is None:  # bye
            db.session.add(Match(tournament_id=tid, round_num=round_num,
                                 player1_id=player.id, player2_id=None, result="bye"))
            continue

        opponent = ensure_player(m["opponent"])
        result = m["result"]
        scores = result_to_scores(result) or (1, 1)
        games_a, games_b = scores

        db.session.add(Match(tournament_id=tid, round_num=round_num,
                             player1_id=player.id, player2_id=opponent.id, result=result))
        # Elo update for imported rounds
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
