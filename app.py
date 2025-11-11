import os
import re
import logging
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_dance.contrib.google import make_google_blueprint, google
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import requests
import html
from werkzeug.utils import secure_filename
from PIL import Image


import sys
print("Hello from Flask startup", file=sys.stderr)

# Allow HTTP for local OAuth (do not use in production)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
app.secret_key = "supersecret"

# --- Persistent SQLite paths ---
BASEDIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASEDIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "tournament.db")
USERS_PATH = os.path.join(DATA_DIR, "users.db")

# --- SQLAlchemy configuration (separate bind for users) ---
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{DB_PATH}"
app.config['SQLALCHEMY_BINDS'] = {
    'users': f"sqlite:///{USERS_PATH}"
}
app.config['SQLALCHEMY_TRACK_NOTIFICATIONS'] = False  # harmless typo-safe line if using older versions
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

DEFAULT_ELO = 1400

# --- Login ---
login_manager = LoginManager(app)
login_manager.login_view = "google.login"

class User(db.Model, UserMixin):
    __bind_key__ = 'users'
    id = db.Column(db.String(255), primary_key=True)  # Google user_id
    name = db.Column(db.String(120))
    email = db.Column(db.String(120), unique=True)
    is_admin = db.Column(db.Boolean, default=False)
    is_scorekeeper = db.Column(db.Boolean, default=False)  # NEW

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(user_id)

google_bp = make_google_blueprint(
    client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    scope=[
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ],
    redirect_to="google_login",
)
app.register_blueprint(google_bp, url_prefix="/login")

# --- Models ---
class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    elo = db.Column(db.Integer, default=DEFAULT_ELO)

class Tournament(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    date = db.Column(db.Date, nullable=False)
    rounds = db.Column(db.Integer, nullable=False)
    imported_from_text = db.Column(db.Boolean, default=False)
    top_cut = db.Column(db.Integer, nullable=True)
    casual = db.Column(db.Boolean, default=False) 
    player_count = db.Column(db.Integer, nullable=True)  # NEW

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

class Deck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=True)
    name = db.Column(db.String(120))
    list_text = db.Column(db.Text)
    colors = db.Column(db.String(10))  # NEW: store deck colors like "WUBRG"
    image_url = db.Column(db.String(255))  # NEW: store archetype image path

    player = db.relationship('Player', backref='decks')
    tournament = db.relationship('Tournament', backref='decks')



# --- Helpers ---
def ensure_player(name: str) -> Player:
    name = name.strip()
    if not name:
        return None
    player = Player.query.filter_by(name=name).first()
    if not player:
        player = Player(name=name, elo=DEFAULT_ELO)
        db.session.add(player)
        db.session.commit()
    return player

def result_to_scores(result: str):
    mapping = {
        "2-0": (2, 0),
        "0-2": (0, 2),
        "1-1": (1, 1),
        "bye": (2, 0),
        # accept 2-1 / 1-2 for manual entry; treat as a match win/loss
        "2-1": (2, 1),
        "1-2": (1, 2),
        "1-0": (1, 0),
        "0-1": (0, 1),
    }
    return mapping.get(result)

def default_top_cut(num_players: int) -> int:
    # WPN-inspired ranges with clamping
    if 9 <= num_players <= 16:
        cut = 4
    elif 17 <= num_players <= 32:
        cut = 8
    elif 33 <= num_players <= 64:
        cut = 8
    elif 65 <= num_players <= 128:
        cut = 8
    elif 129 <= num_players <= 216:
        cut = 8
    elif 217 <= num_players <= 256:
        cut = 16
    elif 257 <= num_players <= 512:
        cut = 16
    elif 513 <= num_players <= 1024:
        cut = 32
    elif 1025 <= num_players <= 2048:
        cut = 32
    else:
        cut = 0
    return min(cut, num_players)

def update_elo(player_a, player_b, score_a, score_b, k=32):
    # Convert game scores to outcome for Elo
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



# --- Auth routes ---
@app.route("/google_login")
def google_login():
    if not google.authorized:
        return redirect(url_for("google.login"))

    resp = google.get("/oauth2/v2/userinfo")
    if not resp.ok:
        flash("Failed to fetch user info from Google", "error")
        return redirect(url_for("players"))

    user_info = resp.json()
    user_id = user_info.get("id")
    email = user_info.get("email")
    name = user_info.get("name")

    if not user_id or not email:
        flash("Missing Google user id or email.", "error")
        return redirect(url_for("players"))

    # Try to find by id first
    user = User.query.get(user_id)

    if not user:
        # If no user with this id, check if someone already exists with this email
        existing_by_email = User.query.filter_by(email=email).first()
        if existing_by_email:
            # Update that record to use the new Google id
            existing_by_email.id = user_id
            existing_by_email.name = name
            db.session.commit()
            user = existing_by_email
        else:
            # Create brand new user
            user = User(id=user_id, name=name, email=email)
            db.session.add(user)
            db.session.commit()

    login_user(user)
    flash(f"Logged in as {user.name}", "success")
    return redirect(url_for("players"))


@app.route("/logout")
def logout():
    logout_user()
    flash("You have logged out.", "success")
    return redirect(url_for("players"))

# --- Admin panel ---
@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin_panel():
    if not current_user.is_admin:
        flash("You do not have permission to access the admin panel.", "error")
        return redirect(url_for("players"))

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add_by_email":
            # existing admin creation logic
            email = request.form.get("email", "").strip()
            if not email:
                flash("Email required.", "error")
                return redirect(url_for("admin_panel"))
            user = User.query.filter_by(email=email).first()
            if user:
                user.is_admin = True
                flash(f"{email} promoted to admin.", "success")
            else:
                user = User(id=email, name=email.split("@")[0], email=email, is_admin=True)
                db.session.add(user)
                flash(f"New admin created for {email}.", "success")
            db.session.commit()

        elif action == "add_scorekeeper_by_email":
            # NEW: add scorekeeper by email
            email = request.form.get("email", "").strip()
            if not email:
                flash("Email required.", "error")
                return redirect(url_for("admin_panel"))
            user = User.query.filter_by(email=email).first()
            if user:
                user.is_scorekeeper = True
                flash(f"{email} promoted to scorekeeper.", "success")
            else:
                user = User(id=email, name=email.split("@")[0], email=email, is_scorekeeper=True)
                db.session.add(user)
                flash(f"New scorekeeper created for {email}.", "success")
            db.session.commit()

        else:
            # toggle roles
            user_id = request.form.get("user_id")
            make_admin = request.form.get("make_admin")
            make_scorekeeper = request.form.get("make_scorekeeper")
            user = User.query.get(user_id)
            if user:
                if make_admin is not None:
                    user.is_admin = (make_admin == "true")
                if make_scorekeeper is not None:
                    user.is_scorekeeper = (make_scorekeeper == "true")
                db.session.commit()
                flash(f"Updated roles for {user.email}", "success")

        return redirect(url_for("admin_panel"))

    users = User.query.all()
    return render_template("admin.html", users=users)

@app.route("/make_me_admin")
@login_required
def make_me_admin():
    # Promote the current user to admin automatically
    current_user.is_admin = True
    db.session.commit()
    flash("You are now an admin!", "success")
    return redirect(url_for("admin_panel"))



@app.route('/archetype/<name>/edit', methods=['GET','POST'])
@login_required
def edit_archetype(name):
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for('decks_list'))

    if request.method == 'POST':
        file = request.files.get('image')
        if not file or file.filename == '':
            flash("No file selected", "error")
            return redirect(url_for('decks_list'))

        # enforce .jpg
        if not file.filename.lower().endswith('.jpg'):
            flash("Only .jpg files are allowed", "error")
            return redirect(url_for('decks_list'))

        filename = secure_filename(file.filename)
        upload_dir = os.path.join(app.static_folder, 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        path = os.path.join(upload_dir, filename)

        # resize to 512x512
        img = Image.open(file)
        img = img.convert("RGB")
        img = img.resize((512, 512))
        img.save(path, "JPEG")

        # assign to last deck of archetype
        deck = Deck.query.filter_by(name=name).order_by(Deck.id.desc()).first()
        if deck:
            deck.image_url = f'uploads/{filename}'
            db.session.commit()
            flash("Image updated", "success")
        else:
            flash("No deck found for archetype", "error")

        return redirect(url_for('decks_list'))

    return render_template("edit_archetype.html", name=name)



# --- Player search for autocomplete ---
@app.route('/players/search')
def player_search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    results = Player.query.filter(Player.name.ilike(f"%{q}%")).limit(10).all()
    return jsonify([p.name for p in results])

# --- Parsing helpers ---
def normalize_points(points_raw: str) -> str:
    """
    Normalize EventLink and Arena result tokens to: "2-0","0-2","1-1","bye".
    Handle English and Spanish ("Ronda libre").
    """
    points_raw = points_raw.strip().lower()
    if "***bye***" in points_raw or points_raw == "bye":
        return "bye"
    if "***ronda libre***" in points_raw or points_raw == "ronda libre":
        return "bye"
    if "-" in points_raw:
        left, right = points_raw.split("-", 1)
        left, right = left.strip(), right.strip()
        if (left, right) == ("2", "1"):
            return "2-0"
        if (left, right) == ("1", "2"):
            return "0-2"
        if (left, right) in [("1", "1"), ("1", "1-1")]:
            return "1-1"
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
        if val >= 3:
            return "2-0"
        elif val == 1:
            return "1-1"
        elif val == 0:
            return "0-2"
    return "1-1"

def clean_name(name: str) -> str:
    # remove Arena "(12 pts)" suffixes
    return re.sub(r"\(\s*\d+\s*pts?\s*\)", "", name).strip()

def parse_arena_text(all_text: str):
    matches = []
    current_round = None
    for raw_line in all_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("round "):
            try:
                current_round = int(line.split()[1])
            except Exception:
                current_round = None
            continue
        if "--- Bye ---" in line:
            parts = line.split("vs")
            player = clean_name(parts[0].strip())
            matches.append({"round": current_round, "player": player, "opponent": None, "result": "bye"})
            continue
        if "vs" in line:
            parts = line.split("vs")
            if len(parts) == 2:
                player, opponent = clean_name(parts[0].strip()), clean_name(parts[1].strip())
                matches.append({"round": current_round, "player": player, "opponent": opponent, "result": None})
            continue
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
    for m in matches:
        if m["result"] is None:
            m["result"] = "1-1"
    return matches

def parse_eventlink_text(all_text: str):
    matches = []
    current_round = None
    event_name = None
    for raw_line in all_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("event:") or line.lower().startswith("evento:"):
            event_name = line.split(":", 1)[1].strip()
            continue
        if line.lower().startswith("round ") or line.lower().startswith("ronda "):
            try:
                current_round = int(line.split()[1])
            except Exception:
                current_round = None
            continue
        skip_prefixes = [
            "table", "mesa", "eventlink", "report:", "reportar:",
            "event date:", "fecha del evento:", "event information:", "información del evento:"
        ]
        if any(line.lower().startswith(pref) for pref in skip_prefixes):
            continue
        if set(line) == set("-") or "copyright" in line.lower():
            continue
        if "***bye***" in line.lower() or "***ronda libre***" in line.lower():
            parts = re.split(r"\s{2,}", line)
            if len(parts) >= 2:
                player = parts[0].strip()
                matches.append({"round": current_round, "player": player, "opponent": None, "result": "bye"})
            continue
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
    return matches, event_name
# --- Basic routes ---
@app.route('/')
def home():
    return redirect(url_for('players'))

from sqlalchemy import func

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

    # Top 4 archetypes by number of decks
    top_archetypes = (
        db.session.query(Deck.name, func.count(Deck.id).label("count"))
        .filter(Deck.name.isnot(None))
        .group_by(Deck.name)
        .order_by(func.count(Deck.id).desc())
        .limit(4)
        .all()
    )

    top_decks = []
    for name, _ in top_archetypes:
        last_deck = Deck.query.filter_by(name=name).order_by(Deck.id.desc()).first()
        top_decks.append({
            "name": name,
            "image_url": last_deck.image_url if last_deck and last_deck.image_url else ""
        })

    return render_template('players.html', players=all_players, top_decks=top_decks)


@app.route('/decks')
def decks_list():
    decks = Deck.query.filter(Deck.name.isnot(None)).all()
    archetypes = {}
    for d in decks:
        archetypes.setdefault(d.name, []).append(d)

    # compute colors for each archetype using the last submitted deck
    archetype_colors = {}
    for name, deck_list in archetypes.items():
        # sort by id (or by tournament_id/date if you prefer)
        last_deck = sorted(deck_list, key=lambda d: d.id)[-1]
        archetype_colors[name] = last_deck.colors or ""

    return render_template(
        "decks.html",
        archetypes=archetypes,
        archetype_colors=archetype_colors
    )

@app.route('/decks/<deck_name>')
def deck_detail(deck_name):
    decks = Deck.query.filter_by(name=deck_name).all()
    rows = []
    for d in decks:
        player = Player.query.get(d.player_id)
        tournament = Tournament.query.get(d.tournament_id)
        tp = TournamentPlayer.query.filter_by(tournament_id=tournament.id, player_id=player.id).first()
        rank = getattr(tp, "rank", None)
        rows.append({
            "player": player,
            "tournament": tournament,
            "rank": rank,
            "deck": d
        })

    # get most recent deck image
    last_deck = Deck.query.filter_by(name=deck_name).order_by(Deck.id.desc()).first()
    image_url = last_deck.image_url if last_deck and last_deck.image_url else None

    return render_template("deck_detail.html",
                           deck_name=deck_name,
                           rows=rows,
                           image_url=image_url)



@app.route('/reset_db', methods=['POST'])
@login_required
def reset_db():
    if not current_user.is_admin:
        flash("Only admins can reset the database.", "error")
        return redirect(url_for('players'))
    # Drop and recreate only the tournament (default) DB
    with app.app_context():
        db.Model.metadata.drop_all(bind=db.engine)
        db.Model.metadata.create_all(bind=db.engine)
    flash("Tournament database has been reset.", "success")
    return redirect(url_for('players'))

@app.route('/tournament/choose')
@login_required
def choose_tournament_type():
    return render_template('choose_tournament_type.html')



# --- New Tournament ---
@app.route('/tournament/new', methods=['GET', 'POST'])
@login_required
def new_tournament():
    if not (current_user.is_admin or current_user.is_scorekeeper):
        flash("Only admins or scorekeepers can create tournaments.", "error")
        return redirect(url_for('players'))

    if request.method == 'POST':
        workflow = request.form.get('workflow')

        if workflow == 'manual':
            tournament_name = request.form.get('tournament_name')
            date_str = request.form.get('date')
            player_names = [n.strip() for n in request.form.getlist('players') if n.strip()]
            player_objs = [ensure_player(name) for name in player_names]

            num_players = len(player_objs)
            rounds = 3 if num_players <= 8 else 4 if num_players <= 16 else \
                     5 if num_players <= 32 else 6 if num_players <= 64 else 7

            tournament = Tournament(
                name=tournament_name,
                date=datetime.strptime(date_str, "%Y-%m-%d"),
                rounds=rounds,
                imported_from_text=False,
            )
            db.session.add(tournament)
            db.session.commit()

            for p in player_objs:
                db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=p.id))
            db.session.commit()

            return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

        elif workflow == 'import':
            import_format = request.form.get('import_format')
            raw_text = request.form.get('import_text', '')

            if import_format == 'eventlink':
                parsed_matches, event_name = parse_eventlink_text(raw_text)
                if not event_name:
                    event_name = "EventLink Import"
                if not parsed_matches:
                    flash("No matches found.", "error")
                    return redirect(url_for('new_tournament'))

                player_names = {m["player"] for m in parsed_matches}
                player_names |= {m["opponent"] for m in parsed_matches if m["opponent"]}
                player_objs = [ensure_player(name) for name in sorted(player_names)]

                num_players = len(player_objs)
                rounds = 3 if num_players <= 8 else 4 if num_players <= 16 else \
                         5 if num_players <= 32 else 6 if num_players <= 64 else 7

                tournament = Tournament(name=event_name, date=datetime.today().date(),
                                        rounds=rounds, imported_from_text=True)
                db.session.add(tournament)
                db.session.commit()

                for p in player_objs:
                    db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=p.id))
                db.session.commit()

                for m in parsed_matches:
                    p1 = ensure_player(m["player"])
                    if m["opponent"] is None:
                        db.session.add(Match(tournament_id=tournament.id, round_num=m["round"],
                                             player1_id=p1.id, player2_id=None, result="bye"))
                    else:
                        p2 = ensure_player(m["opponent"])
                        db.session.add(Match(tournament_id=tournament.id, round_num=m["round"],
                                             player1_id=p1.id, player2_id=p2.id, result=m["result"]))
                db.session.commit()
                flash("Tournament created from EventLink text!", "success")
                return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

            elif import_format == 'arena':
                parsed_matches = parse_arena_text(raw_text)
                event_name = request.form.get('arena_tournament_name')
                if not event_name:
                    flash("Arena tournament name required.", "error")
                    return redirect(url_for('new_tournament'))

                parsed_names = {m["player"] for m in parsed_matches if m["player"]}
                parsed_names |= {m["opponent"] for m in parsed_matches if m["opponent"]}
                existing_names = {p.name for p in Player.query.all()}
                unknown_names = parsed_names - existing_names

                if unknown_names:
                    return render_template("confirm_players.html",
                                           unknown_names=sorted(unknown_names),
                                           existing_players=Player.query.all(),
                                           raw_text=raw_text,
                                           event_name=event_name)

                if not parsed_matches:
                    flash("No matches found.", "error")
                    return redirect(url_for('new_tournament'))

                player_objs = [ensure_player(name) for name in sorted(parsed_names)]
                num_players = len(player_objs)
                rounds = 3 if num_players <= 8 else 4 if num_players <= 16 else \
                         5 if num_players <= 32 else 6 if num_players <= 64 else 7

                tournament = Tournament(name=event_name, date=datetime.today().date(),
                                        rounds=rounds, imported_from_text=True)
                db.session.add(tournament)
                db.session.commit()

                for p in player_objs:
                    db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=p.id))
                db.session.commit()

                for m in parsed_matches:
                    p1 = ensure_player(m["player"])
                    if m["opponent"] is None:
                        db.session.add(Match(tournament_id=tournament.id, round_num=m["round"],
                                             player1_id=p1.id, player2_id=None, result="bye"))
                    else:
                        p2 = ensure_player(m["opponent"])
                        db.session.add(Match(tournament_id=tournament.id, round_num=m["round"],
                                             player1_id=p1.id, player2_id=p2.id, result=m["result"]))
                db.session.commit()
                flash("Tournament created from Arena text!", "success")
                return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

    return render_template('new_tournament.html')




def merge_player_ids(source_id, target_id):
    if source_id == target_id:
        return
    # Update all references from source to target
    TournamentPlayer.query.filter_by(player_id=source_id).update({"player_id": target_id})
    Match.query.filter_by(player1_id=source_id).update({"player1_id": target_id})
    Match.query.filter_by(player2_id=source_id).update({"player2_id": target_id})
    Deck.query.filter_by(player_id=source_id).update({"player_id": target_id})
    db.session.commit()
    # Delete the old player row only if it still exists
    src = Player.query.get(source_id)
    if src:
        db.session.delete(src)
        db.session.commit()


@app.route('/tournament/new_casual', methods=['GET', 'POST'])
@login_required
def new_tournament_casual():
    if request.method == 'POST':
        tournament_name = request.form.get('tournament_name')
        date_str = request.form.get('date')
        top_cut = int(request.form.get('top_cut') or 0)
        player_count = int(request.form.get('player_count') or 0)
        rounds = int(request.form.get('rounds') or 0)

        # Create the casual tournament
        tournament = Tournament(
            name=tournament_name,
            date=datetime.strptime(date_str, "%Y-%m-%d"),
            rounds=rounds,                 # no swiss rounds
            imported_from_text=False,
            casual=True,               # mark as casual
            top_cut=top_cut,
            player_count=player_count   # store it here
        )
        tournament.player_count = player_count  # if you add this column
        db.session.add(tournament)
        db.session.commit()

        # Loop through final standings rows
        for rank in range(1, top_cut + 1):
            player_name = request.form.get(f"player_{rank}", "").strip()
            deck_name = html.escape(request.form.get(f"deck_name_{rank}", "").strip())
            deck_list = html.escape(request.form.get(f"deck_list_{rank}", "").strip())

            if player_name:
                player = ensure_player(player_name)
                db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=player.id))

                if deck_name or deck_list:
                    deck = Deck.query.filter_by(player_id=player.id, tournament_id=tournament.id).first()
                    if deck:
                        deck.name = deck_name
                        deck.list_text = deck_list
                    else:
                        deck = Deck(player_id=player.id,
                                    tournament_id=tournament.id,
                                    name=deck_name,
                                    list_text=deck_list)
                        db.session.add(deck)


        db.session.commit()
        flash("Casual tournament final standings saved!", "success")

        # Redirect to a view page that shows players/decks so Edit/Delete buttons appear
        return redirect(url_for('players'))


    return render_template('new_tournament_casual.html')

@app.route('/player/<int:pid>')
@login_required
def player_info(pid):
    player = Player.query.get_or_404(pid)
    tournaments = (
        db.session.query(Tournament, TournamentPlayer)
        .join(TournamentPlayer, Tournament.id == TournamentPlayer.tournament_id)
        .filter(TournamentPlayer.player_id == pid)
        .all()
    )

    history = []
    top1 = top4 = top8 = 0
    for t, tp in tournaments:
        deck = Deck.query.filter_by(player_id=pid, tournament_id=t.id).first()

        rank = getattr(tp, "rank", None)  # use the actual rank field

        if rank and t.top_cut and rank <= t.top_cut:
            if rank == 1:
                top1 += 1
            elif rank <= 4:
                top4 += 1
            elif rank <= 8:
                top8 += 1

        history.append({"tournament": t, "deck": deck, "rank": rank})

    stats = {
        "tournaments_played": len(tournaments),
        "top1": top1,
        "top4": top4,
        "top8": top8,
    }

    return render_template("playerinfo.html", player=player, history=history, stats=stats)




@app.route('/confirm_players', methods=['POST'])
def confirm_players():
    raw_text = request.form.get("raw_text", "")
    parsed_matches = parse_arena_text(raw_text)

    # Resolve each name to a final player ID
    for m in parsed_matches:
        for role in ["player", "opponent"]:
            name = m.get(role)
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
                replace_id_str = request.form.get(f"replace_{name}")
                if replace_id_str:
                    replace_id = int(replace_id_str)
                    # If the name exists as a Player row, merge it into the chosen existing ID
                    current = Player.query.filter_by(name=name).first()
                    if current and current.id != replace_id:
                        merge_player_ids(current.id, replace_id)
                    # Write the final ID to the parsed structure
                    m[role] = replace_id
                else:
                    # Fallback: if no replace_id provided, use existing player by name if present
                    player = Player.query.filter_by(name=name).first()
                    if player:
                        m[role] = player.id

            else:
                # Default: use existing player if present
                player = Player.query.filter_by(name=name).first()
                if player:
                    m[role] = player.id

    # Collect final IDs from parsed matches
    final_ids = {pid for m in parsed_matches for pid in [m.get("player"), m.get("opponent")] if isinstance(pid, int)}

    # Create the tournament
    num_players = len(final_ids)
    rounds = 3 if num_players <= 8 else 4 if num_players <= 16 else 5 if num_players <= 32 else 6 if num_players <= 64 else 7
    event_name = request.form.get("event_name") or "Imported Tournament"

    tournament = Tournament(name=event_name, date=datetime.today().date(), rounds=rounds, imported_from_text=True)
    db.session.add(tournament)
    db.session.commit()

    # Ensure TournamentPlayer rows exist for all final_ids
    for pid in final_ids:
        if not TournamentPlayer.query.filter_by(tournament_id=tournament.id, player_id=pid).first():
            db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=pid))
    db.session.commit()

    # Insert matches using final IDs, normalize byes
    for m in parsed_matches:
        p1_id = m["player"] if isinstance(m["player"], int) else None
        p2_id = m["opponent"] if isinstance(m["opponent"], int) else None
        result = m.get("result") or "1-1"

        # If an ID is missing but the name exists in DB, resolve it
        if p1_id is None and isinstance(m.get("player"), str):
            p = Player.query.filter_by(name=m["player"]).first()
            p1_id = p.id if p else None
        if p2_id is None and isinstance(m.get("opponent"), str):
            p = Player.query.filter_by(name=m["opponent"]).first()
            p2_id = p.id if p else None

        is_bye = (result == "bye") or (p2_id is None)
        db.session.add(Match(
            tournament_id=tournament.id,
            round_num=m.get("round") or 1,
            player1_id=p1_id,
            player2_id=None if is_bye else p2_id,
            result="bye" if is_bye else result
        ))

    db.session.commit()

    # Repopulate TournamentPlayer from the matches to guarantee coverage
    match_ids = set()
    for mm in Match.query.filter_by(tournament_id=tournament.id).all():
        if mm.player1_id:
            match_ids.add(mm.player1_id)
        if mm.player2_id:
            match_ids.add(mm.player2_id)
    for pid in match_ids:
        if not TournamentPlayer.query.filter_by(tournament_id=tournament.id, player_id=pid).first():
            db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=pid))
    db.session.commit()

    flash("Players confirmed and tournament created.", "success")
    return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))







@app.route('/submit_deck', methods=['POST'])
def submit_deck():
    player_id = request.form.get("player_id")
    tournament_id = request.form.get("tournament_id")

    # sanitize here
    deck_name = html.escape(request.form.get("deck_name", "").strip())
    deck_list = html.escape(request.form.get("deck_list", "").strip())

    if not player_id or not tournament_id:
        flash("Missing player or tournament ID", "error")
        return redirect(url_for("players"))

    deck = Deck.query.filter_by(player_id=player_id, tournament_id=tournament_id).first()
    if deck:
        deck.name = deck_name
        deck.list_text = deck_list
    else:
        deck = Deck(player_id=player_id, tournament_id=tournament_id,
                    name=deck_name, list_text=deck_list)
        db.session.add(deck)

    db.session.commit()
    flash("Deck saved!", "success")
    return redirect(url_for('tournament_round', tid=tournament_id, round_num=1))




@app.route('/tournament/<int:tid>/round/<int:round_num>', methods=['GET', 'POST'])
def tournament_round(tid, round_num):
    tournament = Tournament.query.get_or_404(tid)
    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id) \
                          .filter(TournamentPlayer.tournament_id == tid).all()
    existing_matches = Match.query.filter_by(tournament_id=tid, round_num=round_num).all()

    standings = None

    if tournament.imported_from_text:
        all_matches = Match.query.filter_by(tournament_id=tid).all()

        elo_changes = {p.id: 0 for p in players}
        for m in all_matches:
            if m.result == "bye" and m.player1_id and not m.player2_id:
                p1 = Player.query.get(m.player1_id)
                if p1:
                    phantom = Player(id=-1, name="BYE", elo=DEFAULT_ELO)
                    old1 = p1.elo
                    update_elo(p1, phantom, 2, 0)
                    elo_changes[p1.id] += p1.elo - old1
                    p1.elo = old1
                continue
            if not m.player1_id or not m.player2_id:
                continue
            p1 = Player.query.get(m.player1_id)
            p2 = Player.query.get(m.player2_id)
            if not p1 or not p2:
                continue
            scores = result_to_scores(m.result) or (1, 1)
            old1, old2 = p1.elo, p2.elo
            update_elo(p1, p2, *scores)
            elo_changes[p1.id] += p1.elo - old1
            elo_changes[p2.id] += p2.elo - old2
            p1.elo, p2.elo = old1, old2

        standings = []
        for p in players:
            wins = draws = losses = points = 0
            for m in all_matches:
                if m.player1_id == p.id or m.player2_id == p.id:
                    if m.result == "bye" and m.player1_id == p.id:
                        wins += 1; points += 3
                    elif m.result in ["2-0", "2-1", "1-0"] and m.player1_id == p.id:
                        wins += 1; points += 3
                    elif m.result in ["0-2", "1-2", "0-1"] and m.player2_id == p.id:
                        wins += 1; points += 3
                    elif m.result == "1-1":
                        draws += 1; points += 1
                    else:
                        losses += 1
            deck = Deck.query.filter_by(player_id=p.id, tournament_id=tid).first()

            standings.append({
                "player": p,
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "points": points,
                "elo_delta": elo_changes[p.id],
                "deck": deck,
                "deck_colors": None  # skip backend color calculation
            })





        standings.sort(key=lambda s: (s["points"], s["wins"]), reverse=True)

        return render_template(
            'round.html',
            players=players,
            round_num=round_num,
            tid=tid,
            matches=existing_matches,
            tournament=tournament,
            standings=standings
        )

    # Manual entry workflow
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
                    tournament_id=tid, round_num=round_num,
                    player1_id=None if p1_val == "bye" else int(p1_val),
                    player2_id=None if p2_val == "bye" else int(p2_val),
                    result="bye"
                )
                db.session.add(match)
                winner_id = None if p1_val == "bye" else int(p1_val)
                if winner_id:
                    winner = Player.query.get(winner_id)
                    phantom = Player(id=-1, name="BYE", elo=DEFAULT_ELO)
                    update_elo(winner, phantom, 2, 0)
            else:
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
        flash("Round submitted!", "success")
        return redirect(url_for('tournament_round', tid=tid, round_num=round_num))

    return render_template('round.html', players=players, round_num=round_num,
                           tid=tid, matches=existing_matches, tournament=tournament)




@app.route('/tournament/<int:tid>/apply_top_cut', methods=['POST'])
def apply_top_cut(tid):
    tournament = Tournament.query.get_or_404(tid)
    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id) \
                          .filter(TournamentPlayer.tournament_id == tid).all()
    matches = Match.query.filter_by(tournament_id=tournament.id).all()
    num_players = len(players)

    # Top cut input
    top_cut_val = request.form.get("top_cut")
    try:
        cut = int(top_cut_val) if top_cut_val else None
    except ValueError:
        cut = None

    if cut is None:
        tournament.top_cut = default_top_cut(num_players)
    elif cut > num_players:
        flash(f"Top Cut {cut} is larger than {num_players} players. Using Auto instead.", "error")
        tournament.top_cut = default_top_cut(num_players)
    else:
        tournament.top_cut = cut

    # Finalize Elo: handle byes as wins, skip orphaned references
    for m in matches:
        if m.result == "bye" and m.player1_id and not m.player2_id:
            p1 = Player.query.get(m.player1_id)
            if p1:
                phantom = Player(id=-1, name="BYE", elo=DEFAULT_ELO)
                update_elo(p1, phantom, 2, 0)
            continue

        if not m.player1_id or not m.player2_id:
            continue
        p1 = Player.query.get(m.player1_id)
        p2 = Player.query.get(m.player2_id)
        if not p1 or not p2:
            continue
        scores = result_to_scores(m.result) or (1, 1)
        update_elo(p1, p2, *scores)

    db.session.commit()
    flash("Tournament finalized. Elo updated.", "success")
    return redirect(url_for('players'))



# --- EventLink text import into existing tournament (rounds) ---
@app.route('/tournament/<int:tid>/import_text', methods=['GET', 'POST'])
def import_text(tid):
    tournament = Tournament.query.get_or_404(tid)

    if request.method == 'POST':
        pasted_text = request.form.get('eventlink_text', '')
        if not pasted_text.strip():
            flash("Please paste EventLink text.", "error")
            return redirect(url_for('import_text', tid=tid))

        parsed_matches, _ = parse_eventlink_text(pasted_text)

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

# --- Create tables ---
with app.app_context():
    db.create_all()

# --- Misc ---
@app.route('/tournaments')
def tournaments_list():
    # Query all tournaments ordered by date
    tournaments = Tournament.query.order_by(Tournament.date.desc()).all()

    # Attach player counts
    for t in tournaments:
        t.count = TournamentPlayer.query.filter_by(tournament_id=t.id).count()

    # Separate into competitive and casual lists
    competitive_tournaments = [t for t in tournaments if not t.casual]
    casual_tournaments = [t for t in tournaments if t.casual]

    # Pass both lists to the template
    return render_template(
        'tournaments.html',
        competitive_tournaments=competitive_tournaments,
        casual_tournaments=casual_tournaments
    )


@app.route('/delete_player/<int:pid>', methods=['POST'])
@login_required
def delete_player(pid):
    player = Player.query.get_or_404(pid)
    db.session.delete(player)
    db.session.commit()
    flash(f"Deleted {player.name}", "success")
    return redirect(url_for('players'))

@app.route('/merge_player', methods=['POST'])
@login_required
def merge_player():
    source_id = int(request.form['source_id'])
    target_id = int(request.form['target_id'])
    source = Player.query.get_or_404(source_id)
    target = Player.query.get_or_404(target_id)

    # reassign tournament participations
    for tp in TournamentPlayer.query.filter_by(player_id=source.id).all():
        tp.player_id = target.id

    # recalc Elo for target
    recalc_elo(target.id)

    db.session.delete(source)
    db.session.commit()
    flash(f"Merged {source.name} into {target.name}", "success")
    return redirect(url_for('players'))

def recalc_elo(player_id, k=32):
    player = Player.query.get(player_id)
    if not player:
        return
    player.elo = DEFAULT_ELO

    matches = Match.query.filter(
        (Match.player1_id == player_id) | (Match.player2_id == player_id)
    ).order_by(Match.id).all()

    for m in matches:
        if not m.player2_id:  # bye
            continue

        p1 = Player.query.get(m.player1_id)
        p2 = Player.query.get(m.player2_id)

        scores = result_to_scores(m.result)
        if not scores:
            continue

        if m.player1_id == player_id:
            update_elo(player, p2, *scores, k=k)
        else:
            update_elo(player, p1, scores[1], scores[0], k=k)

    db.session.commit()

@app.route('/tournament/<int:tid>/casual_final', methods=['GET', 'POST'])
@login_required
def casual_final(tid):
    tournament = Tournament.query.get_or_404(tid)
    if not tournament.casual:
        flash("This page is only for casual tournaments.", "error")
        return redirect(url_for('tournament_round', tid=tid, round_num=1))

    if request.method == 'POST':
        # Collect top cut players and their deck lists
        top_cut = int(request.form.get("top_cut"))
        tournament.top_cut = top_cut
        db.session.commit()

        for rank in range(1, top_cut+1):
            player_id = request.form.get(f"player_{rank}")
            deck_name = request.form.get(f"deck_name_{rank}")
            deck_list = request.form.get(f"deck_list_{rank}")
            if player_id and deck_name and deck_list:
                deck = Deck.query.filter_by(player_id=player_id, tournament_id=tournament.id).first()
                if deck:
                    deck.name = deck_name
                    deck.list_text = deck_list
                else:
                    deck = Deck(player_id=player_id, tournament_id=tournament.id,
                                name=deck_name, list_text=deck_list)
                    db.session.add(deck)
        db.session.commit()
        flash("Casual final standings saved!", "success")
        return redirect(url_for('tournament_round', tid=tid, round_num=1))

    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id)\
                          .filter(TournamentPlayer.tournament_id == tid).all()
    return render_template("casual_final.html", tournament=tournament, players=players)



@app.route('/tournament/<int:tid>/standings')
def tournament_standings(tid):
    tournament = Tournament.query.get_or_404(tid)
    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id) \
                          .filter(TournamentPlayer.tournament_id == tid).all()
    matches = Match.query.filter_by(tournament_id=tid).all()

    # Snapshot Elo changes (preview only)
    elo_changes = {p.id: 0 for p in players}
    for m in matches:
        if m.player2_id:
            p1 = Player.query.get(m.player1_id)
            p2 = Player.query.get(m.player2_id)
            scores = result_to_scores(m.result) or (1, 1)
            old1, old2 = p1.elo, p2.elo
            update_elo(p1, p2, *scores)
            elo_changes[p1.id] += p1.elo - old1
            elo_changes[p2.id] += p2.elo - old2
            p1.elo, p2.elo = old1, old2

    standings = []
    for p in players:
        wins = draws = losses = points = 0
        for m in matches:
            if m.player1_id == p.id or m.player2_id == p.id:
                if m.result == "bye" and m.player1_id == p.id:
                    wins += 1; points += 3
                elif m.result in ["2-0", "2-1", "1-0"] and m.player1_id == p.id:
                    wins += 1; points += 3
                elif m.result in ["0-2", "1-2", "0-1"] and m.player2_id == p.id:
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
            "elo_delta": elo_changes[p.id],
            "deck": Deck.query.filter_by(player_id=p.id, tournament_id=tid).first()
        })

    standings.sort(key=lambda s: (s["points"], s["wins"]), reverse=True)
    return render_template('tournament_standings.html',
                           tournament=tournament,
                           standings=standings)




@app.route('/remove_deck', methods=['POST'])
def remove_deck():
    player_id = request.form.get("player_id")
    tournament_id = request.form.get("tournament_id")
    deck = Deck.query.filter_by(player_id=player_id, tournament_id=tournament_id).first()
    if deck:
        db.session.delete(deck)
        db.session.commit()
        flash("Deck removed.", "success")
    return redirect(url_for('tournament_round', tid=tournament_id, round_num=1))


def fetch_deck_colors(deck_list_text: str) -> str:
    colors = set()
    for line in deck_list_text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("sideboard"):
            continue
        parts = line.split(maxsplit=1)
        name = parts[1] if len(parts) > 1 else parts[0]
        print("DEBUG: Looking up card name ->", name)   # <--- debug
        resp = requests.get("https://api.scryfall.com/cards/named", params={"fuzzy": name})
        if resp.ok:
            data = resp.json()
            print("DEBUG: Scryfall returned colors ->", data.get("color_identity"))  # <--- debug
            for c in data.get("color_identity", []):
                colors.add(c)
        else:
            print("DEBUG: Scryfall request failed for", name, resp.status_code)
    order = "WUBRG"
    result = "".join([c for c in order if c in colors])
    print("DEBUG: Final deck colors ->", result)   # <--- debug
    return result


def compute_colors_from_list(deck_list_text: str) -> str:
    colors = set()
    for line in deck_list_text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("sideboard"):
            continue
        parts = line.split(maxsplit=1)
        name = parts[1] if len(parts) > 1 else parts[0]
        resp = requests.get("https://api.scryfall.com/cards/named", params={"fuzzy": name})
        if resp.ok:
            data = resp.json()
            for c in data.get("color_identity", []):
                colors.add(c)
    order = "WUBRG"
    return "".join([c for c in order if c in colors])

# --- Run locally ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))