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
import secrets


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

DEFAULT_ELO = 1000

# --- Login ---
login_manager = LoginManager(app)
login_manager.login_view = "google.login"

class User(db.Model, UserMixin):
    __bind_key__ = 'users'
    __tablename__ = 'users'   # explicitly name the table
    id = db.Column(db.String(255), primary_key=True)
    name = db.Column(db.String(120))
    email = db.Column(db.String(120), unique=True)
    is_admin = db.Column(db.Boolean, default=False)
    is_scorekeeper = db.Column(db.Boolean, default=False)

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
    country = db.Column(db.String(5), nullable=True)
    casual_points = db.Column(db.Integer, default=0)   # NEW

class Tournament(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    date = db.Column(db.Date, nullable=False)
    rounds = db.Column(db.Integer, nullable=False)
    imported_from_text = db.Column(db.Boolean, default=False)
    top_cut = db.Column(db.Integer, nullable=True)
    casual = db.Column(db.Boolean, default=False) 
    premium = db.Column(db.Boolean, default=False)  
    player_count = db.Column(db.Integer, nullable=True) 
    country = db.Column(db.String(5), nullable=True)  #ISO code like "AR", "BR", "CL"
    pending = db.Column(db.Boolean, default=True)
    confirm_token = db.Column(db.String(64), nullable=True)
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'), nullable=True)
    user_id = db.Column(db.String(255))
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    

class TournamentPlayer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)

    tournament = db.relationship('Tournament', backref='tournament_players')
    player = db.relationship('Player', backref='tournament_players')

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

class Store(db.Model):
    __tablename__ = 'stores'   # default bind (tournament.db)
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    location = db.Column(db.String(120))
    country = db.Column(db.String(5))
    premium = db.Column(db.Boolean, default=False)
    image_url = db.Column(db.String(255))

    tournaments = db.relationship("Tournament", backref="store")
    assignments = db.relationship(
        "StoreAssignment",
        primaryjoin="Store.id==foreign(StoreAssignment.store_id)",
        viewonly=True
        )


class StoreAssignment(db.Model):
    __bind_key__ = 'users'     # lives in users.db
    __tablename__ = 'store_assignments'
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, nullable=False)  # just an integer reference
    user_id = db.Column(db.String(255), db.ForeignKey('users.id'), nullable=False)

    user = db.relationship("User", backref="store_assignments")
    

    __table_args__ = (db.UniqueConstraint('store_id', 'user_id', name='uq_store_user'),)


def stores_for_user(user: User):
    if not user:
        return []
    store_ids = [a.store_id for a in user.store_assignments]
    if not store_ids:
        return []
    return Store.query.filter(Store.id.in_(store_ids)).all()


def can_use_store(user: User, store_id: int) -> bool:
    if not user:
        return False
    if user.is_admin:
        return True
    return any(a.store_id == store_id for a in user.store_assignments)



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

def tournaments_played(player_id: int) -> int:
    return TournamentPlayer.query.filter_by(player_id=player_id).count()



def update_elo(player_a, player_b, score_a, score_b, tournament: Tournament = None):
    """
    Update Elo ratings for two players based on match result.
    - player_a, player_b: Player objects
    - score_a, score_b: numeric scores (e.g. 2,0 for a 2-0 win)
    - tournament: Tournament object (used for K-factor modifiers)
    """

    # Skip Elo updates if either side is a bye
    if player_a is None or player_b is None:
        return

    # Convert scores to result values (S)
    if score_a > score_b:
        result_a, result_b = 1, 0
    elif score_a < score_b:
        result_a, result_b = 0, 1
    else:
        result_a = result_b = 0.5

    # Expected scores
    expected_a = 1 / (1 + 10 ** ((player_b.elo - player_a.elo) / 400))
    expected_b = 1 - expected_a  # simplifies calculation

    # Base K-factor depending on tournament type
    if tournament and tournament.premium:
        k = 48
    elif tournament and tournament.casual:
        k = 16
    else:
        k = 32

    # Apply provisional multiplier for first 3 tournaments
    played_a = tournaments_played(player_a.id)
    played_b = tournaments_played(player_b.id)

    k_a = k * 3 if played_a < 3 else k
    k_b = k * 3 if played_b < 3 else k

    # Update Elo ratings (rounded, not truncated)
    player_a.elo += round(k_a * (result_a - expected_a))
    player_b.elo += round(k_b * (result_b - expected_b))



def award_casual_points(tournament: Tournament, rank: int, top_cut: int):
    """
    tournament: Tournament object (casual=True)
    rank: player's finishing position (1-based)
    top_cut: tournament top cut size (e.g. 4, 8, 16, 32)
    Returns points to award
    """
    num_players = tournament.player_count or 0
    if num_players < 9:
        return 0

    if 9 <= num_players <= 16:
        if rank == 1: return 2
        if rank == 2: return 1

    elif 17 <= num_players <= 32:
        if rank == 1: return 4
        if rank == 2: return 3
        if rank <= 4: return 2
        if rank <= 8: return 1

    elif num_players >= 33:
        if rank == 1: return 8
        if rank == 2: return 6
        if rank <= 4: return 4
        if rank <= 8: return 2

    return 0


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
        action = request.form.get("action", "").strip()

        if action == "add_by_email":
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

        elif action in ["approve_scorekeeper", "approve_admin", "deny_request"]:
            req_id = request.form.get("request_id")
            req = AccessRequest.query.get(req_id)
            if not req:
                flash("Request not found.", "error")
                return redirect(url_for("admin_panel"))

            if action == "approve_scorekeeper":
                req.user.is_scorekeeper = True
                req.reviewed = True
                flash(f"{req.user.email} approved as scorekeeper.", "success")

            elif action == "approve_admin":
                req.user.is_admin = True
                req.reviewed = True
                flash(f"{req.user.email} approved as admin.", "success")

            elif action == "deny_request":
                req.reviewed = True
                flash(f"Request from {req.user.email} denied.", "error")

            db.session.commit()

        elif action == "delete_user_db":
            engine = db.get_engine(app, bind="users")
            db.Model.metadata.drop_all(bind=engine, tables=[User.__table__, AccessRequest.__table__])
            db.Model.metadata.create_all(bind=engine, tables=[User.__table__, AccessRequest.__table__])
            flash("User database has been reset (tables dropped and recreated).", "success")

        elif action == "delete_tournament_db":
            db.Model.metadata.drop_all(bind=db.engine)
            db.Model.metadata.create_all(bind=db.engine)
            flash("Tournament database has been deleted and recreated.", "success")

        elif action == "delete_all_players":
            Player.query.delete()
            db.session.commit()
            flash("All players have been deleted.", "success")

        else:
            # toggle roles
            user_id = request.form.get("user_id")
            user = User.query.get(user_id)
            if user:
                make_admin = request.form.get("make_admin")
                make_scorekeeper = request.form.get("make_scorekeeper")
                if make_admin is not None:
                    user.is_admin = (make_admin == "true")
                if make_scorekeeper is not None:
                    user.is_scorekeeper = (make_scorekeeper == "true")
                db.session.commit()
                flash(f"Updated roles for {user.email}", "success")

        return redirect(url_for("admin_panel"))

    # --- GET request: load data for template ---
    users = User.query.all()
    requests = AccessRequest.query.order_by(AccessRequest.date_submitted.desc()).all()
    stores = Store.query.all()

    # No events list needed; template will just show "Coming soon"
    return render_template("admin.html", users=users, requests=requests, stores=stores)


@app.route("/admin/stores", methods=["GET", "POST"])
@login_required
def admin_stores():
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for("players"))

    if request.method == "POST":
        store_id = request.form.get("store_id")
        store_name = request.form.get("store_name", "").strip()
        location = request.form.get("location", "").strip()
        country = request.form.get("country", "").strip()
        premium = bool(request.form.get("premium"))

        if store_id:
            # Update existing store by ID
            store = Store.query.get(store_id)
            if store:
                store.name = store_name
                store.location = location
                store.country = country
                store.premium = premium
                db.session.commit()
                flash(f"Store '{store.name}' updated.", "success")
            else:
                flash("Store not found.", "error")

        elif store_name:
            # Create new store if no ID provided
            existing = Store.query.filter_by(name=store_name).first()
            if existing:
                flash(f"Store '{store_name}' already exists.", "error")
            else:
                store = Store(
                    name=store_name,
                    location=location,
                    country=country,
                    premium=premium
                )
                db.session.add(store)
                db.session.commit()
                flash(f"Store '{store_name}' created.", "success")

        return redirect(url_for("admin_stores"))

    # GET request: show stores and users
    stores = Store.query.all()
    users = User.query.all()
    requests = AccessRequest.query.filter_by(reviewed=False).order_by(AccessRequest.date_submitted.desc()).all()
    return render_template("admin.html", users=users, requests=requests, stores=stores)


@app.route("/admin/store/<int:store_id>/assign", methods=["POST"])
@login_required
def assign_user_to_store(store_id):
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for("admin_stores"))

    email = request.form.get("email", "").strip()
    if not email:
        flash("Email is required.", "error")
        return redirect(url_for("admin_stores"))

    user = User.query.filter_by(email=email).first()
    if user:
        existing = StoreAssignment.query.filter_by(store_id=store_id, user_id=user.id).first()
        if existing:
            flash(f"User {email} is already assigned to this store.", "info")
        else:
            assignment = StoreAssignment(store_id=store_id, user_id=user.id)
            db.session.add(assignment)
            db.session.commit()
            flash(f"User {email} assigned to store.", "success")
    else:
        flash("User not found.", "error")

    return redirect(url_for("admin_stores"))



@app.route("/admin/store/<int:store_id>/remove/<user_id>", methods=["POST"])
@login_required
def remove_user_from_store(store_id, user_id):
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for("admin_stores"))

    assignment = StoreAssignment.query.filter_by(store_id=store_id, user_id=user_id).first()
    if assignment:
        db.session.delete(assignment)
        db.session.commit()
        flash("User removed from store.", "success")
    return redirect(url_for("admin_stores"))

@app.route("/store/<int:store_id>/edit_image", methods=["POST"])
@login_required
def edit_store_image(store_id):
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for("admin_stores"))

    file = request.files.get("image")
    if not file or file.filename == "":
        flash("No file selected", "error")
        return redirect(url_for("admin_stores"))

    if not file.filename.lower().endswith(".jpg"):
        flash("Only .jpg files allowed", "error")
        return redirect(url_for("admin_stores"))

    filename = secure_filename(file.filename)
    upload_dir = os.path.join(app.static_folder, "store_images")
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, filename)

    img = Image.open(file).convert("RGB")

  
    img = img.resize((512, 256))
    img.save(path, "JPEG")


    store = Store.query.get(store_id)
    if store:
        store.image_url = f"store_images/{filename}"
        db.session.commit()
        flash("Store image updated.", "success")

    return redirect(url_for("admin_stores"))






#@app.route("/make_me_admin")
#@login_required
#def make_me_admin():
#    current_user.is_admin = True
#    db.session.commit()
#    flash("You are now an admin!", "success")
#    return redirect(url_for("admin_panel"))

@app.route("/request_access", methods=["GET", "POST"])
@login_required
def request_access():
    if request.method == "POST":
        reason = request.form.get("reason")
        # Save or email the request here
        flash("Your access request has been submitted.", "success")
        return redirect(url_for("players"))
    return render_template("request_access.html")


class AccessRequest(db.Model):
    __bind_key__ = 'users'    # same bind as User
    __tablename__ = 'access_requests'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(255), db.ForeignKey('users.id'), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    date_submitted = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed = db.Column(db.Boolean, default=False)

    user = db.relationship('User', backref='access_requests')


@app.route("/admin/access_requests")
@login_required
def access_requests():
    if not current_user.is_admin:
        flash("You do not have permission to view access requests.", "error")
        return redirect(url_for("players"))

    requests = AccessRequest.query.order_by(AccessRequest.date_submitted.desc()).all()
    return render_template("access_requests.html", requests=requests)



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

@app.route("/profile/settings", methods=["GET", "POST"])
@login_required
def profile_settings():
    if request.method == "POST":
        new_name = request.form.get("name")
        if new_name:
            current_user.name = new_name
            db.session.commit()
            flash("Profile updated successfully!", "success")

    # 👇 Collect the stores assigned to this user
    stores = Store.query.filter(
        Store.id.in_([a.store_id for a in current_user.store_assignments])
    ).all()

    # Pass `stores` into the template
    return render_template("profile_settings.html", stores=stores)



@app.route("/user/<user_id>/tournaments")
@login_required
def user_tournaments(user_id):
    tournaments = Tournament.query.filter_by(user_id=user_id).order_by(Tournament.date.desc()).all()
    return render_template("user_tournaments.html", tournaments=tournaments)





@app.route('/players', methods=['GET', 'POST'])
def players():
    # === Handle new player creation ===
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if name:
            new_player = Player(name=name)
            db.session.add(new_player)
            db.session.commit()
            flash(f"Player '{name}' added.", "success")
        else:
            flash("Player name cannot be empty.", "error")
        return redirect(url_for('players'))

    # --- Optional country filter ---
    country_filter = request.args.get("country")

    # Helper: compute a player's "home country" based on tournaments played
    def player_country(player_id):
        tps = TournamentPlayer.query.filter_by(player_id=player_id).all()
        countries = []
        for tp in tps:
            tournament = Tournament.query.get(tp.tournament_id)
            if tournament and tournament.country and tournament.country.strip():
                countries.append(tournament.country)
        if not countries:
            return None
        from collections import Counter
        counts = Counter(countries)
        max_count = max(counts.values())
        tied = [c for c, cnt in counts.items() if cnt == max_count]
        if len(tied) == 1:
            return tied[0]
        # tie → first encountered
        for c in countries:
            if c in tied:
                return c
        return None

    # === Competitive ranking (Elo) ===
    all_competitive_players = (
        db.session.query(Player)
        .join(TournamentPlayer, Player.id == TournamentPlayer.player_id)
        .join(Tournament, Tournament.id == TournamentPlayer.tournament_id)
        .filter(Tournament.pending == False)   # 🔒 only finalized tournaments
        .order_by(Player.elo.desc())
        .all()
    )

    if country_filter:
        all_competitive_players = [
            p for p in all_competitive_players
            if player_country(p.id) and player_country(p.id).upper() == country_filter.upper()
        ]

    competitive_ranked = []
    rank = 1
    for p in all_competitive_players:
        num_tournaments = tournaments_played(p.id)
        if num_tournaments >= 3:
            competitive_ranked.append({"player": p, "rank": rank})
            rank += 1
        else:
            # always include, but mark rank as None so template shows "-"
            competitive_ranked.append({"player": p, "rank": None, "provisional": True})

    # sort so ranked players come first, unranked ("–") go to the bottom
    competitive_ranked.sort(
        key=lambda x: (x["rank"] is None, x["rank"] if x["rank"] is not None else float("inf"))
    )


    # === Casual ranking (points) ===
    casual_players = (
        db.session.query(Player)
        .join(TournamentPlayer, Player.id == TournamentPlayer.player_id)
        .join(Tournament, Tournament.id == TournamentPlayer.tournament_id)
        .filter(Tournament.pending == False)   # 🔒 only finalized tournaments
        .order_by(Player.casual_points.desc())
        .all()
    )
    if country_filter:
        casual_players = [
            p for p in casual_players
            if player_country(p.id) and player_country(p.id).upper() == country_filter.upper()
        ]
    casual_ranked = [{"player": p, "rank": idx} for idx, p in enumerate(casual_players, start=1)]

    # === Top 4 archetypes by number of decks ===
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

    # === Render template ===
    return render_template(
        'players.html',
        players=competitive_ranked,       # Competitive Elo (with hidden/unranked logic)
        casual_players=casual_ranked,     # Casual Ranking
        top_decks=top_decks,
        player_country=player_country,
        tournaments_played=tournaments_played
    )








@app.route('/decks', methods=['GET', 'POST'])
def decks_list():
    if request.method == 'POST':
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Admins only", "error")
            return redirect(url_for('decks_list'))

        deck_name = request.form.get("deck_name", "").strip()
        if deck_name:
            new_deck = Deck(name=deck_name, list_text="", colors="", image_url=None, player_id=0, tournament_id=None)
            db.session.add(new_deck)
            db.session.commit()
            flash(f"Archetype '{deck_name}' created", "success")
        return redirect(url_for('decks_list'))

    # existing GET logic
    decks = Deck.query.filter(Deck.name.isnot(None)).all()
    archetypes = {}
    for d in decks:
        archetypes.setdefault(d.name, []).append(d)

    archetype_colors = {}
    for name, deck_list in archetypes.items():
        last_deck = sorted(deck_list, key=lambda d: d.id)[-1]
        archetype_colors[name] = last_deck.colors or ""

    return render_template("decks.html", archetypes=archetypes, archetype_colors=archetype_colors)


@app.route('/decks/<deck_name>')
def deck_detail(deck_name):
    decks = Deck.query.filter_by(name=deck_name).all()
    rows = []
    for d in decks:
        player = Player.query.get(d.player_id) if d.player_id else None
        tournament = Tournament.query.get(d.tournament_id) if d.tournament_id else None

        rank = None
        if player and tournament:
            tp = TournamentPlayer.query.filter_by(
                tournament_id=tournament.id,
                player_id=player.id
            ).first()
            rank = getattr(tp, "rank", None) if tp else None

        rows.append({
            "player": player,
            "tournament": tournament,
            "rank": rank,
            "deck": d
        })

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
        tournament_type = request.form.get("tournament_type", "competitive")

        # --- Manual workflow ---
        if workflow == 'manual':
            tournament_name = request.form.get('tournament_name')
            date_str = request.form.get('date')
            player_names = [n.strip() for n in request.form.getlist('players') if n.strip()]
            player_objs = [ensure_player(name) for name in player_names]

            num_players = len(player_objs)
            rounds = 3 if num_players <= 8 else 4 if num_players <= 16 else \
                     5 if num_players <= 32 else 6 if num_players <= 64 else 7
            country = request.form.get("country")

            tournament = Tournament(
                name=tournament_name,
                date=datetime.strptime(date_str, "%Y-%m-%d"),
                rounds=rounds,
                imported_from_text=False,
                country=country,
                casual=(tournament_type == "casual"),
                premium=(tournament_type == "premium"),
                pending=True,
                user_id=current_user.id,
                submitted_at=datetime.utcnow()
            )
            db.session.add(tournament)
            db.session.commit()

            for p in player_objs:
                db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=p.id))
                if not p.country and tournament.country:
                    p.country = tournament.country
            db.session.commit()

            return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

        # --- Import workflow ---
        elif workflow == 'import':
            import_format = request.form.get('import_format')
            raw_text = request.form.get('import_text', '')

            def gen_token():
                return secrets.token_urlsafe(16)

            # EventLink import
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

                tournament = Tournament(
                    name=event_name,
                    date=datetime.today().date(),
                    rounds=rounds,
                    imported_from_text=True,
                    casual=(tournament_type == "casual"),
                    premium=(tournament_type == "premium"),
                    pending=True,
                    confirm_token=gen_token(),
                    user_id=current_user.id,
                    submitted_at=datetime.utcnow()
                )
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

                confirm_url = url_for('tournament_round', tid=tournament.id, round_num=1,
                                      token=tournament.confirm_token, _external=True)
                flash(f"Tournament created from EventLink text. Confirmation link: {confirm_url}", "success")
                return redirect(confirm_url)

            # Arena import
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

                tournament = Tournament(
                    name=event_name,
                    date=datetime.today().date(),
                    rounds=rounds,
                    imported_from_text=True,
                    casual=(tournament_type == "casual"),
                    premium=(tournament_type == "premium"),
                    pending=True,
                    confirm_token=secrets.token_urlsafe(16),
                    user_id=current_user.id,
                    submitted_at=datetime.utcnow()
                )
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

                confirm_url = url_for('tournament_round', tid=tournament.id, round_num=1,
                                      token=tournament.confirm_token, _external=True)
                flash(f"Tournament created from Arena text. Confirmation link: {confirm_url}", "success")
                return redirect(confirm_url)

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

from collections import Counter

def player_country(player_id):
    tournaments = (
        db.session.query(Tournament.country)
        .join(TournamentPlayer, Tournament.id == TournamentPlayer.tournament_id)
        .filter(TournamentPlayer.player_id == player_id, Tournament.country.isnot(None))
        .all()
    )
    if not tournaments:
        return None
    countries = [c[0] for c in tournaments]
    counts = Counter(countries)
    # max count; if tie, earliest tournament
    max_count = max(counts.values())
    top_countries = [c for c, v in counts.items() if v == max_count]
    if len(top_countries) == 1:
        return top_countries[0]
    else:
        first_tournament = (
            db.session.query(Tournament.country)
            .join(TournamentPlayer, Tournament.id == TournamentPlayer.tournament_id)
            .filter(TournamentPlayer.player_id == player_id)
            .order_by(Tournament.date.asc())
            .first()
        )
        return first_tournament[0] if first_tournament else None


@app.route('/tournament/new_casual', methods=['GET', 'POST'])
@login_required
def new_tournament_casual():
    if request.method == 'POST':
        tournament_name = request.form.get('tournament_name')
        date_str = request.form.get('date')
        top_cut = int(request.form.get('top_cut') or 0)
        player_count = int(request.form.get('player_count') or 0)
        rounds = int(request.form.get('rounds') or 0)
        country = request.form.get("country")

        # Create the casual tournament
        tournament = Tournament(
            name=tournament_name,
            date=datetime.strptime(date_str, "%Y-%m-%d"),
            rounds=rounds,
            imported_from_text=False,
            casual=True,
            top_cut=top_cut,
            player_count=player_count,
            country=country,
            user_id=current_user.id,
            submitted_at=datetime.utcnow()
        )
        db.session.add(tournament)
        db.session.commit()

        # Helper: award casual points based on rules
        def award_casual_points(num_players: int, rank: int) -> int:
            if num_players < 9:
                return 0
            if 9 <= num_players <= 16:
                if rank == 1: return 2
                if rank == 2: return 1
            elif 17 <= num_players <= 32:
                if rank == 1: return 4
                if rank == 2: return 3
                if rank <= 4: return 2
                if rank <= 8: return 1
            elif num_players >= 33:
                if rank == 1: return 8
                if rank == 2: return 6
                if rank <= 4: return 4
                if rank <= 8: return 2
            return 0

        # Loop through final standings rows
        for rank in range(1, top_cut + 1):
            player_name = request.form.get(f"player_{rank}", "").strip()
            deck_name = html.escape(request.form.get(f"deck_name_{rank}", "").strip())
            deck_list = html.escape(request.form.get(f"deck_list_{rank}", "").strip())

            if player_name:
                player = ensure_player(player_name)
                db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=player.id))

                # Award casual points
                pts = award_casual_points(player_count, rank)
                if pts > 0:
                    player.casual_points = (player.casual_points or 0) + pts

                # Save deck if provided
                if deck_name or deck_list:
                    deck = Deck.query.filter_by(player_id=player.id, tournament_id=tournament.id).first()
                    if deck:
                        deck.name = deck_name
                        deck.list_text = deck_list
                    else:
                        deck = Deck(
                            player_id=player.id,
                            tournament_id=tournament.id,
                            name=deck_name,
                            list_text=deck_list
                        )
                        db.session.add(deck)

        db.session.commit()
        flash("Casual tournament final standings saved!", "success")
        return redirect(url_for('players'))

    return render_template('new_tournament_casual.html')


@app.route('/tournament/<int:tid>/set_country', methods=['POST'])
@login_required
def set_tournament_country(tid):
    tournament = Tournament.query.get_or_404(tid)

    # 🚨 Security guard: only admins or scorekeepers can change
    if not (current_user.is_admin or current_user.is_scorekeeper):
        flash("You do not have permission to set the country.", "error")
        return redirect(url_for('tournament_round', tid=tid, round_num=1))

    # 🚨 Block changes if tournament is already finalized/discarded
    if tournament.imported_from_text and not tournament.pending:
        flash("This tournament has already been finalized or discarded. Country cannot be changed.", "error")
        return redirect(url_for('players'))

    # === Validate input ===
    country = request.form.get("country")
    if not country:
        flash("Please select a country.", "error")
        return redirect(url_for('tournament_round', tid=tid, round_num=1))

    # === Apply change ===
    tournament.country = country
    db.session.commit()

    flash(f"Tournament country set to {country}.", "success")
    return redirect(url_for('tournament_round', tid=tid, round_num=1))

@app.route("/tournament/<int:tid>/set_store", methods=["POST"])
@login_required
def set_tournament_store(tid):
    tournament = Tournament.query.get_or_404(tid)

    # Only admins or scorekeepers can change
    if not (current_user.is_admin or current_user.is_scorekeeper):
        flash("You do not have permission to set the store.", "error")
        return redirect(url_for('tournament_round', tid=tid, round_num=1))

    # Block changes if imported tournament is already finalized/discarded
    if tournament.imported_from_text and not tournament.pending:
        flash("This tournament has already been finalized or discarded. Store cannot be changed.", "error")
        return redirect(url_for('players'))

    store_id = request.form.get("store_id")
    if not store_id:
        flash("Please select a store.", "error")
        return redirect(url_for('tournament_round', tid=tid, round_num=1))

    try:
        store_id_int = int(store_id)
    except ValueError:
        flash("Invalid store.", "error")
        return redirect(url_for('tournament_round', tid=tid, round_num=1))

    store = Store.query.get(store_id_int)
    if not store:
        flash("Store not found.", "error")
        return redirect(url_for('tournament_round', tid=tid, round_num=1))

    # Ensure this user can use that store
    if not can_use_store(current_user, store.id):
        flash("You are not assigned to this store.", "error")
        return redirect(url_for('tournament_round', tid=tid, round_num=1))

    # Apply store and auto-country
    tournament.store_id = store.id
    tournament.country = store.country
    db.session.commit()

    flash(f"Store set to {store.name}. Country auto-set to {store.country}.", "success")
    return redirect(url_for('tournament_round', tid=tid, round_num=1))


@app.route('/player/<int:pid>')
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
        rank = getattr(tp, "rank", None)

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

    # Helper: get most recent tournament country for a player
    def player_country(player_id):
        tp = (
            TournamentPlayer.query.filter_by(player_id=player_id)
            .order_by(TournamentPlayer.id.desc())
            .first()
        )
        if tp:
            tournament = Tournament.query.get(tp.tournament_id)
            if tournament and tournament.country:
                return tournament.country
        return None

    return render_template(
        "playerinfo.html",
        player=player,
        history=history,
        stats=stats,
        player_country=player_country   # <-- pass helper
    )





# --- Confirm Players ---
@app.route('/confirm_players', methods=['POST'])
@login_required
def confirm_players():
    raw_text = request.form.get("raw_text", "")
    event_name = request.form.get("event_name") or "Imported Tournament"
    parsed_matches = parse_arena_text(raw_text)

    # Resolve each name to a final player ID
    for m in parsed_matches:
        for role in ["player", "opponent"]:
            name = m.get(role)
            if not name:
                continue

            safe_name = name.replace(" ", "_")
            action = request.form.get(f"action_{safe_name}")

            if action == "create":
                player = Player.query.filter_by(name=name).first()
                if not player:
                    player = Player(name=name, elo=DEFAULT_ELO)
                    db.session.add(player)
                    db.session.commit()
                m[role] = player.id

            elif action == "replace":
                replace_id_str = request.form.get(f"replace_{safe_name}")
                if replace_id_str:
                    replace_id = int(replace_id_str)
                    current = Player.query.filter_by(name=name).first()
                    if current and current.id != replace_id:
                        merge_player_ids(current.id, replace_id)
                    m[role] = replace_id
                else:
                    player = Player.query.filter_by(name=name).first()
                    if player:
                        m[role] = player.id

            else:
                player = Player.query.filter_by(name=name).first()
                if player:
                    m[role] = player.id

    # Collect final IDs from parsed matches
    final_ids = {
        pid for m in parsed_matches
        for pid in [m.get("player"), m.get("opponent")]
        if isinstance(pid, int)
    }

    # Create the tournament with a confirm_token
    num_players = len(final_ids)
    rounds = 3 if num_players <= 8 else 4 if num_players <= 16 else \
             5 if num_players <= 32 else 6 if num_players <= 64 else 7

    tournament = Tournament(
        name=event_name,
        date=datetime.today().date(),
        rounds=rounds,
        imported_from_text=True,
        pending=True,
        confirm_token=secrets.token_urlsafe(16),
        user_id=current_user.id,
        submitted_at=datetime.utcnow()
    )
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

    # Redirect with token to avoid invalid/expired link errors
    confirm_url = url_for(
        'tournament_round',
        tid=tournament.id,
        round_num=1,
        token=tournament.confirm_token
    )
    return redirect(confirm_url)









@app.route('/submit_deck', methods=['POST'])
@login_required
def submit_deck():
    player_id = request.form.get("player_id")
    tournament_id = request.form.get("tournament_id")

    # 🚨 Validate input
    if not player_id or not tournament_id:
        flash("Missing player or tournament ID.", "error")
        return redirect(url_for("players"))

    tournament = Tournament.query.get_or_404(tournament_id)

    # 🚨 Security guard: block submission if tournament is finalized/discarded
    if tournament.imported_from_text and not tournament.pending:
        flash("This tournament has already been finalized or discarded. Decks cannot be submitted.", "error")
        return redirect(url_for("players"))

    # === Prepare deck data ===
    deck_name = html.escape(request.form.get("deck_name", "").strip())
    deck_list = request.form.get("deck_list", "").strip()  # store raw text

    # === Save or update deck ===
    deck = Deck.query.filter_by(player_id=player_id, tournament_id=tournament_id).first()
    if deck:
        deck.name = deck_name
        deck.list_text = deck_list
    else:
        deck = Deck(
            player_id=player_id,
            tournament_id=tournament_id,
            name=deck_name,
            list_text=deck_list
        )
        db.session.add(deck)

    db.session.commit()
    flash("Deck saved!", "success")

    return redirect(url_for('tournament_round', tid=tournament_id, round_num=1))



@app.route("/archetype/<name>/edit_name", methods=["POST"])
@login_required
def edit_archetype_name(name):
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for("decks_list"))

    new_name = request.form.get("new_name", "").strip()
    if not new_name:
        flash("New name required", "error")
        return redirect(url_for("decks_list"))

    # Update all decks with this archetype name
    decks = Deck.query.filter_by(name=name).all()
    for d in decks:
        d.name = new_name
    db.session.commit()
    flash(f"Archetype renamed to {new_name}", "success")
    return redirect(url_for("decks_list"))

@app.route("/archetype/add_deck", methods=["POST"])
@login_required
def add_deck():
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for("decks_list"))

    deck_name = request.form.get("deck_name", "").strip()
    if not deck_name:
        flash("Deck name required", "error")
        return redirect(url_for("decks_list"))

    # Archetype and deck name are the same
    new_deck = Deck(name=deck_name, list_text="", colors="", image_url=None, player_id=0)
    db.session.add(new_deck)
    db.session.commit()
    flash(f"New deck archetype '{deck_name}' created", "success")
    return redirect(url_for("decks_list"))


from flask import session

@app.route('/tournament/<int:tid>/round/<int:round_num>', methods=['GET', 'POST'])
def tournament_round(tid, round_num):
    tournament = Tournament.query.get_or_404(tid)

    # Block access if imported tournament is already finalized/discarded
    if tournament.imported_from_text and not tournament.pending:
        flash("This tournament has already been finalized or discarded.", "error")
        return redirect(url_for('players'))

    # Token guard for imported, pending tournaments
    if tournament.imported_from_text and tournament.pending:
        session_key = f"tok_{tid}"
        url_token = request.args.get("token")
        session_token = session.get(session_key)

        if url_token and url_token == tournament.confirm_token:
            session[session_key] = url_token  # bind token to session
        elif not session_token or session_token != tournament.confirm_token:
            flash("Invalid or expired confirmation link.", "error")
            return redirect(url_for('players'))

    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id) \
                          .filter(TournamentPlayer.tournament_id == tid).all()
    existing_matches = Match.query.filter_by(tournament_id=tid, round_num=round_num).all()

    # Build stores list for selector
    stores = []
    if current_user.is_authenticated:
        if current_user.is_admin:
            stores = Store.query.order_by(Store.name.asc()).all()
        else:
            stores = stores_for_user(current_user)

    # Imported tournament preview workflow
    if tournament.imported_from_text:
        all_matches = Match.query.filter_by(tournament_id=tid).all()

        elo_changes = {p.id: 0 for p in players}
        for m in all_matches:
            if m.result == "bye" and m.player1_id and not m.player2_id:
                p1 = Player.query.get(m.player1_id)
                if p1:
                    phantom = Player(id=-1, name="BYE", elo=DEFAULT_ELO)
                    old1 = p1.elo
                    
                    if tournament and not tournament.pending:
                        update_elo(p1, phantom, 2, 0, tournament)

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
            update_elo(p1, p2, *scores, tournament)
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
                "deck_colors": None
            })

        standings.sort(key=lambda s: (s["points"], s["wins"]), reverse=True)

        return render_template(
            'round.html',
            players=players,
            round_num=round_num,
            tid=tid,
            matches=existing_matches,
            tournament=tournament,
            standings=standings,
            confirm_token=session.get(f"tok_{tid}"),
            stores=stores
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
                    update_elo(winner, phantom, 2, 0, tournament)
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
                update_elo(player1, player2, games_a, games_b, tournament)

        db.session.commit()
        flash("Round submitted!", "success")
        return redirect(url_for('tournament_round', tid=tid, round_num=round_num))

    return render_template(
        'round.html',
        players=players,
        round_num=round_num,
        tid=tid,
        matches=existing_matches,
        tournament=tournament,
        confirm_token=session.get(f"tok_{tid}"),
        stores=stores
    )



from flask import make_response

@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route('/tournament/<int:tid>/discard', methods=['POST'])
@login_required
def discard_tournament(tid):
    app.logger.info(f"Discard called for tournament {tid}")
    tournament = Tournament.query.get_or_404(tid)

    # Only admins or scorekeepers can discard
    if not (current_user.is_admin or current_user.is_scorekeeper):
        app.logger.warning(f"Unauthorized discard attempt for tournament {tid}")
        return ("", 403)

    # Only allow discarding imported, still-pending tournaments
    if tournament.imported_from_text and tournament.pending:
        # Delete related rows to avoid integrity errors
        TournamentPlayer.query.filter_by(tournament_id=tournament.id).delete()
        Match.query.filter_by(tournament_id=tournament.id).delete()
        Deck.query.filter_by(tournament_id=tournament.id).delete()

        db.session.delete(tournament)
        db.session.commit()
        app.logger.info(f"Tournament {tid} and related records deleted")
        return ("", 204)

    app.logger.info(f"Tournament {tid} not pending or not imported; discard ignored")
    return ("", 204)




@app.route('/tournament/<int:tid>/apply_top_cut', methods=['POST'])
@login_required
def apply_top_cut(tid):
    # Always re-query fresh, and bail if missing
    tournament = Tournament.query.get(tid)
    if not tournament:
        flash("Tournament not found or already discarded.", "error")
        return redirect(url_for('players'))

    # Only admins or scorekeepers can finalize
    if not (current_user.is_admin or current_user.is_scorekeeper):
        flash("You do not have permission to finalize this tournament.", "error")
        return redirect(url_for('players'))

    # Block if already finalized/discarded
    if not tournament.pending:
        flash("Tournament was already finalized or discarded.", "error")
        return redirect(url_for('players'))

    # Gather players and matches
    players = (
        Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id)
        .filter(TournamentPlayer.tournament_id == tid)
        .all()
    )
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

    # Finalize Elo
    for m in matches:
        if m.result == "bye" and m.player1_id and not m.player2_id:
            p1 = Player.query.get(m.player1_id)
            if p1:
                phantom = Player(id=-1, name="BYE", elo=DEFAULT_ELO)
                update_elo(p1, phantom, 2, 0, tournament)
            continue

        if not m.player1_id or not m.player2_id:
            continue
        p1 = Player.query.get(m.player1_id)
        p2 = Player.query.get(m.player2_id)
        if not p1 or not p2:
            continue
        scores = result_to_scores(m.result) or (1, 1)
        update_elo(p1, p2, *scores, tournament)

    # Mark final and clear token
    tournament.pending = False
    tournament.confirm_token = None

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Failed to finalize tournament {tid}: {e}")
        flash("Error finalizing tournament. Please try again.", "error")
        return redirect(url_for('players'))

    flash("Tournament finalized. Elo updated.", "success")
    return redirect(url_for('players'))





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
            # pass tournament for correct K-factor
            update_elo(player, opponent, games_a, games_b, tournament)

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


@app.route("/tournament/<int:tid>/delete", methods=["POST"])
@login_required
def delete_tournament(tid):
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for("players"))

    tournament = Tournament.query.get_or_404(tid)

    # --- Delete dependent records ---
    Match.query.filter_by(tournament_id=tournament.id).delete()
    Deck.query.filter_by(tournament_id=tournament.id).delete()
    TournamentPlayer.query.filter_by(tournament_id=tournament.id).delete()

    # Delete the tournament itself
    db.session.delete(tournament)
    db.session.commit()

    # --- Reset Elo for all players ---
    for player in Player.query.all():
        player.elo = DEFAULT_ELO
    db.session.commit()

    # --- Recalculate Elo from remaining tournaments ---
    tournaments = (
        Tournament.query
        .filter_by(pending=False)
        .order_by(Tournament.submitted_at.asc())   # <-- use submission timestamp
        .all()
    )

    for t in tournaments:
        matches = (
            Match.query
            .filter_by(tournament_id=t.id)
            .order_by(Match.round_num.asc())
            .all()
        )
        for m in matches:
            if m.player1_id and m.player2_id:
                p1 = Player.query.get(m.player1_id)
                p2 = Player.query.get(m.player2_id)
                scores = result_to_scores(m.result)
                if scores:
                    update_elo(p1, p2, scores[0], scores[1], t)
    db.session.commit()

    flash(f"Tournament '{tournament.name}' deleted. Elo recalculated.", "success")
    return redirect(url_for("tournaments_list"))  # <-- use your actual list view endpoint name




@app.route("/tournament/<int:tid>/edit", methods=["GET", "POST"])
@login_required
def edit_tournament(tid):
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for("tournaments"))

    tournament = Tournament.query.get_or_404(tid)

    if request.method == "POST":
        tournament.name = request.form.get("name", tournament.name)
        tournament.date = request.form.get("date", tournament.date)
        tournament.rounds = request.form.get("rounds", tournament.rounds)
        tournament.player_count = request.form.get("player_count", tournament.player_count)
        tournament.top_cut = request.form.get("top_cut", tournament.top_cut)
        tournament.country = request.form.get("country", tournament.country)
        tournament.casual = bool(request.form.get("casual")) or tournament.casual
        tournament.premium = bool(request.form.get("premium")) or tournament.premium
        db.session.commit()
        flash("Tournament updated.", "success")
        return redirect(url_for("tournaments"))

    return render_template("edit_tournament.html", tournament=tournament)




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
            update_elo(p1, p2, *scores, tournament)
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
@login_required
def remove_deck():
    player_id = request.form.get("player_id")
    tournament_id = request.form.get("tournament_id")

    # 🚨 Validate input
    if not player_id or not tournament_id:
        flash("Missing player or tournament information.", "error")
        return redirect(url_for('players'))

    tournament = Tournament.query.get_or_404(tournament_id)

    # 🚨 Security guard: block removal if tournament is finalized/discarded
    if tournament.imported_from_text and not tournament.pending:
        flash("This tournament has already been finalized or discarded. Decks cannot be removed.", "error")
        return redirect(url_for('players'))

    # === Remove deck if it exists ===
    deck = Deck.query.filter_by(player_id=player_id, tournament_id=tournament_id).first()
    if deck:
        db.session.delete(deck)
        db.session.commit()
        flash("Deck removed.", "success")
    else:
        flash("No deck found for this player in the tournament.", "error")

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