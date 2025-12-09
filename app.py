import os
import re
import json
import logging
import time
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

# Demo mode state file
DEMO_MODE_FILE = os.path.join(DATA_DIR, "demo_mode.txt")

# Check if demo mode is enabled
def is_demo_mode():
    if os.path.exists(DEMO_MODE_FILE):
        with open(DEMO_MODE_FILE, 'r') as f:
            return f.read().strip() == 'true'
    return False

# Get current database paths based on mode
def get_db_paths():
    if is_demo_mode():
        return (
            os.path.join(DATA_DIR, "tournament_demo.db"),
            os.path.join(DATA_DIR, "users_demo.db")
        )
    else:
        return (
            os.path.join(DATA_DIR, "tournament.db"),
            os.path.join(DATA_DIR, "users.db")
        )

DB_PATH, USERS_PATH = get_db_paths()

# --- SQLAlchemy configuration (separate bind for users) ---
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{DB_PATH}"
app.config['SQLALCHEMY_BINDS'] = {
    'users': f"sqlite:///{USERS_PATH}"
}
app.config['SQLALCHEMY_TRACK_NOTIFICATIONS'] = False  # harmless typo-safe line if using older versions
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Log which databases we're using on startup
print(f"[STARTUP] Demo mode: {is_demo_mode()}", file=sys.stderr)
print(f"[STARTUP] Tournament DB: {DB_PATH}", file=sys.stderr)
print(f"[STARTUP] Users DB: {USERS_PATH}", file=sys.stderr)

DEFAULT_ELO = 1000
ELO_DIVISOR = DEFAULT_ELO / 2.5  # Scale divisor based on default elo (400 for 1000, 600 for 1500, etc.)

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
    dark_mode = db.Column(db.Boolean, default=False)
    profile_picture = db.Column(db.String(500))

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
    edit_token = db.Column(db.String(64), nullable=True)  # For editing existing tournaments
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
    
    player1 = db.relationship('Player', foreign_keys=[player1_id])
    player2 = db.relationship('Player', foreign_keys=[player2_id])

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

class DeckSubmissionLink(db.Model):
    __tablename__ = 'deck_submission_links'
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    player_name = db.Column(db.String(120), nullable=False)
    submission_token = db.Column(db.String(64), unique=True, nullable=False)
    deck_submitted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    tournament = db.relationship('Tournament', backref='deck_submission_links')

class CasualPointsHistory(db.Model):
    __tablename__ = 'casual_points_history'
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    store_id = db.Column(db.Integer, db.ForeignKey('stores.id'), nullable=True)
    points = db.Column(db.Integer, nullable=False)
    rank = db.Column(db.Integer, nullable=False)
    awarded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    player = db.relationship('Player', backref='casual_points_history')
    tournament = db.relationship('Tournament', backref='casual_points_awards')
    store = db.relationship('Store', backref='casual_points_awards')

class CasualRankingSnapshot(db.Model):
    __tablename__ = 'casual_ranking_snapshot'
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)
    rank = db.Column(db.Integer, nullable=False)
    points = db.Column(db.Integer, nullable=False)
    snapshot_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    player = db.relationship('Player', backref='casual_ranking_snapshots')

class ArchetypeModel(db.Model):
    __tablename__ = 'archetype_models'
    id = db.Column(db.Integer, primary_key=True)
    archetype_name = db.Column(db.String(120), unique=True, nullable=False)
    model_decklist = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class Store(db.Model):
    __tablename__ = 'stores'   # default bind (tournament.db)
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    location = db.Column(db.String(120))
    country = db.Column(db.String(5))
    premium = db.Column(db.Boolean, default=False)
    image_url = db.Column(db.String(255))
    competitive_tokens = db.Column(db.Integer, default=5)
    premium_tokens = db.Column(db.Integer, default=1)
    last_token_reset = db.Column(db.Date, nullable=True)
    default_competitive_tokens = db.Column(db.Integer, default=5)
    default_premium_tokens = db.Column(db.Integer, default=1)

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


def reset_store_tokens_if_needed(store: Store):
    """Reset tokens if we're in a new month"""
    from datetime import date
    today = date.today()
    
    if not store.last_token_reset or (today.month != store.last_token_reset.month or today.year != store.last_token_reset.year):
        store.competitive_tokens = store.default_competitive_tokens if store.default_competitive_tokens is not None else 5
        store.premium_tokens = (store.default_premium_tokens if store.default_premium_tokens is not None else 1) if store.premium else 0
        store.last_token_reset = today
        db.session.commit()


def get_user_store_tokens(user: User):
    """Get token availability for all user's stores"""
    from datetime import date
    from calendar import monthrange
    
    stores = stores_for_user(user)
    store_data = []
    today = date.today()
    
    # Calculate next reset date (1st of next month)
    if today.day == 1:
        next_reset = today
    else:
        # Get next month
        if today.month == 12:
            next_reset = date(today.year + 1, 1, 1)
        else:
            next_reset = date(today.year, today.month + 1, 1)
    
    for store in stores:
        reset_store_tokens_if_needed(store)
        # Ensure tokens are not None
        competitive_tokens = store.competitive_tokens if store.competitive_tokens is not None else 5
        premium_tokens = store.premium_tokens if store.premium_tokens is not None else (1 if store.premium else 0)
        default_competitive = store.default_competitive_tokens if store.default_competitive_tokens is not None else 5
        default_premium = store.default_premium_tokens if store.default_premium_tokens is not None else 1
        
        store_data.append({
            'id': store.id,
            'name': store.name,
            'premium': store.premium,
            'competitive_tokens': competitive_tokens,
            'premium_tokens': premium_tokens,
            'default_competitive_tokens': default_competitive,
            'default_premium_tokens': default_premium,
            'has_competitive': competitive_tokens > 0,
            'has_premium': store.premium and premium_tokens > 0,
            'next_reset_date': next_reset
        })
    
    return store_data


class BlogPost(db.Model):
    __bind_key__ = 'users'
    __tablename__ = 'blog_posts'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    author_id = db.Column(db.String(255), nullable=False)
    author_name = db.Column(db.String(255), nullable=False)
    image_url = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    @property
    def author(self):
        # Create a simple object with name attribute for template compatibility
        class Author:
            def __init__(self, name):
                self.name = name
        return Author(self.author_name)

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
    return (
        db.session.query(TournamentPlayer)
        .join(Tournament, TournamentPlayer.tournament_id == Tournament.id)
        .filter(TournamentPlayer.player_id == player_id)
        .filter(Tournament.pending == False)
        .count()
    )

def calculate_per_round_elo_changes(tid: int, tournament: Tournament):
    """
    Calculate elo changes per round for each match.
    Returns dict: {round_num: {match_id: {player1_elo_delta, player2_elo_delta, winner}}}
    """
    all_matches = Match.query.filter_by(tournament_id=tid).order_by(Match.round_num, Match.id).all()
    per_round_elo = {}
    
    for match in all_matches:
        round_num = match.round_num
        if round_num not in per_round_elo:
            per_round_elo[round_num] = {}
        
        # Handle bye - no elo change for byes
        if match.result == "bye" and match.player1_id and not match.player2_id:
            per_round_elo[round_num][match.id] = {
                'player1_elo_delta': 0,
                'player2_elo_delta': 0,
                'winner': 1
            }
            continue
        
        # Handle normal match
        if not match.player1_id or not match.player2_id:
            continue
        
        p1 = Player.query.get(match.player1_id)
        p2 = Player.query.get(match.player2_id)
        if not p1 or not p2:
            continue
        
        scores = result_to_scores(match.result) or (1, 1)
        old1, old2 = p1.elo, p2.elo
        update_elo(p1, p2, *scores, tournament)
        elo_change_p1 = int(p1.elo - old1)
        elo_change_p2 = int(p2.elo - old2)
        
        # Determine winner (1=player1, 2=player2, 0=draw)
        winner = 0
        if scores[0] > scores[1]:
            winner = 1
        elif scores[1] > scores[0]:
            winner = 2
        # else: winner stays 0 for draws
        
        per_round_elo[round_num][match.id] = {
            'player1_elo_delta': elo_change_p1,
            'player2_elo_delta': elo_change_p2,
            'winner': winner
        }
        
        p1.elo, p2.elo = old1, old2
    
    return per_round_elo


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
        # Draw: normalize based on expected outcomes
        # Higher rated player loses more points, lower rated gains more
        result_a = result_b = 0.5

    # Expected scores
    expected_a = 1 / (1 + 10 ** ((player_b.elo - player_a.elo) / ELO_DIVISOR))
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
    picture = user_info.get("picture")

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
            existing_by_email.profile_picture = picture
            db.session.commit()
            user = existing_by_email
        else:
            # Create brand new user
            user = User(id=user_id, name=name, email=email, profile_picture=picture)
            db.session.add(user)
            db.session.commit()
    else:
        # Update existing user's profile picture if it changed
        if user.profile_picture != picture:
            user.profile_picture = picture
            db.session.commit()

    login_user(user)
    
    # Hardcode swarped7@gmail.com as admin
    if user.email == "swarped7@gmail.com" and not user.is_admin:
        user.is_admin = True
        db.session.commit()
    
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
                log_event(
                    action_type='access_granted',
                    details=f"Granted scorekeeper access to {req.user.name} ({req.user.email})",
                    recoverable=False
                )
                flash(f"{req.user.email} approved as scorekeeper.", "success")

            elif action == "approve_admin":
                req.user.is_admin = True
                req.reviewed = True
                log_event(
                    action_type='access_granted',
                    details=f"Granted admin access to {req.user.name} ({req.user.email})",
                    recoverable=False
                )
                flash(f"{req.user.email} approved as admin.", "success")

            elif action == "deny_request":
                req.reviewed = True
                flash(f"Request from {req.user.email} denied.", "error")

            db.session.commit()

        elif action == "delete_user_db":
            # Backup users database before deleting
            import shutil
            backup_path = USERS_PATH + '.backup_' + datetime.now().strftime('%Y%m%d_%H%M%S')
            shutil.copy2(USERS_PATH, backup_path)
            
            log_event(
                action_type='database_deleted',
                details=f"Deleted users database. Backup saved to: {os.path.basename(backup_path)}",
                backup_data=backup_path,
                recoverable=True
            )
            
            engine = db.get_engine(app, bind="users")
            db.Model.metadata.drop_all(bind=engine, tables=[User.__table__, AccessRequest.__table__])
            db.Model.metadata.create_all(bind=engine, tables=[User.__table__, AccessRequest.__table__])
            flash("User database has been reset. Backup saved.", "success")

        elif action == "delete_tournament_db":
            # Backup tournament database before deleting
            import shutil
            backup_path = DB_PATH + '.backup_' + datetime.now().strftime('%Y%m%d_%H%M%S')
            shutil.copy2(DB_PATH, backup_path)
            
            # Preserve archetype templates before deletion
            archetype_templates = Deck.query.filter_by(player_id=0, tournament_id=None).all()
            archetype_data = [{
                'name': deck.name,
                'list_text': deck.list_text,
                'colors': deck.colors,
                'image_url': deck.image_url
            } for deck in archetype_templates]
            
            # Preserve archetype models before deletion
            archetype_models = ArchetypeModel.query.all()
            model_data = [{
                'archetype_name': model.archetype_name,
                'model_decklist': model.model_decklist,
                'created_at': model.created_at,
                'updated_at': model.updated_at
            } for model in archetype_models]
            
            log_event(
                action_type='database_deleted',
                details=f"Deleted tournament database. Backup saved to: {os.path.basename(backup_path)}",
                backup_data=backup_path,
                recoverable=True
            )
            
            db.Model.metadata.drop_all(bind=db.engine)
            db.Model.metadata.create_all(bind=db.engine)
            
            # Restore archetype templates
            for archetype in archetype_data:
                new_deck = Deck(
                    name=archetype['name'],
                    list_text=archetype['list_text'],
                    colors=archetype['colors'],
                    image_url=archetype['image_url'],
                    player_id=0,
                    tournament_id=None
                )
                db.session.add(new_deck)
            
            # Restore archetype models
            for model in model_data:
                new_model = ArchetypeModel(
                    archetype_name=model['archetype_name'],
                    model_decklist=model['model_decklist'],
                    created_at=model['created_at'],
                    updated_at=model['updated_at']
                )
                db.session.add(new_model)
            
            db.session.commit()
            
            flash("Tournament database deleted and recreated. Backup saved. Archetypes and models preserved.", "success")

        elif action == "delete_all_players":
            # Backup tournament database before deleting players and tournaments
            import shutil
            backup_path = DB_PATH + '.backup_players_tournaments_' + datetime.now().strftime('%Y%m%d_%H%M%S')
            shutil.copy2(DB_PATH, backup_path)
            
            # Count what will be deleted for logging
            player_count = Player.query.count()
            tournament_count = Tournament.query.count()
            deck_count = Deck.query.filter(Deck.player_id != 0).count()
            match_count = Match.query.count()
            
            # Delete all players, tournaments, and their data but preserve archetypes and models
            # IMPORTANT: Delete in order to avoid cascade issues
            
            # 1. Delete player deck submissions (preserve archetypes: player_id=0, tournament_id=None)
            Deck.query.filter(
                db.and_(
                    Deck.player_id != 0,
                    Deck.tournament_id.isnot(None)
                )
            ).delete(synchronize_session=False)
            
            # 2. Delete deck submission links
            DeckSubmissionLink.query.delete(synchronize_session=False)
            
            # 3. Delete matches
            Match.query.delete(synchronize_session=False)
            
            # 4. Delete casual points history
            CasualPointsHistory.query.delete(synchronize_session=False)
            
            # 5. Delete tournament players
            TournamentPlayer.query.delete(synchronize_session=False)
            
            # 6. Delete tournaments
            Tournament.query.delete(synchronize_session=False)
            
            # 7. Delete players (but not player_id=0 which is used for archetypes)
            Player.query.filter(Player.id != 0).delete(synchronize_session=False)
            
            db.session.commit()
            
            log_event(
                action_type='players_tournaments_deleted',
                details=f"Deleted all players and tournaments. Players: {player_count}, Tournaments: {tournament_count}, Decks: {deck_count}, Matches: {match_count}. Backup saved to: {os.path.basename(backup_path)}",
                backup_data=backup_path,
                recoverable=True
            )
            
            flash("All players and tournaments deleted. Archetypes and models preserved. Backup saved.", "success")

        elif action == "toggle_demo_mode":
            current_mode = is_demo_mode()
            new_mode = not current_mode
            
            # Write new mode to file
            with open(DEMO_MODE_FILE, 'w') as f:
                f.write('true' if new_mode else 'false')
            
            mode_name = "Demo" if new_mode else "Production"
            flash(f"Switched to {mode_name} mode. Please restart the application for changes to take effect.", "success")

        elif action == "restart_server":
            flash("Server is restarting...", "info")
            db.session.commit()  # Ensure any pending changes are saved
            
            # Use a background thread to restart after response is sent
            def restart_app():
                import time
                time.sleep(2)  # Wait for response to be sent
                import os
                import sys
                os.execl(sys.executable, sys.executable, *sys.argv)
            
            import threading
            threading.Thread(target=restart_app, daemon=True).start()
            return redirect(url_for('admin_panel'))

        else:
            # toggle roles
            user_id = request.form.get("user_id")
            user = User.query.get(user_id)
            if user:
                make_admin = request.form.get("make_admin")
                make_scorekeeper = request.form.get("make_scorekeeper")
                if make_admin is not None:
                    old_status = user.is_admin
                    user.is_admin = (make_admin == "true")
                    if old_status != user.is_admin:
                        status = "granted" if user.is_admin else "revoked"
                        log_event(
                            action_type='access_changed',
                            details=f"Admin access {status} for {user.name} ({user.email})",
                            recoverable=False
                        )
                if make_scorekeeper is not None:
                    old_status = user.is_scorekeeper
                    user.is_scorekeeper = (make_scorekeeper == "true")
                    if old_status != user.is_scorekeeper:
                        status = "granted" if user.is_scorekeeper else "revoked"
                        log_event(
                            action_type='access_changed',
                            details=f"Scorekeeper access {status} for {user.name} ({user.email})",
                            recoverable=False
                        )
                db.session.commit()
                flash(f"Updated roles for {user.email}", "success")

        return redirect(url_for("admin_panel"))

    # --- GET request: load data for template ---
    users = User.query.all()
    requests = AccessRequest.query.filter_by(reviewed=False).order_by(AccessRequest.date_submitted.desc()).all()
    stores = Store.query.all()

    # Convert users to JSON-serializable format
    users_json = [{"id": u.id, "name": u.name, "email": u.email} for u in users]

    # Query event logs (most recent 100)
    event_logs = EventLog.query.order_by(EventLog.timestamp.desc()).limit(100).all()
    
    # Calculate next reset date
    from datetime import date
    today = date.today()
    if today.day == 1:
        next_reset = today
    else:
        if today.month == 12:
            next_reset = date(today.year + 1, 1, 1)
        else:
            next_reset = date(today.year, today.month + 1, 1)
    
    return render_template("admin.html", users=users, users_json=users_json, requests=requests, stores=stores, demo_mode=is_demo_mode(), event_logs=event_logs, next_reset_date=next_reset)


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
                
                log_event(
                    action_type='store_created',
                    details=f"Created store: {store_name} (Country: {country}, Location: {location})",
                    recoverable=False
                )
                
                flash(f"Store '{store_name}' created.", "success")

        return redirect(url_for("admin_stores"))

    # GET request: show stores and users
    stores = Store.query.all()
    users = User.query.all()
    requests = AccessRequest.query.filter_by(reviewed=False).order_by(AccessRequest.date_submitted.desc()).all()
    
    # Convert users to JSON-serializable format
    users_json = [{"id": u.id, "name": u.name, "email": u.email} for u in users]
    
    # Query event logs (most recent 100)
    event_logs = EventLog.query.order_by(EventLog.timestamp.desc()).limit(100).all()
    
    # Calculate next reset date
    from datetime import date
    today = date.today()
    if today.day == 1:
        next_reset = today
    else:
        if today.month == 12:
            next_reset = date(today.year + 1, 1, 1)
        else:
            next_reset = date(today.year, today.month + 1, 1)
    
    return render_template("admin.html", users=users, users_json=users_json, requests=requests, stores=stores, demo_mode=is_demo_mode(), event_logs=event_logs, next_reset_date=next_reset)


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

@app.route("/store/<int:store_id>/assign_user", methods=["POST"])
@login_required
def assign_user_to_store_ajax(store_id):
    if not current_user.is_admin:
        return jsonify({"error": "Admins only"}), 403

    user_id = request.form.get("user_id")
    if not user_id:
        return jsonify({"error": "User ID is required"}), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    existing = StoreAssignment.query.filter_by(store_id=store_id, user_id=user_id).first()
    if existing:
        return jsonify({"error": "User is already assigned to this store"}), 400

    assignment = StoreAssignment(store_id=store_id, user_id=user_id)
    db.session.add(assignment)
    db.session.commit()
    
    return jsonify({"success": True, "message": f"User {user.email} assigned to store"}), 200




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

@app.route("/admin/store/<int:store_id>/update_tokens", methods=["POST"])
@login_required
def update_store_tokens(store_id):
    if not current_user.is_admin:
        return jsonify({"error": "Admins only"}), 403

    store = Store.query.get_or_404(store_id)
    
    try:
        competitive_tokens = request.form.get("competitive_tokens", type=int)
        premium_tokens = request.form.get("premium_tokens", type=int)
        default_competitive_tokens = request.form.get("default_competitive_tokens", type=int)
        default_premium_tokens = request.form.get("default_premium_tokens", type=int)
        
        if competitive_tokens is not None and competitive_tokens >= 0:
            store.competitive_tokens = competitive_tokens
        if premium_tokens is not None and premium_tokens >= 0:
            store.premium_tokens = premium_tokens
        if default_competitive_tokens is not None and default_competitive_tokens >= 0:
            store.default_competitive_tokens = default_competitive_tokens
        if default_premium_tokens is not None and default_premium_tokens >= 0:
            store.default_premium_tokens = default_premium_tokens
        
        db.session.commit()
        
        log_event(
            action_type='store_tokens_updated',
            details=f"Updated tokens for {store.name}: Competitive={store.competitive_tokens}, Premium={store.premium_tokens}, Defaults: Competitive={store.default_competitive_tokens}, Premium={store.default_premium_tokens}",
            recoverable=False
        )
        
        return jsonify({
            "success": True,
            "competitive_tokens": store.competitive_tokens,
            "premium_tokens": store.premium_tokens,
            "default_competitive_tokens": store.default_competitive_tokens,
            "default_premium_tokens": store.default_premium_tokens
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400

@app.route("/admin/store/<int:store_id>/delete", methods=["POST"])
@login_required
def delete_store(store_id):
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for("admin_stores"))

    store = Store.query.get_or_404(store_id)
    store_name = store.name
    
    # Create backup data for recovery
    import json
    backup_data = {
        'store': {
            'id': store.id,
            'name': store.name,
            'location': store.location,
            'country': store.country,
            'premium': store.premium,
            'image_url': store.image_url
        },
        'assignments': [{'user_id': a.user_id} for a in store.assignments],
        'tournament_ids': []
    }
    
    # Update all tournaments that reference this store to have blank fields
    tournaments_affected = Tournament.query.filter_by(store_id=store_id).all()
    for tournament in tournaments_affected:
        backup_data['tournament_ids'].append(tournament.id)
        tournament.store_id = None
    
    # Delete all store assignments
    StoreAssignment.query.filter_by(store_id=store_id).delete()
    
    # Delete the store
    db.session.delete(store)
    db.session.commit()
    
    # Log the deletion with backup data
    log_event(
        action_type='store_deleted',
        details=f"Deleted store: {store_name} (affected {len(tournaments_affected)} tournament(s))",
        backup_data=json.dumps(backup_data),
        recoverable=True
    )
    
    flash(f"Store '{store_name}' deleted successfully. {len(tournaments_affected)} tournament(s) updated.", "success")
    return redirect(url_for("admin_stores"))


# --- Blog Routes ---
@app.route("/blog", methods=["GET", "POST"])
def blog():
    if request.method == "POST":
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Only admins can create blog posts.", "error")
            return redirect(url_for("blog"))
        
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        image_url = request.form.get("image_url", "").strip()
        
        if not title or not content:
            flash("Title and content are required.", "error")
            return redirect(url_for("blog"))
        
        post = BlogPost(
            title=title,
            content=content,
            author_id=current_user.id,
            author_name=current_user.name,
            image_url=image_url if image_url else None
        )
        db.session.add(post)
        db.session.commit()
        
        flash("Blog post published successfully!", "success")
        return redirect(url_for("blog"))
    
    # GET request - show all posts
    posts = BlogPost.query.order_by(BlogPost.created_at.desc()).all()
    
    # Debug: Log image URLs
    for post in posts:
        print(f"[DEBUG] Post '{post.title}' has image_url: {post.image_url}", file=sys.stderr)
    
    return render_template("blog.html", posts=posts)


@app.route("/blog/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_blog_post(post_id):
    if not current_user.is_admin:
        flash("Only admins can delete blog posts.", "error")
        return redirect(url_for("blog"))
    
    post = BlogPost.query.get_or_404(post_id)
    db.session.delete(post)
    db.session.commit()
    
    flash("Blog post deleted successfully.", "success")
    return redirect(url_for("blog"))


@app.route("/blog/upload_image", methods=["POST"])
@login_required
def upload_blog_image():
    if not current_user.is_admin:
        return jsonify({"error": "Only admins can upload images"}), 403
    
    file = request.files.get("image")
    if not file or file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    
    # Generate unique filename
    import uuid
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in ["jpg", "jpeg", "png"]:
        return jsonify({"error": "Invalid file type"}), 400
    
    filename = f"blog_{uuid.uuid4().hex}.{ext}"
    upload_folder = os.path.join(app.root_path, "static", "uploads")
    os.makedirs(upload_folder, exist_ok=True)
    
    filepath = os.path.join(upload_folder, filename)
    file.save(filepath)
    
    image_url = f"uploads/{filename}"
    return jsonify({"success": True, "image_url": image_url})


@app.route("/store/<int:store_id>/edit_image", methods=["POST"])
@login_required
def edit_store_image(store_id):
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for("admin_stores"))

    store = Store.query.get_or_404(store_id)
    
    # Check if selecting an existing image
    existing_image = request.form.get('existing_image')
    if existing_image:
        store.image_url = existing_image
        db.session.commit()
        flash("Store image updated.", "success")
        return redirect(url_for("admin_stores"))
    
    # Otherwise, handle file upload (already cropped by frontend)
    file = request.files.get("image")
    if not file or file.filename == "":
        flash("No file selected", "error")
        return redirect(url_for("admin_stores"))

    # Generate unique filename
    timestamp = int(time.time())
    filename = f"store_{store_id}_{timestamp}.jpg"
    upload_dir = os.path.join(app.static_folder, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, filename)

    # Save the already-cropped image (512x256 from frontend)
    img = Image.open(file)
    img = img.convert("RGB")
    # Ensure it's exactly 512x256
    img = img.resize((512, 256), Image.Resampling.LANCZOS)
    img.save(path, "JPEG", quality=90)

    store.image_url = f"uploads/{filename}"
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
        reason = request.form.get("reason", "").strip()
        store_name = request.form.get("store_name", "").strip()
        store_country = request.form.get("store_country", "").strip()
        
        if not reason:
            flash("Please provide a reason for your request.", "error")
            return redirect(url_for("request_access"))
        
        if not store_name:
            flash("Please provide a store name.", "error")
            return redirect(url_for("request_access"))
        
        # Check if user already has a pending request
        existing = AccessRequest.query.filter_by(user_id=current_user.id, reviewed=False).first()
        if existing:
            flash("You already have a pending access request.", "info")
            return redirect(url_for("players"))
        
        # Handle optional user image upload
        user_image_url = None
        user_image_file = request.files.get("user_image")
        if user_image_file and user_image_file.filename:
            timestamp = int(time.time())
            filename = f"request_{current_user.id}_{timestamp}.jpg"
            upload_dir = os.path.join(app.static_folder, 'uploads')
            os.makedirs(upload_dir, exist_ok=True)
            path = os.path.join(upload_dir, filename)
            
            img = Image.open(user_image_file)
            img = img.convert("RGB")
            img.thumbnail((800, 800))  # Resize large images
            img.save(path, "JPEG", quality=90)
            user_image_url = f'uploads/{filename}'
        
        # Handle store image upload (required)
        store_image_url = None
        store_image_file = request.files.get("store_image")
        if not store_image_file or not store_image_file.filename:
            flash("Please provide a store image.", "error")
            return redirect(url_for("request_access"))
        
        timestamp = int(time.time())
        filename = f"store_request_{current_user.id}_{timestamp}.jpg"
        upload_dir = os.path.join(app.static_folder, 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        path = os.path.join(upload_dir, filename)
        
        img = Image.open(store_image_file)
        img = img.convert("RGB")
        img = img.resize((512, 256), Image.Resampling.LANCZOS)
        img.save(path, "JPEG", quality=90)
        store_image_url = f'uploads/{filename}'
        
        # Create new access request
        access_request = AccessRequest(
            user_id=current_user.id,
            reason=reason,
            reviewed=False,
            image_url=user_image_url,
            store_name=store_name,
            store_country=store_country,
            store_image_url=store_image_url
        )
        db.session.add(access_request)
        db.session.commit()
        
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
    image_url = db.Column(db.String(255))  # Optional image attachment
    store_name = db.Column(db.String(255))  # Proposed store name
    store_country = db.Column(db.String(5))  # Proposed store country
    store_image_url = db.Column(db.String(255))  # Proposed store image

    user = db.relationship('User', backref='access_requests')


class EventLog(db.Model):
    __bind_key__ = 'users'    # store in users database
    __tablename__ = 'event_logs'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    user_id = db.Column(db.String(255), db.ForeignKey('users.id'), nullable=False)
    user_name = db.Column(db.String(255), nullable=False)  # Snapshot of user's name
    action_type = db.Column(db.String(100), nullable=False)  # e.g., 'tournament_created', 'tournament_deleted'
    details = db.Column(db.Text)  # JSON or text description of the action
    backup_data = db.Column(db.Text)  # JSON backup for recoverable actions
    recoverable = db.Column(db.Boolean, default=False)
    recovered = db.Column(db.Boolean, default=False)
    
    user = db.relationship('User', backref='event_logs')


# Helper function to log events
def log_event(action_type, details, backup_data=None, recoverable=False):
    """Log an event to the event log database."""
    if not current_user.is_authenticated:
        return
    
    event = EventLog(
        user_id=current_user.id,
        user_name=current_user.name,
        action_type=action_type,
        details=details,
        backup_data=backup_data,
        recoverable=recoverable
    )
    db.session.add(event)
    db.session.commit()


@app.route("/admin/access_requests")
@login_required
def access_requests():
    if not current_user.is_admin:
        flash("You do not have permission to view access requests.", "error")
        return redirect(url_for("players"))

    requests = AccessRequest.query.order_by(AccessRequest.date_submitted.desc()).all()
    return render_template("access_requests.html", requests=requests)


@app.route("/admin/event_logs")
@login_required
def event_logs():
    if not current_user.is_admin:
        flash("You do not have permission to view event logs.", "error")
        return redirect(url_for("players"))
    
    # Create test event if none exist (for debugging)
    if EventLog.query.count() == 0:
        test_event = EventLog(
            user_id=current_user.id,
            user_name=current_user.name,
            action_type='system_test',
            details='Test event created to verify logging system is working',
            recoverable=False
        )
        db.session.add(test_event)
        db.session.commit()
    
    logs = EventLog.query.order_by(EventLog.timestamp.desc()).limit(500).all()
    return render_template("event_logs.html", logs=logs)


@app.route("/admin/recover_event/<int:event_id>", methods=["POST"])
@login_required
def recover_event(event_id):
    if not current_user.is_admin:
        flash("Admin access required", "error")
        return redirect(url_for("admin_panel"))
    
    event = EventLog.query.get_or_404(event_id)
    
    if not event.recoverable or event.recovered:
        flash("This event cannot be recovered or has already been recovered.", "error")
        return redirect(url_for("admin_panel"))
    
    import json
    
    try:
        if event.action_type == 'tournament_deleted':
            # Recover deleted tournament
            backup = json.loads(event.backup_data)
            from datetime import date
            
            tournament = Tournament(
                name=backup['tournament']['name'],
                date=date.fromisoformat(backup['tournament']['date']) if backup['tournament']['date'] else datetime.today().date(),
                rounds=backup['tournament']['rounds'],
                player_count=backup['tournament']['player_count'],
                country=backup['tournament']['country'],
                casual=backup['tournament']['casual'],
                premium=backup['tournament']['premium'],
                store_id=backup['tournament']['store_id'],
                top_cut=backup['tournament']['top_cut'],
                pending=False
            )
            db.session.add(tournament)
            db.session.flush()
            
            # Restore tournament players
            for player_id in backup['players']:
                tp = TournamentPlayer(tournament_id=tournament.id, player_id=player_id)
                db.session.add(tp)
            
            # Restore matches
            for match_data in backup['matches']:
                match = Match(
                    tournament_id=tournament.id,
                    round_num=match_data['round_num'],
                    player1_id=match_data['player1_id'],
                    player2_id=match_data['player2_id'],
                    result=match_data['result']
                )
                db.session.add(match)
            
            # Restore decks
            for deck_data in backup['decks']:
                deck = Deck(
                    tournament_id=tournament.id,
                    player_id=deck_data['player_id'],
                    name=deck_data['name'],
                    list_text=deck_data['list_text'],
                    colors=deck_data['colors'],
                    image_url=deck_data['image_url']
                )
                db.session.add(deck)
            
            event.recovered = True
            db.session.commit()
            
            log_event(
                action_type='tournament_recovered',
                details=f"Recovered tournament: {tournament.name} (Event Log ID: {event_id})",
                recoverable=False
            )
            
            flash(f"Tournament '{tournament.name}' has been recovered successfully!", "success")
            
        elif event.action_type == 'database_deleted':
            # Recover from database backup
            backup_path = event.backup_data
            import shutil
            
            if 'users' in backup_path:
                shutil.copy2(backup_path, USERS_PATH)
                flash("Users database has been restored from backup!", "success")
            else:
                shutil.copy2(backup_path, DB_PATH)
                flash("Tournament database has been restored from backup!", "success")
            
            event.recovered = True
            db.session.commit()
            
            log_event(
                action_type='database_recovered',
                details=f"Recovered database from backup: {backup_path}",
                recoverable=False
            )
            
            flash("Database restored. Please restart the server for changes to take effect.", "warning")
        
        elif event.action_type == 'store_deleted':
            # Recover deleted store
            backup = json.loads(event.backup_data)
            
            # Recreate the store
            store = Store(
                name=backup['store']['name'],
                location=backup['store']['location'],
                country=backup['store']['country'],
                premium=backup['store']['premium'],
                image_url=backup['store']['image_url']
            )
            db.session.add(store)
            db.session.flush()
            
            # Restore store assignments
            for assignment_data in backup['assignments']:
                assignment = StoreAssignment(store_id=store.id, user_id=assignment_data['user_id'])
                db.session.add(assignment)
            
            # Restore tournament associations
            for tournament_id in backup['tournament_ids']:
                tournament = Tournament.query.get(tournament_id)
                if tournament:
                    tournament.store_id = store.id
            
            event.recovered = True
            db.session.commit()
            
            log_event(
                action_type='store_recovered',
                details=f"Recovered store: {store.name} (Event Log ID: {event_id})",
                recoverable=False
            )
            
            flash(f"Store '{store.name}' has been recovered successfully!", "success")
        
        else:
            flash("Unknown recovery type.", "error")
            
    except Exception as e:
        db.session.rollback()
        flash(f"Error during recovery: {str(e)}", "error")
    
    return redirect(url_for("admin_panel"))


@app.route('/get_available_images', methods=['GET'])
@login_required
def get_available_images():
    """Return a JSON list of all available images in the uploads folder."""
    upload_dir = os.path.join(app.static_folder, 'uploads')
    if not os.path.exists(upload_dir):
        return jsonify([])
    
    images = []
    for filename in os.listdir(upload_dir):
        if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
            images.append(f'uploads/{filename}')
    
    return jsonify(sorted(images))

@app.route('/archetype/<name>/edit', methods=['GET','POST'])
@login_required
def edit_archetype(name):
    if not current_user.is_admin:
        flash("Admins only", "error")
        return redirect(url_for('decks_list'))

    if request.method == 'POST':
        # Check if selecting an existing image
        existing_image = request.form.get('existing_image')
        if existing_image:
            # Assign existing image to archetype
            deck = Deck.query.filter_by(name=name).order_by(Deck.id.desc()).first()
            if deck:
                deck.image_url = existing_image
                db.session.commit()
                flash("Image updated", "success")
            else:
                flash("No deck found for archetype", "error")
            return redirect(url_for('decks_list'))
        
        # Otherwise, handle file upload (already cropped by frontend)
        file = request.files.get('image')
        if not file or file.filename == '':
            flash("No file selected", "error")
            return redirect(url_for('decks_list'))

        # Generate unique filename
        timestamp = int(time.time())
        filename = f"{secure_filename(name)}_{timestamp}.jpg"
        upload_dir = os.path.join(app.static_folder, 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        path = os.path.join(upload_dir, filename)

        # Save the already-cropped image (512x256 from frontend)
        img = Image.open(file)
        img = img.convert("RGB")
        # Ensure it's exactly 512x256
        img = img.resize((512, 256), Image.Resampling.LANCZOS)
        img.save(path, "JPEG", quality=90)

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
        dark_mode = request.form.get("dark_mode") == "on"
        
        if new_name:
            current_user.name = new_name
        current_user.dark_mode = dark_mode
        db.session.commit()
        flash("Profile updated successfully!", "success")

    # 👇 Collect the stores assigned to this user
    stores = Store.query.filter(
        Store.id.in_([a.store_id for a in current_user.store_assignments])
    ).all()

    # Pass `stores` into the template
    return render_template("profile_settings.html", stores=stores)


@app.route('/toggle_dark_mode', methods=['POST'])
@login_required
def toggle_dark_mode():
    current_user.dark_mode = not current_user.dark_mode
    db.session.commit()
    return jsonify({"success": True, "dark_mode": current_user.dark_mode})


@app.route("/user/<user_id>/tournaments")
@login_required
def user_tournaments(user_id):
    tournaments = Tournament.query.filter_by(user_id=user_id, pending=False).order_by(Tournament.date.desc()).all()
    return render_template("user_tournaments.html", tournaments=tournaments)





@app.route('/players', methods=['GET', 'POST'])
def players():
    # Clean up expired pending tournaments (older than 24 hours)
    cleanup_expired_tournaments()
    
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

    # --- Optional country and store filters ---
    country_filter = request.args.get("country")
    store_filter = request.args.get("store")

    # Helper: compute a player's "home country" based on tournaments played
    def player_country_local(player_id):
        tps = TournamentPlayer.query.filter_by(player_id=player_id).all()
        countries = []
        for tp in tps:
            tournament = Tournament.query.filter_by(id=tp.tournament_id, pending=False).first()
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

    # Helper: check if player played at a specific store
    def player_played_at_store(player_id, store_id):
        tps = TournamentPlayer.query.filter_by(player_id=player_id).all()
        for tp in tps:
            tournament = Tournament.query.filter_by(id=tp.tournament_id, pending=False).first()
            if tournament and tournament.store_id == int(store_id):
                return True
        return False
    
    # Helper: get all store IDs where player has played
    def player_stores(player_id):
        tps = TournamentPlayer.query.filter_by(player_id=player_id).all()
        store_ids = set()
        for tp in tps:
            tournament = Tournament.query.filter_by(id=tp.tournament_id, pending=False).first()
            if tournament and tournament.store_id:
                store_ids.add(tournament.store_id)
        return list(store_ids)
    
    # Helper: calculate casual points for a player at a specific store
    def player_casual_points_at_store(player_id, store_id):
        # Sum all casual points earned at this store from history
        total_points = db.session.query(func.sum(CasualPointsHistory.points)).filter(
            CasualPointsHistory.player_id == player_id,
            CasualPointsHistory.store_id == int(store_id)
        ).scalar() or 0
        
        print(f"DEBUG: Player {player_id} has {total_points} casual points at store {store_id} (from history)", file=sys.stderr)
        return total_points
    
    # Helper: get points breakdown by store for a player
    def player_points_by_store(player_id):
        # Get all points grouped by store
        results = db.session.query(
            CasualPointsHistory.store_id,
            func.sum(CasualPointsHistory.points).label('total_points')
        ).filter(
            CasualPointsHistory.player_id == player_id
        ).group_by(CasualPointsHistory.store_id).all()
        
        breakdown = {}
        for store_id, total_points in results:
            if store_id:
                store = Store.query.get(store_id)
                breakdown[store_id] = {
                    'store_name': store.name if store else f'Store {store_id}',
                    'points': int(total_points)
                }
        return breakdown
    
    # Helper: get previous rank for a player
    def get_previous_rank(player_id):
        # Get the second most recent snapshot for this player (skip the current one)
        snapshots = CasualRankingSnapshot.query.filter_by(
            player_id=player_id
        ).order_by(CasualRankingSnapshot.snapshot_date.desc()).limit(2).all()
        
        # Return the second snapshot's rank if it exists
        return snapshots[1].rank if len(snapshots) > 1 else None
    
    # Helper: update ranking snapshots
    def update_ranking_snapshots(casual_ranked):
        # Keep the last 3 snapshots per player for rank change tracking
        for player_rank in casual_ranked:
            player_id = player_rank['player'].id
            
            # Delete all but the most recent 2 snapshots (we'll add a new one, making it 3 total)
            old_snapshots = CasualRankingSnapshot.query.filter_by(
                player_id=player_id
            ).order_by(CasualRankingSnapshot.snapshot_date.desc()).offset(2).all()
            
            for snapshot in old_snapshots:
                db.session.delete(snapshot)
            
            # Create new snapshot
            new_snapshot = CasualRankingSnapshot(
                player_id=player_id,
                rank=player_rank['rank'],
                points=player_rank['points']
            )
            db.session.add(new_snapshot)
        
        db.session.commit()

    # === Competitive ranking (Elo) ===
    all_competitive_players = (
        db.session.query(Player)
        .join(TournamentPlayer, Player.id == TournamentPlayer.player_id)
        .join(Tournament, Tournament.id == TournamentPlayer.tournament_id)
        .filter(Tournament.pending == False)   # 🔒 only finalized tournaments
        .order_by(Player.elo.desc())
        .all()
    )

    # Get unique countries from all competitive players (before filtering)
    available_countries = set()
    for p in all_competitive_players:
        country = player_country_local(p.id)
        if country:
            available_countries.add(country.upper())
    available_countries = sorted(list(available_countries))

    if country_filter:
        all_competitive_players = [
            p for p in all_competitive_players
            if player_country_local(p.id) and player_country_local(p.id).upper() == country_filter.upper()
        ]
    
    if store_filter:
        all_competitive_players = [
            p for p in all_competitive_players
            if player_played_at_store(p.id, store_filter)
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
    if store_filter:
        # When filtering by store, get players directly from history table
        player_ids_with_points = (
            db.session.query(CasualPointsHistory.player_id)
            .filter(CasualPointsHistory.store_id == int(store_filter))
            .distinct()
            .all()
        )
        player_ids = [pid[0] for pid in player_ids_with_points]
        casual_players = Player.query.filter(Player.id.in_(player_ids)).all() if player_ids else []
        
        if country_filter:
            casual_players = [
                p for p in casual_players
                if player_country_local(p.id) and player_country_local(p.id).upper() == country_filter.upper()
            ]
    else:
        # No store filter - get all players who played casual tournaments
        casual_players = (
            db.session.query(Player)
            .join(TournamentPlayer, Player.id == TournamentPlayer.player_id)
            .join(Tournament, Tournament.id == TournamentPlayer.tournament_id)
            .filter(Tournament.pending == False)   # 🔒 only finalized tournaments
            .filter(Tournament.casual == True)  # Only casual tournaments
            .distinct()
            .all()
        )
        
        if country_filter:
            casual_players = [
                p for p in casual_players
                if player_country_local(p.id) and player_country_local(p.id).upper() == country_filter.upper()
            ]
    
    # Calculate points (dynamic if store filter is active, otherwise use stored total)
    casual_players_with_points = []
    for p in casual_players:
        if store_filter:
            points = player_casual_points_at_store(p.id, store_filter)
            print(f"DEBUG: Filtering by store {store_filter} - Player {p.id} ({p.name}) has {points} points", file=sys.stderr)
            # Only include players who have points at this store
            if points > 0:
                casual_players_with_points.append((p, points))
        else:
            points = p.casual_points
            casual_players_with_points.append((p, points))
    
    # Sort by points descending
    casual_players_with_points.sort(key=lambda x: x[1], reverse=True)
    
    # Create ranked list with dynamic points
    casual_ranked = []
    for idx, (player, points) in enumerate(casual_players_with_points, start=1):
        # Get points breakdown by store for debug display
        points_by_store = player_points_by_store(player.id)
        
        # Get previous rank for rank change indicator
        previous_rank = get_previous_rank(player.id)
        rank_change = None
        if previous_rank is not None:
            rank_change = previous_rank - idx  # Positive = moved up, negative = moved down
        
        casual_ranked.append({
            "player": player,
            "rank": idx,
            "points": points,  # Dynamic points based on filter
            "points_by_store": points_by_store,  # Debug info
            "rank_change": rank_change  # For trend arrows
        })
    
    # Update snapshots with current rankings (only when not filtering by store)
    if not store_filter:
        update_ranking_snapshots(casual_ranked)

    # === Top 4 archetypes by number of decks ===
    top_archetypes = (
        db.session.query(Deck.name, func.count(Deck.id).label("count"))
        .join(Tournament, Deck.tournament_id == Tournament.id)
        .filter(Deck.name.isnot(None))
        .filter(Tournament.pending == False)
        .group_by(Deck.name)
        .order_by(func.count(Deck.id).desc())
        .limit(4)
        .all()
    )
    # Calculate meta share for top decks
    total_all_decks = Deck.query.count()
    top_decks = []
    for name, deck_count in top_archetypes:
        last_deck = Deck.query.filter_by(name=name).order_by(Deck.id.desc()).first()
        meta_share = round((deck_count / total_all_decks * 100), 1) if total_all_decks > 0 else 0
        top_decks.append({
            "name": name,
            "image_url": last_deck.image_url if last_deck and last_deck.image_url else "",
            "meta_share": meta_share
        })

    # === Top 3 stores by number of tournaments ===
    top_stores_data = (
        db.session.query(Store, func.count(Tournament.id).label("tournament_count"))
        .join(Tournament, Tournament.store_id == Store.id)
        .filter(Tournament.pending == False)
        .group_by(Store.id)
        .order_by(func.count(Tournament.id).desc())
        .limit(3)
        .all()
    )
    top_stores = [{"store": store, "count": count} for store, count in top_stores_data]

    # Get latest 3 blog posts for featured section
    featured_posts = BlogPost.query.order_by(BlogPost.created_at.desc()).limit(3).all()

    # Get all stores grouped by country for filtering
    stores_by_country = {}
    all_stores_query = (
        db.session.query(Store)
        .join(Tournament, Tournament.store_id == Store.id)
        .filter(Tournament.pending == False)
        .distinct()
        .order_by(Store.country, Store.name)
        .all()
    )
    for store in all_stores_query:
        country = store.country or 'Unknown'
        if country not in stores_by_country:
            stores_by_country[country] = []
        # Convert store to dict for JSON serialization
        stores_by_country[country].append({
            'id': store.id,
            'name': store.name,
            'country': store.country
        })

    # === Render template ===
    return render_template(
        'players.html',
        players=competitive_ranked,       # Competitive Elo (with hidden/unranked logic)
        casual_players=casual_ranked,     # Casual Ranking
        top_decks=top_decks,
        top_stores=top_stores,
        player_country=player_country,
        tournaments_played=tournaments_played,
        featured_posts=featured_posts,
        available_countries=available_countries,
        stores_by_country=stores_by_country,
        player_stores=player_stores
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
    print(f"DEBUG: Found {len(decks)} total decks in database")
    archetypes = {}
    for d in decks:
        archetypes.setdefault(d.name, []).append(d)
    
    # Add archetypes from ArchetypeModel that don't have any decks yet
    # Create placeholder deck entries for them
    models = ArchetypeModel.query.all()
    for model in models:
        if model.archetype_name not in archetypes:
            # Check if a placeholder deck already exists
            placeholder = Deck.query.filter_by(
                name=model.archetype_name,
                player_id=0,
                tournament_id=None
            ).first()
            
            if not placeholder:
                # Create a placeholder deck with the model decklist
                placeholder = Deck(
                    name=model.archetype_name,
                    list_text=model.model_decklist,
                    colors="",
                    image_url=None,
                    player_id=0,
                    tournament_id=None
                )
                db.session.add(placeholder)
                db.session.commit()
                print(f"DEBUG: Created placeholder deck for '{model.archetype_name}'")
            
            archetypes[model.archetype_name] = [placeholder]
            print(f"DEBUG: Added empty archetype '{model.archetype_name}' from model")
    
    print(f"DEBUG: Found {len(archetypes)} archetypes (including empty ones)")

    # Calculate archetype metrics for sorting and tier assignment
    archetype_stats = []
    for name, deck_list in archetypes.items():
        deck_count = len(deck_list)
        
        # Calculate average rank (lower is better)
        ranks = []
        for d in deck_list:
            player = Player.query.get(d.player_id) if d.player_id else None
            tournament = Tournament.query.get(d.tournament_id) if d.tournament_id else None
            
            if player and tournament:
                matches = Match.query.filter_by(tournament_id=tournament.id).all()
                players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id) \
                                      .filter(TournamentPlayer.tournament_id == tournament.id).all()
                
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
                        "player_id": p.id,
                        "points": points,
                        "wins": wins
                    })
                
                standings.sort(key=lambda s: (s["points"], s["wins"]), reverse=True)
                
                for idx, s in enumerate(standings):
                    if s["player_id"] == player.id:
                        ranks.append(idx + 1)
                        break
        
        avg_rank = sum(ranks) / len(ranks) if ranks else 999
        
        archetype_stats.append({
            "name": name,
            "deck_count": deck_count,
            "avg_rank": avg_rank
        })
    
    # Sort by deck count (desc), then by avg rank (asc)
    archetype_stats.sort(key=lambda x: (-x["deck_count"], x["avg_rank"]))
    
    # Assign tiers
    total_archetypes = len(archetype_stats)
    tier_1_cutoff = int(total_archetypes * 0.20)
    tier_2_cutoff = int(total_archetypes * 0.50)
    
    archetype_tiers = {}
    for idx, stat in enumerate(archetype_stats):
        if stat["name"] == "Rogue":
            archetype_tiers[stat["name"]] = "Rogue"
        elif idx < tier_1_cutoff:
            archetype_tiers[stat["name"]] = "Tier 1"
        elif idx < tier_2_cutoff:
            archetype_tiers[stat["name"]] = "Tier 2"
        else:
            archetype_tiers[stat["name"]] = "Tier 3"

    archetype_colors = {}
    for name, deck_list in archetypes.items():
        sorted_decks = sorted(deck_list, key=lambda d: d.id, reverse=True)
        recent_decks = sorted_decks[:5] if len(sorted_decks) >= 5 else sorted_decks
        
        if len(recent_decks) < 5:
            recent_decks = sorted_decks
        
        print(f"DEBUG: Archetype '{name}' has {len(recent_decks)} recent decks")
        for deck in recent_decks:
            print(f"  Deck ID {deck.id}: colors='{deck.colors}', has_list_text={bool(deck.list_text and deck.list_text.strip())}")
        
        color_counter = {}
        for deck in recent_decks:
            if deck.colors and deck.colors.strip() and deck.colors not in ['None', 'none', 'null']:
                colors_set = set(deck.colors)
                color_key = ''.join([c for c in 'WUBRG' if c in colors_set])
                color_counter[color_key] = color_counter.get(color_key, 0) + 1
        
        print(f"  Color counter: {color_counter}")
        
        if color_counter:
            most_common = max(color_counter.items(), key=lambda x: x[1])[0]
            archetype_colors[name] = most_common
            print(f"  Result: {most_common}")
        else:
            archetype_colors[name] = ""
            print(f"  Result: empty (no colors found)")

    # Re-order archetypes dict by sorted order, with "Rogue" always at the bottom
    rogue_decks = None
    sorted_archetypes = {}
    for stat in archetype_stats:
        if stat["name"] == "Rogue":
            rogue_decks = archetypes[stat["name"]]
        else:
            sorted_archetypes[stat["name"]] = archetypes[stat["name"]]
    
    # Add empty archetypes that weren't in archetype_stats (no decks yet)
    for name, deck_list in archetypes.items():
        if name not in sorted_archetypes and name != "Rogue":
            # Add empty archetype, create a placeholder deck for template
            if len(deck_list) == 0:
                # Create a placeholder deck object for display purposes only
                placeholder = type('obj', (object,), {
                    'id': 0,
                    'name': name,
                    'list_text': '',
                    'colors': '',
                    'image_url': None,
                    'player_id': 0,
                    'tournament_id': None
                })()
                sorted_archetypes[name] = [placeholder]
            else:
                sorted_archetypes[name] = deck_list
            # Assign default tier for empty archetypes
            archetype_tiers[name] = "Tier 3"
            archetype_colors[name] = ""
    
    # Add Rogue at the end if it exists
    if rogue_decks is not None:
        sorted_archetypes["Rogue"] = rogue_decks

    return render_template("decks.html", archetypes=sorted_archetypes, archetype_colors=archetype_colors, archetype_tiers=archetype_tiers)


@app.route('/decks/recalculate', methods=['POST'])
@login_required
def recalculate_archetypes():
    """Recalculate all deck archetypes based on similarity to model decklists"""
    if not current_user.is_admin:
        return jsonify({"error": "Admin access required"}), 403
    
    try:
        # First, delete all decks with empty decklists (placeholder/empty submissions)
        # BUT preserve archetype templates (player_id=0, tournament_id=None)
        empty_decks = Deck.query.filter(
            db.or_(
                Deck.list_text.is_(None),
                Deck.list_text == ''
            ),
            db.not_(db.and_(Deck.player_id == 0, Deck.tournament_id.is_(None)))
        ).all()
        
        deleted_count = 0
        deleted_log = []
        
        for empty_deck in empty_decks:
            player = Player.query.get(empty_deck.player_id) if empty_deck.player_id else None
            tournament = Tournament.query.get(empty_deck.tournament_id) if empty_deck.tournament_id else None
            
            deleted_detail = {
                'deck_id': empty_deck.id,
                'player': player.name if player else 'Unknown',
                'tournament': tournament.name if tournament else 'Unknown',
                'archetype': empty_deck.name
            }
            deleted_log.append(deleted_detail)
            
            print(f"[RECALCULATE] Deleting empty deck ID {empty_deck.id}: {empty_deck.name} (player: {player.name if player else 'Unknown'})")
            db.session.delete(empty_deck)
            deleted_count += 1
        
        # Now get all decks with decklists for validation and recalculation
        all_decks = Deck.query.filter(Deck.list_text.isnot(None), Deck.list_text != '').all()
        
        # Delete decks with less than 60 cards in main deck
        for deck in all_decks[:]:  # Use slice to avoid modifying list during iteration
            if deck.list_text:
                # Count main deck cards (exclude sideboard)
                lines = deck.list_text.strip().split('\n')
                in_sideboard = False
                main_deck_count = 0
                
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    
                    # Check if we've entered sideboard section
                    if line.lower() in ['sideboard', 'sb']:
                        in_sideboard = True
                        continue
                    
                    # Skip sideboard cards
                    if in_sideboard:
                        continue
                    
                    # Skip section headers
                    if line.lower() in ['mainboard', 'maindeck', 'main']:
                        continue
                    
                    # Count cards in main deck
                    match = re.match(r'^(\d+)x?\s+', line)
                    if match:
                        count = int(match.group(1))
                        main_deck_count += count
                
                # Delete if less than 60 cards
                if main_deck_count < 60:
                    player = Player.query.get(deck.player_id) if deck.player_id else None
                    tournament = Tournament.query.get(deck.tournament_id) if deck.tournament_id else None
                    
                    deleted_detail = {
                        'deck_id': deck.id,
                        'player': player.name if player else 'Unknown',
                        'tournament': tournament.name if tournament else 'Unknown',
                        'archetype': deck.name,
                        'reason': f'Incomplete deck ({main_deck_count} cards)'
                    }
                    deleted_log.append(deleted_detail)
                    
                    print(f"[RECALCULATE] Deleting incomplete deck ID {deck.id}: {deck.name} ({main_deck_count} cards)")
                    db.session.delete(deck)
                    deleted_count += 1
                    all_decks.remove(deck)
        
        # Get all available model decklists
        models = ArchetypeModel.query.all()
        if not models:
            return jsonify({"error": "No archetype models found. Please create model decklists first."}), 400
        
        updated_count = 0
        unchanged_count = 0
        rogue_count = 0
        skipped_count = 0
        changes_log = []
        
        for deck in all_decks:
            old_archetype = deck.name
            
            # Detect new archetype based on similarity with 20% threshold
            # Use require_threshold=True to classify as Rogue if below 20%
            new_archetype, similarity = detect_archetype_from_decklist(
                deck.list_text, 
                similarity_threshold=0.2,   # 20% threshold (same as imports)
                require_threshold=True      # Classify as Rogue if below threshold
            )
            
            if new_archetype != old_archetype:
                # Log the change
                player = Player.query.get(deck.player_id) if deck.player_id else None
                tournament = Tournament.query.get(deck.tournament_id) if deck.tournament_id else None
                
                change_detail = {
                    'deck_id': deck.id,
                    'player': player.name if player else 'Unknown',
                    'tournament': tournament.name if tournament else 'Unknown',
                    'old_archetype': old_archetype,
                    'new_archetype': new_archetype,
                    'similarity': round(similarity * 100, 2)
                }
                changes_log.append(change_detail)
                
                # ONLY update the deck's archetype name - do NOT modify image_url, colors, or any other fields
                # Archetype images and other properties should remain unchanged
                deck.name = new_archetype
                updated_count += 1
                print(f"[RECALCULATE] Updated deck ID {deck.id}: {old_archetype} -> {new_archetype} (similarity: {similarity:.2%})")
            else:
                unchanged_count += 1
            
            if new_archetype == "Rogue":
                rogue_count += 1
        
        # Log the recalculation event
        log_event(
            action_type='archetype_recalculation',
            details=f'Recalculated {len(all_decks)} decks. Updated: {updated_count}, Unchanged: {unchanged_count}, Rogue: {rogue_count}, Deleted empty: {deleted_count}',
            backup_data=json.dumps({'changes': changes_log, 'deleted': deleted_log}, indent=2),
            recoverable=True
        )
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "updated": updated_count,
            "unchanged": unchanged_count,
            "rogue": rogue_count,
            "deleted": deleted_count,
            "skipped": skipped_count,
            "total": len(all_decks),
            "changes": changes_log
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"[RECALCULATE] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/decks/<deck_name>')
def deck_detail(deck_name):
    decks = Deck.query.filter_by(name=deck_name).all()
    
    # Get the model decklist for this archetype
    model = ArchetypeModel.query.filter_by(archetype_name=deck_name).first()
    model_decklist = model.model_decklist if model else None
    
    rows = []
    for d in decks:
        player = Player.query.get(d.player_id) if d.player_id else None
        tournament = Tournament.query.get(d.tournament_id) if d.tournament_id else None

        rank = None
        if player and tournament:
            # Calculate rank from tournament standings
            matches = Match.query.filter_by(tournament_id=tournament.id).all()
            players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id) \
                                  .filter(TournamentPlayer.tournament_id == tournament.id).all()
            
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
                    "player_id": p.id,
                    "points": points,
                    "wins": wins
                })
            
            standings.sort(key=lambda s: (s["points"], s["wins"]), reverse=True)
            
            # Find player's rank
            for idx, s in enumerate(standings):
                if s["player_id"] == player.id:
                    rank = idx + 1
                    break

        rows.append({
            "player": player,
            "tournament": tournament,
            "rank": rank,
            "deck": d
        })

    last_deck = Deck.query.filter_by(name=deck_name).order_by(Deck.id.desc()).first()
    image_url = last_deck.image_url if last_deck and last_deck.image_url else None
    
    # Calculate deck statistics
    total_decks = len(decks)
    
    # Get colors from the last deck
    colors = last_deck.colors if last_deck else None
    
    # Calculate tier and meta share (placeholder logic - you can enhance this)
    if deck_name == "Rogue":
        tier = "Rogue"
    else:
        tier = "Tier 1" if total_decks >= 10 else "Tier 2" if total_decks >= 5 else "Tier 3"
    
    # Meta share calculation (example: based on total decks in database)
    total_all_decks = Deck.query.count()
    meta_share = round((total_decks / total_all_decks * 100), 1) if total_all_decks > 0 else 0

    return render_template("deck_detail.html",
                           deck_name=deck_name,
                           rows=rows,
                           image_url=image_url,
                           total_decks=total_decks,
                           tier=tier,
                           meta_share=meta_share,
                           colors=colors,
                           model_decklist=model_decklist)


@app.route('/archetype/<archetype_name>/model', methods=['GET'])
@login_required
def calculate_deck_similarity(deck_list_1, deck_list_2):
    """
    Calculate similarity between two decklists (0.0 to 1.0)
    Uses Jaccard similarity on card names (mainboard only, excludes sideboard)
    """
    def parse_decklist(decklist_text):
        """Extract card names from decklist text (mainboard only)"""
        cards = set()
        lines = decklist_text.strip().split('\n')
        in_sideboard = False
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Check if we've entered sideboard section
            if line.lower() in ['sideboard', 'sb']:
                in_sideboard = True
                continue
            
            # Skip sideboard cards
            if in_sideboard:
                continue
            
            # Skip section headers
            if line.lower() in ['mainboard', 'maindeck', 'main']:
                continue
            
            # Match "4 Card Name" or "4x Card Name" format
            match = re.match(r'^\d+x?\s+(.+)$', line)
            if match:
                card_name = match.group(1).strip().lower()
                cards.add(card_name)
        return cards
    
    cards_1 = parse_decklist(deck_list_1)
    cards_2 = parse_decklist(deck_list_2)
    
    if not cards_1 or not cards_2:
        return 0.0
    
    intersection = len(cards_1 & cards_2)
    union = len(cards_1 | cards_2)
    
    return intersection / union if union > 0 else 0.0


def detect_archetype_from_decklist(deck_list_text, similarity_threshold=0.2, require_threshold=True):
    """
    Auto-detect archetype by comparing deck to all model decklists
    Returns (archetype_name, similarity_score)
    
    Default threshold: 20% (0.2)
    If require_threshold=True: Returns "Rogue" if no match above threshold
    If require_threshold=False: Returns best match regardless of threshold (unless all are 0)
    """
    if not deck_list_text or not deck_list_text.strip():
        return "Rogue", 0.0
    
    models = ArchetypeModel.query.all()
    if not models:
        return "Rogue", 0.0
    
    best_match = None
    best_similarity = 0.0
    
    for model in models:
        similarity = calculate_deck_similarity(deck_list_text, model.model_decklist)
        if similarity > best_similarity:
            best_similarity = similarity
            best_match = model.archetype_name
    
    # If we don't require threshold, return best match as long as similarity > 0
    if not require_threshold:
        if best_similarity > 0 and best_match:
            return best_match, best_similarity
        return "Rogue", 0.0
    
    # Original behavior: require threshold
    if best_similarity >= similarity_threshold:
        return best_match, best_similarity
    
    # No match found - classify as Rogue
    return "Rogue", 0.0


def get_archetype_model(archetype_name):
    """Get the model decklist for an archetype"""
    model = ArchetypeModel.query.filter_by(archetype_name=archetype_name).first()
    if model:
        return jsonify({
            'success': True,
            'decklist': model.model_decklist
        })
    return jsonify({
        'success': False,
        'message': 'No model decklist found for this archetype'
    })


@app.route('/archetype/<archetype_name>/model', methods=['POST'])
@login_required
def save_archetype_model(archetype_name):
    """Save or update the model decklist for an archetype"""
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': 'Admin access required'}), 403
    
    decklist = request.form.get('decklist', '').strip()
    
    if not decklist:
        return jsonify({'success': False, 'message': 'Decklist cannot be empty'}), 400
    
    model = ArchetypeModel.query.filter_by(archetype_name=archetype_name).first()
    if model:
        model.model_decklist = decklist
        model.updated_at = datetime.utcnow()
    else:
        model = ArchetypeModel(
            archetype_name=archetype_name,
            model_decklist=decklist
        )
        db.session.add(model)
    
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Model decklist saved successfully'})


@app.route('/store/<int:store_id>')
def store_page(store_id):
    """Display store page with tournaments held at this store"""
    store = Store.query.get_or_404(store_id)
    
    # Get all tournaments at this store
    tournaments = Tournament.query.filter_by(store_id=store_id, pending=False).order_by(Tournament.date.desc()).all()
    
    # Calculate statistics
    total_tournaments = len(tournaments)
    total_players = 0
    for t in tournaments:
        total_players += TournamentPlayer.query.filter_by(tournament_id=t.id).count()
    
    avg_players = round(total_players / total_tournaments, 1) if total_tournaments > 0 else 0
    
    return render_template("store_page.html",
                           store=store,
                           tournaments=tournaments,
                           total_tournaments=total_tournaments,
                           avg_players=avg_players)




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
    store_tokens = get_user_store_tokens(current_user)
    
    # Check if user has any stores with available tokens
    has_tokens_competitive = any(s['has_competitive'] for s in store_tokens)
    has_tokens_premium = any(s['has_premium'] for s in store_tokens)
    
    # Cards are only enabled if there are actual tokens available
    # (Admin/scorekeeper status doesn't bypass this UI restriction)
    has_any_competitive = has_tokens_competitive
    has_any_premium = has_tokens_premium
    
    return render_template('choose_tournament_type.html', 
                         store_tokens=store_tokens,
                         has_any_competitive=has_any_competitive,
                         has_any_premium=has_any_premium,
                         has_tokens_competitive=has_tokens_competitive,
                         has_tokens_premium=has_tokens_premium)



# --- New Tournament ---
@app.route('/tournament/new', methods=['GET', 'POST'])
@login_required
def new_tournament():
    if not (current_user.is_admin or current_user.is_scorekeeper):
        flash("Only admins or scorekeepers can create tournaments.", "error")
        return redirect(url_for('players'))
    
    # Check token availability before allowing access
    tournament_type = request.args.get('type', 'competitive')
    store_tokens = get_user_store_tokens(current_user)
    
    if tournament_type == 'premium':
        has_premium_tokens = any(s['has_premium'] for s in store_tokens)
        if not has_premium_tokens:
            flash("No premium tokens available. Please add tokens to your stores.", "error")
            return redirect(url_for('choose_tournament_type'))
    elif tournament_type == 'competitive':
        has_competitive_tokens = any(s['has_competitive'] for s in store_tokens)
        if not has_competitive_tokens:
            flash("No competitive tokens available. Please add tokens to your stores.", "error")
            return redirect(url_for('choose_tournament_type'))
    # Casual tournaments don't require token check

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
                pending=False,  # Manual tournaments don't need confirmation
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

            # Log event
            tournament_type_label = "Premium" if tournament.premium else ("Casual" if tournament.casual else "Competitive")
            log_event(
                action_type='tournament_created',
                details=f"Created {tournament_type_label} tournament: {tournament_name}",
                recoverable=False
            )

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

                # Log event
                tournament_type_label = "Premium" if tournament.premium else ("Casual" if tournament.casual else "Competitive")
                log_event(
                    action_type='tournament_created',
                    details=f"Created {tournament_type_label} tournament from EventLink import: {event_name}",
                    recoverable=False
                )

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

                # Log event
                tournament_type_label = "Premium" if tournament.premium else ("Casual" if tournament.casual else "Competitive")
                log_event(
                    action_type='tournament_created',
                    details=f"Created {tournament_type_label} tournament from Arena import: {event_name}",
                    recoverable=False
                )

                confirm_url = url_for('tournament_round', tid=tournament.id, round_num=1,
                                      token=tournament.confirm_token, _external=True)
                flash(f"Tournament created from Arena text. Confirmation link: {confirm_url}", "success")
                return redirect(confirm_url)

    return render_template('new_tournament.html')







def merge_player_ids(source_id, target_id):
    if source_id == target_id:
        return
    
    # Handle TournamentPlayer entries - delete source entries where target already exists
    source_tps = TournamentPlayer.query.filter_by(player_id=source_id).all()
    for tp in source_tps:
        # Check if target already has an entry for this tournament
        target_tp = TournamentPlayer.query.filter_by(
            tournament_id=tp.tournament_id, 
            player_id=target_id
        ).first()
        
        if target_tp:
            # Target already registered for this tournament, just delete source entry
            db.session.delete(tp)
        else:
            # Target not registered, update source entry to target
            tp.player_id = target_id
    
    # Update all match references from source to target
    Match.query.filter_by(player1_id=source_id).update({"player1_id": target_id})
    Match.query.filter_by(player2_id=source_id).update({"player2_id": target_id})
    
    # Update all deck references from source to target
    Deck.query.filter_by(player_id=source_id).update({"player_id": target_id})
    
    db.session.commit()
    
    # Delete the old player row only if it still exists
    src = Player.query.get(source_id)
    if src:
        db.session.delete(src)
        db.session.commit()

from collections import Counter

def cleanup_expired_tournaments():
    """Delete pending tournaments older than 24 hours"""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(hours=24)
    
    expired = Tournament.query.filter(
        Tournament.pending == True,
        Tournament.submitted_at < cutoff
    ).all()
    
    for t in expired:
        # Delete associated matches, decks, and tournament players
        Match.query.filter_by(tournament_id=t.id).delete()
        Deck.query.filter_by(tournament_id=t.id).delete()
        TournamentPlayer.query.filter_by(tournament_id=t.id).delete()
        db.session.delete(t)
    
    if expired:
        db.session.commit()
        print(f"[CLEANUP] Deleted {len(expired)} expired pending tournament(s)", file=sys.stderr)

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


@app.route('/fetch_tcdecks', methods=['POST'])
@login_required
def fetch_tcdecks():
    """Fetch TCDecks tournament page content using headless browser"""
    from urllib.parse import urlparse
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    
    data = request.get_json()
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'success': False, 'error': 'No URL provided'}), 400
    
    # Validate URL is from tcdecks.net
    try:
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.hostname.endswith('tcdecks.net'):
            return jsonify({'success': False, 'error': 'URL must be from tcdecks.net'}), 400
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid URL format'}), 400
    
    # Use Playwright to fetch and render the page with JavaScript
    try:
        with sync_playwright() as p:
            # Launch browser in headless mode with realistic settings
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox'
                ]
            )
            
            # Create context with realistic browser headers and settings
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                locale='en-US',
                timezone_id='America/New_York',
                extra_http_headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"'
                }
            )
            
            # Override navigator.webdriver property
            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            
            # Navigate to the page and wait for content to load
            page.goto(url, wait_until='domcontentloaded', timeout=20000)
            
            # Wait for dynamic content to load
            page.wait_for_timeout(1000)
            
            # Get the fully rendered HTML
            html_content = page.content()
            
            browser.close()
        
        return jsonify({
            'success': True,
            'html': html_content
        })
    except PlaywrightTimeout:
        return jsonify({'success': False, 'error': 'Request timed out. Please try again.'}), 408
    except Exception as e:
        return jsonify({'success': False, 'error': f'Failed to fetch page: {str(e)}'}), 500


@app.route('/fetch_tcdecks_decklist', methods=['POST'])
@login_required
def fetch_tcdecks_decklist():
    """Fetch individual decklist from TCDecks"""
    from urllib.parse import urlparse
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    from bs4 import BeautifulSoup
    import re
    
    data = request.get_json()
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'success': False, 'error': 'No URL provided'}), 400
    
    # Validate URL is from tcdecks.net
    try:
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.hostname.endswith('tcdecks.net'):
            return jsonify({'success': False, 'error': 'URL must be from tcdecks.net'}), 400
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid URL format'}), 400
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox'
                ]
            )
            
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                locale='en-US',
                timezone_id='America/New_York',
                extra_http_headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'same-origin',
                    'Sec-Fetch-User': '?1',
                    'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"'
                }
            )
            
            page = context.new_page()
            
            # Override navigator.webdriver
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            
            # Set cookies if needed (TCDecks cookie consent)
            page.context.add_cookies([
                {
                    'name': 'cookieconsent_status',
                    'value': 'dismiss',
                    'domain': '.tcdecks.net',
                    'path': '/'
                }
            ])
            
            # Navigate directly to deck page
            page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Wait for dynamic content to load - increase wait time for full deck
            page.wait_for_timeout(2500)
            
            html_content = page.content()
            browser.close()
        
        # Parse the decklist from HTML
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Check if we got a 403 or access denied message
        text = soup.get_text()
        if '403' in text or 'Acceso Denegado' in text or 'Access Denied' in text:
            print(f"[DECKLIST] Access denied for {url}", file=sys.stderr)
            return jsonify({'success': False, 'error': 'Access denied by TCDecks. Try again later.'}), 403
        
        mainboard_cards = []
        sideboard_cards = []
        
        # TCDecks shows decklist in 3 columns: Column 1, Column 2, Column 3 (Sideboard)
        # The actual decklist is in a specific table/div structure, not the summary counts
        
        # Strategy: Look for the actual card list by finding elements with card data
        # TCDecks typically has the deck in a table with 3 columns side by side
        
        # First, replace <br> tags with newlines to preserve structure
        for br in soup.find_all('br'):
            br.replace_with('\n')
        
        # Look for tables that contain actual card lists (not just totals)
        tables = soup.find_all('table')
        deck_table = None
        
        for idx, table in enumerate(tables):
            text_preview = table.get_text()
            # Check if table has multiple card entries (not just "60 Cards", "15 Cards")
            card_pattern_matches = re.findall(r'\d+\s+[A-Z][a-z]', text_preview)
            
            # We want a table with many card entries, not just totals
            if len(card_pattern_matches) > 5:  # Real decklist will have many cards
                deck_table = table
                break
        
        if deck_table:
            # Parse table with 3 columns (or rows with multiple cells)
            # TCDecks can use either TR with 3 TD cells, or nested tables
            
            # Get all text and try to find column structure
            all_tds = deck_table.find_all('td')
            print(f"DEBUG: Found table with {len(all_tds)} total TD cells")
            
            # Check if cells are arranged in groups (columns)
            if len(all_tds) >= 3:
                # Common pattern: 3 TD cells in one row containing the 3 columns
                rows = deck_table.find_all('tr')
                print(f"DEBUG: Table has {len(rows)} rows")
                
                rows_processed = 0
                for row in rows:
                    cells = row.find_all('td')
                    print(f"DEBUG: Row has {len(cells)} cells")
                    
                    if len(cells) >= 3:
                        rows_processed += 1
                        
                        # Process first 3 cells as columns
                        for col_idx in range(min(3, len(cells))):
                            cell = cells[col_idx]
                            is_sideboard = (col_idx == 2)  # Only third column is sideboard
                            
                            cell_text = cell.get_text()
                            cell_lines = [l.strip() for l in cell_text.split('\n') if l.strip()]
                            
                            for line in cell_lines:
                                # Handle combined lines like "Creatures [13]4 Weathered Wayfarer"
                                # Split section header from card line
                                combined_match = re.match(r'^([A-Za-z\s]+\s*\[\d+\])(.+)$', line)
                                if combined_match:
                                    # Split into separate lines and process the card part
                                    line = combined_match.group(2).strip()
                                    print(f"DEBUG: Split combined line, processing: '{line}'")
                                
                                # Skip section headers (e.g., "Creatures [4]", "Land [20]")
                                if re.match(r'^[A-Za-z\s]+\s*\[\d+\]$', line):
                                    continue
                                if line.lower() in ['sideboard', 'mainboard', 'maindeck', 'main deck']:
                                    continue
                                # Skip total count lines
                                if re.match(r'^\d+\s+cards?$', line.lower()):
                                    continue
                                # Skip standalone section names without brackets
                                if line.lower() in ['creatures', 'instants', 'sorceries', 'artifacts', 'enchantments', 'planeswalkers', 'land', 'lands']:
                                    continue
                                
                                # Parse card line: "4 Card Name" or "4x Card Name"
                                card_match = re.match(r'^(\d+)x?\s+(.+)$', line)
                                if card_match:
                                    quantity = card_match.group(1)
                                    card_name = card_match.group(2).strip()
                                    
                                    # Skip if card name is too short or looks like metadata
                                    if len(card_name) < 2:
                                        continue
                                    if card_name.lower() in ['cards', 'total', 'deck value', 'card']:
                                        continue
                                    
                                    card_line = f"{quantity} {card_name}"
                                    print(f"DEBUG: Matched card: {card_line} (sideboard={is_sideboard})")
                                    
                                    if is_sideboard:
                                        sideboard_cards.append(card_line)
                                    else:
                                        mainboard_cards.append(card_line)
                                else:
                                    print(f"DEBUG: Line did not match card pattern: '{line}'")
                
                print(f"DEBUG: Processed {rows_processed} rows with 3+ cells")
            else:
                # Try alternative: cells might be in separate rows
                pass
        
        print(f"DEBUG: After table parsing - mainboard: {len(mainboard_cards)}, sideboard: {len(sideboard_cards)}")
        
        # If table parsing didn't work, try finding divs with specific structure
        if len(mainboard_cards) == 0 and len(sideboard_cards) == 0:
            
            # Look for divs or other containers that might hold columns
            # Get all text and parse linearly, but skip known non-card lines
            text = soup.get_text()
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            
            in_deck_section = False
            
            for line in lines:
                line_lower = line.lower()
                
                # Skip headers, metadata, and totals
                if re.match(r'^[A-Za-z\s]+\s*\[\d+\]$', line):
                    in_deck_section = True  # We're in a card type section
                    continue
                if re.search(r'^\d+\s+cards?$', line_lower):
                    continue  # Skip "60 Cards", "15 Cards"
                if any(kw in line_lower for kw in ['format', 'modern', 'legacy', 'vintage', 'event', 'player', 'ranking', 'position', 'deck value']):
                    continue
                
                # Parse card lines
                card_match = re.match(r'^(\d+)x?\s+(.+)$', line)
                if card_match and in_deck_section:
                    quantity = card_match.group(1)
                    card_name = card_match.group(2).strip()
                    
                    if len(card_name) >= 2 and card_name.lower() not in ['cards', 'total']:
                        mainboard_cards.append(f"{quantity} {card_name}")
        
        # Construct the decklist text
        decklist = ""
        if mainboard_cards:
            decklist += "\n".join(mainboard_cards)
        if sideboard_cards:
            decklist += "\n\nSideboard:\n" + "\n".join(sideboard_cards)
        
        if not decklist.strip():
            # Log failure for debugging
            print(f"[DECKLIST] Could not parse decklist from {url}", file=sys.stderr)
            print(f"[DECKLIST] Page text length: {len(text)}", file=sys.stderr)
            print(f"[DECKLIST] First 500 chars: {text[:500]}", file=sys.stderr)
            return jsonify({'success': False, 'error': 'Could not parse decklist from page'}), 400
        
        print(f"[DECKLIST] Successfully parsed {len(mainboard_cards)} mainboard + {len(sideboard_cards)} sideboard cards", file=sys.stderr)
        
        return jsonify({
            'success': True,
            'decklist': decklist.strip()
        })
        
    except PlaywrightTimeout:
        print(f"[DECKLIST] Timeout fetching {url}", file=sys.stderr)
        return jsonify({'success': False, 'error': 'Request timed out'}), 408
    except Exception as e:
        print(f"[DECKLIST] Error: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Failed to fetch decklist: {str(e)}'}), 500


@app.route('/tournament/new_casual', methods=['GET', 'POST'])
@login_required
def new_tournament_casual():
    if request.method == 'POST':
        tournament_name = request.form.get('tournament_name')
        date_str = request.form.get('date')
        top_cut = int(request.form.get('top_cut') or 8)
        player_count = int(request.form.get('player_count') or 0)
        rounds = int(request.form.get('rounds') or 0)
        store_id = request.form.get('store_id')
        
        # Get country from store if store is selected
        country = None
        if store_id:
            store = Store.query.get(int(store_id))
            if store:
                country = store.country

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
            store_id=int(store_id) if store_id else None,
            pending=False,  # Casual tournaments are immediately finalized
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
        share_links_created = []
        for rank in range(1, top_cut + 1):
            player_name = request.form.get(f"player_{rank}", "").strip()
            deck_name = html.escape(request.form.get(f"deck_name_{rank}", "").strip())
            deck_list = html.escape(request.form.get(f"deck_list_{rank}", "").strip())
            deck_mode = request.form.get(f"deck_mode_{rank}", "add")

            if player_name:
                player = ensure_player(player_name)
                db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=player.id))

                # Award casual points
                pts = award_casual_points(player_count, rank)
                if pts > 0:
                    player.casual_points = (player.casual_points or 0) + pts
                    
                    # Create history record for per-store tracking
                    history_record = CasualPointsHistory(
                        player_id=player.id,
                        tournament_id=tournament.id,
                        store_id=tournament.store_id,
                        points=pts,
                        rank=rank,
                        awarded_at=datetime.utcnow()
                    )
                    db.session.add(history_record)

                # Handle deck submission mode
                if deck_mode == "share":
                    # Generate share link for this player
                    submission_token = secrets.token_urlsafe(32)
                    link = DeckSubmissionLink(
                        tournament_id=tournament.id,
                        player_name=player_name,
                        submission_token=submission_token,
                        deck_submitted=False
                    )
                    db.session.add(link)
                    share_links_created.append({
                        'player_name': player_name,
                        'token': submission_token
                    })
                elif deck_name or deck_list:
                    # Auto-detect archetype from decklist
                    detected_archetype, similarity = detect_archetype_from_decklist(deck_list)
                    
                    # Always use detected archetype (either matched archetype or "Rogue")
                    final_deck_name = detected_archetype
                    if similarity > 0:
                        print(f"[AUTO-DETECT] Player '{player_name}' deck matched to '{detected_archetype}' with {similarity:.2%} similarity", file=sys.stderr)
                    else:
                        print(f"[AUTO-DETECT] Player '{player_name}' deck classified as 'Rogue'", file=sys.stderr)
                    
                    # Save deck if provided
                    deck = Deck.query.filter_by(player_id=player.id, tournament_id=tournament.id).first()
                    if deck:
                        old_name = deck.name
                        deck.name = final_deck_name
                        deck.list_text = deck_list
                        
                        if old_name != final_deck_name:
                            print(f"[AUTO-DETECT] Deck re-grouped from '{old_name}' to '{final_deck_name}'", file=sys.stderr)
                        
                        # Preserve existing image if archetype has one and current deck doesn't
                        if not deck.image_url and final_deck_name:
                            existing_archetype = Deck.query.filter_by(name=final_deck_name).filter(Deck.image_url.isnot(None)).first()
                            if existing_archetype:
                                deck.image_url = existing_archetype.image_url
                    else:
                        # Get archetype image if available
                        archetype_image = None
                        if final_deck_name:
                            existing_archetype = Deck.query.filter_by(name=final_deck_name).filter(Deck.image_url.isnot(None)).first()
                            if existing_archetype:
                                archetype_image = existing_archetype.image_url
                        
                        deck = Deck(
                            player_id=player.id,
                            tournament_id=tournament.id,
                            name=final_deck_name,
                            list_text=deck_list,
                            image_url=archetype_image
                        )
                        db.session.add(deck)

        db.session.commit()
        
        # Log event
        log_event(
            action_type='tournament_created',
            details=f"Created Casual tournament: {tournament_name}",
            recoverable=False
        )
        
        # If there are share links, redirect to share links page
        if share_links_created:
            flash(f"Casual tournament created! {len(share_links_created)} deck submission link(s) generated.", "success")
            return redirect(url_for('show_casual_share_links', tid=tournament.id))
        
        flash("Casual tournament final standings saved!", "success")
        return redirect(url_for('players'))

    # Build stores list for selector
    stores = []
    if current_user.is_authenticated:
        if current_user.is_admin:
            stores = Store.query.all()
        else:
            stores = stores_for_user(current_user)
    
    return render_template('new_tournament_casual.html', stores=stores)


@app.route('/tournament/<int:tid>/edit_casual', methods=['GET', 'POST'])
@login_required
def edit_casual_tournament(tid):
    """Edit an existing casual tournament"""
    tournament = Tournament.query.get_or_404(tid)
    
    # Verify this is a casual tournament
    if not tournament.casual:
        flash("This is not a casual tournament.", "error")
        return redirect(url_for('tournament_standings', tid=tid))
    
    # Check permissions
    can_edit = (current_user.is_admin or tournament.user_id == current_user.id)
    if not can_edit:
        flash("You don't have permission to edit this tournament.", "error")
        return redirect(url_for('tournament_standings', tid=tid))
    
    # Verify edit token
    url_token = request.args.get('edit_token')
    session_token = session.get(f'edit_{tid}')
    
    if url_token and url_token == tournament.edit_token:
        session[f'edit_{tid}'] = url_token
    elif not session_token or session_token != tournament.edit_token:
        flash("Invalid or expired edit link.", "error")
        return redirect(url_for('tournament_standings', tid=tid))
    
    if request.method == 'POST':
        # Get existing players to remove old casual points
        old_tournament_players = TournamentPlayer.query.filter_by(tournament_id=tid).all()
        old_player_data = {}
        
        # Store old rankings and points
        for tp in old_tournament_players:
            player = Player.query.get(tp.player_id)
            if player:
                # Find player's old rank by checking decks
                deck = Deck.query.filter_by(player_id=player.id, tournament_id=tid).first()
                if deck:
                    old_player_data[player.id] = player
        
        # Get old player count to calculate old points
        old_player_count = tournament.player_count or len(old_tournament_players)
        old_top_cut = tournament.top_cut or 0
        
        # Helper function to calculate points
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
        
        # Remove old casual points for all players
        for rank in range(1, old_top_cut + 1):
            player_id = request.form.get(f"old_player_{rank}")
            if player_id and int(player_id) in old_player_data:
                player = old_player_data[int(player_id)]
                old_pts = award_casual_points(old_player_count, rank)
                if old_pts > 0:
                    player.casual_points = max(0, (player.casual_points or 0) - old_pts)
        
        # Delete old history records for this tournament
        CasualPointsHistory.query.filter_by(tournament_id=tid).delete()
        
        # Update tournament metadata
        tournament.name = request.form.get('tournament_name')
        tournament.date = datetime.strptime(request.form.get('date'), "%Y-%m-%d")
        tournament.top_cut = int(request.form.get('top_cut') or 8)
        tournament.player_count = int(request.form.get('player_count') or 0)
        tournament.rounds = int(request.form.get('rounds') or 0)
        store_id = request.form.get('store_id')
        
        # Update store and country
        if store_id:
            tournament.store_id = int(store_id)
            store = Store.query.get(int(store_id))
            if store:
                tournament.country = store.country
        else:
            tournament.store_id = None
        
        # Delete old tournament players and decks
        TournamentPlayer.query.filter_by(tournament_id=tid).delete()
        Deck.query.filter_by(tournament_id=tid).delete()
        
        # Add new players and award new points
        for rank in range(1, tournament.top_cut + 1):
            player_name = request.form.get(f"player_{rank}", "").strip()
            deck_name = html.escape(request.form.get(f"deck_name_{rank}", "").strip())
            deck_list = html.escape(request.form.get(f"deck_list_{rank}", "").strip())
            
            if player_name:
                player = ensure_player(player_name)
                db.session.add(TournamentPlayer(tournament_id=tid, player_id=player.id))
                
                # Award new casual points
                pts = award_casual_points(tournament.player_count, rank)
                if pts > 0:
                    player.casual_points = (player.casual_points or 0) + pts
                    
                    # Create new history record for per-store tracking
                    history_record = CasualPointsHistory(
                        player_id=player.id,
                        tournament_id=tid,
                        store_id=tournament.store_id,
                        points=pts,
                        rank=rank,
                        awarded_at=datetime.utcnow()
                    )
                    db.session.add(history_record)
                
                # Save deck if provided
                if deck_name or deck_list:
                    # Auto-detect archetype from decklist
                    detected_archetype, similarity = detect_archetype_from_decklist(deck_list)
                    
                    # Always use detected archetype (either matched archetype or "Rogue")
                    final_deck_name = detected_archetype
                    if similarity > 0:
                        print(f"[AUTO-DETECT] Player '{player_name}' deck matched to '{detected_archetype}' with {similarity:.2%} similarity", file=sys.stderr)
                    else:
                        print(f"[AUTO-DETECT] Player '{player_name}' deck classified as 'Rogue'", file=sys.stderr)
                    
                    deck = Deck(
                        player_id=player.id,
                        tournament_id=tid,
                        name=final_deck_name,
                        list_text=deck_list
                    )
                    db.session.add(deck)
        
        # Clear edit token to finalize edits
        tournament.edit_token = None
        session.pop(f'edit_{tid}', None)
        
        db.session.commit()
        
        # Log event
        log_event(
            action_type='tournament_updated',
            details=f"Updated Casual tournament: {tournament.name}",
            recoverable=False
        )
        
        flash("Casual tournament updated successfully!", "success")
        return redirect(url_for('players'))
    
    # GET request: Load tournament data for editing
    tournament_players = TournamentPlayer.query.filter_by(tournament_id=tid).all()
    players_with_decks = []
    
    for tp in tournament_players:
        player = Player.query.get(tp.player_id)
        deck = Deck.query.filter_by(player_id=player.id, tournament_id=tid).first()
        players_with_decks.append({
            'player_id': player.id,
            'player_name': player.name,
            'deck_name': deck.name if deck else '',
            'deck_list': deck.list_text if deck else ''
        })
    
    # Build stores list for selector
    stores = []
    if current_user.is_authenticated:
        if current_user.is_admin:
            stores = Store.query.all()
        else:
            stores = stores_for_user(current_user)
    
    return render_template('new_tournament_casual.html', 
                         tournament=tournament,
                         players_with_decks=players_with_decks,
                         is_editing=True,
                         edit_token=tournament.edit_token,
                         stores=stores)


@app.route('/tournament/<int:tid>/set_country', methods=['POST'])
@login_required
def set_tournament_country(tid):
    tournament = Tournament.query.get_or_404(tid)

    # Only admins or scorekeepers can change
    if not (current_user.is_admin or current_user.is_scorekeeper):
        flash("You do not have permission to set the country.", "error")
        return redirect(url_for('players'))

    # Determine states
    is_new_import = tournament.pending and tournament.confirm_token
    is_editing = not tournament.pending and tournament.edit_token
    is_manual_tournament = not tournament.imported_from_text
    
    # Verify token for new imports
    if is_new_import:
        session_key = f"tok_{tid}"
        session_token = session.get(session_key)
        if not session_token or session_token != tournament.confirm_token:
            flash("Invalid or expired confirmation link.", "error")
            return redirect(url_for('players'))
    
    # Verify token for edit mode
    if is_editing:
        session_key = f"edit_{tid}"
        session_token = session.get(session_key)
        if not session_token or session_token != tournament.edit_token:
            flash("Invalid or expired edit link.", "error")
            return redirect(url_for('players'))
    
    # Authorization for manual tournaments
    if is_manual_tournament:
        can_access = (current_user.is_admin or tournament.user_id == current_user.id)
        if not can_access:
            flash("You don't have permission to modify this tournament.", "error")
            return redirect(url_for('players'))
    
    # Block access to finalized imported tournaments
    if tournament.imported_from_text and not is_new_import and not is_editing:
        flash("Country can only be changed during import confirmation or edit mode.", "error")
        return redirect(url_for('players'))

    # Validate input
    country = request.form.get("country")
    if not country:
        flash("Please select a country.", "error")
        if is_new_import:
            return redirect(url_for('tournament_round', tid=tid, round_num=1, token=tournament.confirm_token))
        else:
            return redirect(url_for('tournament_round', tid=tid, round_num=1, edit_token=tournament.edit_token))

    # Apply change
    tournament.country = country
    db.session.commit()

    flash(f"Tournament country set to {country}.", "success")
    if is_new_import:
        return redirect(url_for('tournament_round', tid=tid, round_num=1, token=tournament.confirm_token))
    else:
        return redirect(url_for('tournament_round', tid=tid, round_num=1, edit_token=tournament.edit_token))

@app.route("/tournament/<int:tid>/set_store", methods=["POST"])
@login_required
def set_tournament_store(tid):
    tournament = Tournament.query.get_or_404(tid)

    # Only admins or scorekeepers can change
    if not (current_user.is_admin or current_user.is_scorekeeper):
        flash("You do not have permission to set the store.", "error")
        return redirect(url_for('players'))

    # Determine states
    is_new_import = tournament.pending and tournament.confirm_token
    is_editing = not tournament.pending and tournament.edit_token
    is_manual_tournament = not tournament.imported_from_text
    
    # Verify token for new imports
    if is_new_import:
        session_key = f"tok_{tid}"
        session_token = session.get(session_key)
        if not session_token or session_token != tournament.confirm_token:
            flash("Invalid or expired confirmation link.", "error")
            return redirect(url_for('players'))
    
    # Verify token for edit mode
    if is_editing:
        session_key = f"edit_{tid}"
        session_token = session.get(session_key)
        if not session_token or session_token != tournament.edit_token:
            flash("Invalid or expired edit link.", "error")
            return redirect(url_for('players'))
    
    # Authorization for manual tournaments
    if is_manual_tournament:
        can_access = (current_user.is_admin or tournament.user_id == current_user.id)
        if not can_access:
            flash("You don't have permission to modify this tournament.", "error")
            return redirect(url_for('players'))
    
    # Block access to finalized imported tournaments
    if tournament.imported_from_text and not is_new_import and not is_editing:
        flash("Store can only be changed during import confirmation or edit mode.", "error")
        return redirect(url_for('players'))

    
    store_id = request.form.get("store_id")
    if not store_id:
        flash("Please select a store.", "error")
        if is_new_import:
            return redirect(url_for('tournament_round', tid=tid, round_num=1, token=tournament.confirm_token))
        else:
            return redirect(url_for('tournament_round', tid=tid, round_num=1, edit_token=tournament.edit_token))

    try:
        store_id_int = int(store_id)
    except ValueError:
        flash("Invalid store.", "error")
        if is_new_import:
            return redirect(url_for('tournament_round', tid=tid, round_num=1, token=tournament.confirm_token))
        else:
            return redirect(url_for('tournament_round', tid=tid, round_num=1, edit_token=tournament.edit_token))

    store = Store.query.get(store_id_int)
    if not store:
        flash("Store not found.", "error")
        if is_new_import:
            return redirect(url_for('tournament_round', tid=tid, round_num=1, token=tournament.confirm_token))
        else:
            return redirect(url_for('tournament_round', tid=tid, round_num=1, edit_token=tournament.edit_token))

    # Ensure this user can use that store
    if not can_use_store(current_user, store.id):
        flash("You are not assigned to this store.", "error")
        if is_new_import:
            return redirect(url_for('tournament_round', tid=tid, round_num=1, token=tournament.confirm_token))
        else:
            return redirect(url_for('tournament_round', tid=tid, round_num=1, edit_token=tournament.edit_token))

    # Apply store and auto-country
    tournament.store_id = store.id
    tournament.country = store.country
    db.session.commit()

    flash(f"Store set to {store.name}. Country auto-set to {store.country}.", "success")
    if is_new_import:
        return redirect(url_for('tournament_round', tid=tid, round_num=1, token=tournament.confirm_token))
    else:
        return redirect(url_for('tournament_round', tid=tid, round_num=1, edit_token=tournament.edit_token))
    return redirect(redirect_url)


@app.route('/player/<int:pid>')
def player_info(pid):
    player = Player.query.get_or_404(pid)
    tournaments = (
        db.session.query(Tournament, TournamentPlayer)
        .join(TournamentPlayer, Tournament.id == TournamentPlayer.tournament_id)
        .filter(TournamentPlayer.player_id == pid)
        .filter(Tournament.pending == False)
        .all()
    )

    history = []
    top1 = top4 = top8 = 0
    for t, tp in tournaments:
        deck = Deck.query.filter_by(player_id=pid, tournament_id=t.id).first()
        
        # Calculate player's rank in this tournament based on match results
        rank = None
        matches = Match.query.filter_by(tournament_id=t.id).all()
        if matches:
            # Get all players in tournament
            tournament_players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id) \
                                  .filter(TournamentPlayer.tournament_id == t.id).all()
            
            # Calculate standings
            standings = []
            for p in tournament_players:
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
                    "player_id": p.id,
                    "wins": wins,
                    "draws": draws,
                    "losses": losses,
                    "points": points
                })
            
            # Sort by points, then wins
            standings.sort(key=lambda s: (s["points"], s["wins"]), reverse=True)
            
            # Find player's rank
            for idx, standing in enumerate(standings, 1):
                if standing["player_id"] == pid:
                    rank = idx
                    break

        if rank and t.top_cut and rank <= t.top_cut:
            if rank == 1:
                top1 += 1
            elif rank <= 4:
                top4 += 1
            elif rank <= 8:
                top8 += 1

        history.append({"tournament": t, "deck": deck, "rank": rank})

    # Calculate total casual points
    total_casual_points = db.session.query(db.func.sum(CasualPointsHistory.points)).filter_by(player_id=pid).scalar() or 0

    # Calculate most played stores (max 3)
    store_counts = db.session.query(
        Store.name,
        db.func.count(Tournament.id).label('tournament_count')
    ).join(Tournament, Tournament.store_id == Store.id) \
     .join(TournamentPlayer, Tournament.id == TournamentPlayer.tournament_id) \
     .filter(TournamentPlayer.player_id == pid) \
     .filter(Tournament.pending == False) \
     .group_by(Store.id, Store.name) \
     .order_by(db.desc('tournament_count')) \
     .limit(3) \
     .all()
    
    most_played_stores = [{'name': name, 'count': count} for name, count in store_counts]

    stats = {
        "tournaments_played": len(tournaments),
        "top1": top1,
        "top4": top4,
        "top8": top8,
        "casual_points": total_casual_points,
    }

    # Calculate player's rank among all ranked players for badge tier
    all_players_with_rank = db.session.query(Player).filter(Player.elo.isnot(None)).order_by(Player.elo.desc()).all()
    player_rank = None
    total_ranked = len(all_players_with_rank)
    for idx, p in enumerate(all_players_with_rank, 1):
        if p.id == pid:
            player_rank = idx
            break

    # Prepare data for statistics graphs
    # 1. Top rate over time (chronological tournaments with top placements)
    top_rate_data = []
    for item in sorted(history, key=lambda x: x['tournament'].date):
        t = item['tournament']
        rank = item['rank']
        if rank and t.top_cut and rank <= t.top_cut:
            top_rate_data.append({
                'date': t.date.strftime('%Y-%m-%d'),
                'tournament': t.name,
                'rank': rank,
                'top_cut': t.top_cut
            })
    
    # 2. Color distribution across all decks
    from collections import Counter
    color_counts = Counter()
    archetype_counts = Counter()
    
    for item in history:
        deck = item['deck']
        if deck:
            # Count archetypes
            if deck.name:
                archetype_counts[deck.name] += 1
            
            # Count colors from deck list
            if deck.list_text:
                for line in deck.list_text.split('\n'):
                    line = line.strip()
                    if not line or line.startswith('//') or line.startswith('Sideboard'):
                        continue
                    # Skip quantity prefix (e.g., "4 Lightning Bolt")
                    parts = line.split(' ', 1)
                    if len(parts) >= 2 and parts[0].isdigit():
                        card_name = parts[1].strip()
                        # We'll count colors client-side using Scryfall API
    
    # Get most played archetypes (top 5)
    most_played_archetypes = [{'archetype': arch, 'count': count} 
                              for arch, count in archetype_counts.most_common(5)]

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
        most_played_stores=most_played_stores,
        player_rank=player_rank,
        total_ranked=total_ranked,
        player_country=player_country,   # <-- pass helper
        top_rate_data=top_rate_data,
        most_played_archetypes=most_played_archetypes
    )


@app.route('/share_links/<int:tid>')
@login_required
def show_share_links(tid):
    """Display deck submission share links after tournament creation"""
    tournament = Tournament.query.get_or_404(tid)
    
    # Verify user has permission (admin, scorekeeper, or tournament creator)
    if not (current_user.is_admin or current_user.is_scorekeeper or tournament.user_id == current_user.id):
        flash("You don't have permission to view these links.", "error")
        return redirect(url_for('players'))
    
    # Verify token for pending tournaments
    if tournament.pending and tournament.confirm_token:
        url_token = request.args.get("token")
        session_token = session.get(f"tok_{tid}")
        
        if url_token and url_token == tournament.confirm_token:
            session[f"tok_{tid}"] = url_token
        elif not session_token or session_token != tournament.confirm_token:
            flash("Invalid or expired link.", "error")
            return redirect(url_for('players'))
    
    # Get all deck submission links for this tournament
    share_links = DeckSubmissionLink.query.filter_by(
        tournament_id=tid,
        deck_submitted=False
    ).all()
    
    if not share_links:
        flash("No deck submission links found for this tournament.", "info")
        return redirect(url_for('tournament_round', tid=tid, round_num=1, token=tournament.confirm_token))
    
    return render_template(
        'share_links.html',
        tournament=tournament,
        share_links=share_links,
        token=tournament.confirm_token
    )


@app.route('/casual_share_links/<int:tid>')
@login_required
def show_casual_share_links(tid):
    """Display deck submission share links for casual tournaments"""
    tournament = Tournament.query.get_or_404(tid)
    
    # Verify user has permission (admin or tournament creator)
    if not (current_user.is_admin or tournament.user_id == current_user.id):
        flash("You don't have permission to view these links.", "error")
        return redirect(url_for('players'))
    
    # Get all deck submission links for this tournament
    share_links = DeckSubmissionLink.query.filter_by(
        tournament_id=tid,
        deck_submitted=False
    ).all()
    
    if not share_links:
        flash("No deck submission links found for this tournament.", "info")
        return redirect(url_for('players'))
    
    return render_template(
        'casual_share_links.html',
        tournament=tournament,
        share_links=share_links
    )


# --- Confirm Players ---
@app.route('/confirm_players', methods=['POST'])
@login_required
def confirm_players():
    raw_text = request.form.get("raw_text", "")
    event_name = request.form.get("event_name") or "Imported Tournament"
    parsed_matches = parse_arena_text(raw_text)

    # Build a mapping of names to their final player IDs
    name_to_id = {}
    old_players_to_delete = []  # Store old player IDs to delete after tournament creation
    
    print(f"\n[CONFIRM_PLAYERS] Starting player resolution...", file=sys.stderr)
    
    # First pass: determine what ID each name should map to
    for m in parsed_matches:
        for role in ["player", "opponent"]:
            name = m.get(role)
            if not name or name in name_to_id:
                continue

            safe_name = name.replace(" ", "_")
            action = request.form.get(f"action_{safe_name}")
            
            print(f"[CONFIRM_PLAYERS] Processing '{name}' - action: {action}", file=sys.stderr)

            if action == "create":
                player = Player.query.filter_by(name=name).first()
                if not player:
                    player = Player(name=name, elo=DEFAULT_ELO)
                    db.session.add(player)
                    db.session.commit()
                    print(f"[CONFIRM_PLAYERS] Created new player '{name}' with ID {player.id}", file=sys.stderr)
                else:
                    print(f"[CONFIRM_PLAYERS] Player '{name}' already exists with ID {player.id}", file=sys.stderr)
                name_to_id[name] = player.id

            elif action == "replace":
                replace_id_str = request.form.get(f"replace_{safe_name}")
                if replace_id_str:
                    replace_id = int(replace_id_str)
                    # Map this name directly to the replacement ID
                    name_to_id[name] = replace_id
                    print(f"[CONFIRM_PLAYERS] Mapping '{name}' to replacement ID {replace_id}", file=sys.stderr)
                    # If there's an existing player with this name, mark it for deletion
                    current = Player.query.filter_by(name=name).first()
                    if current and current.id != replace_id:
                        old_players_to_delete.append(current.id)
                        print(f"[CONFIRM_PLAYERS] Marking old player '{name}' (ID {current.id}) for deletion", file=sys.stderr)
                else:
                    player = Player.query.filter_by(name=name).first()
                    if player:
                        name_to_id[name] = player.id
                        print(f"[CONFIRM_PLAYERS] No replacement specified, using existing ID {player.id}", file=sys.stderr)

            else:
                player = Player.query.filter_by(name=name).first()
                if player:
                    name_to_id[name] = player.id
                    print(f"[CONFIRM_PLAYERS] Default action for '{name}', using ID {player.id}", file=sys.stderr)
    
    # Second pass: apply the name-to-id mapping to all matches
    for m in parsed_matches:
        for role in ["player", "opponent"]:
            name = m.get(role)
            if name and name in name_to_id:
                m[role] = name_to_id[name]

    # Collect final IDs from parsed matches
    final_ids = {
        pid for m in parsed_matches
        for pid in [m.get("player"), m.get("opponent")]
        if isinstance(pid, int)
    }
    
    print(f"\n[CONFIRM_PLAYERS] name_to_id mapping: {name_to_id}", file=sys.stderr)
    print(f"[CONFIRM_PLAYERS] final_ids ({len(final_ids)} players): {sorted(final_ids)}", file=sys.stderr)
    print(f"[CONFIRM_PLAYERS] old_players_to_delete: {old_players_to_delete}\n", file=sys.stderr)

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

        # If IDs weren't resolved, skip this match (shouldn't happen with proper mapping)
        if p1_id is None:
            continue

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
    
    print(f"[CONFIRM_PLAYERS] match_ids from created matches: {sorted(match_ids)}", file=sys.stderr)
    
    for pid in match_ids:
        if not TournamentPlayer.query.filter_by(tournament_id=tournament.id, player_id=pid).first():
            db.session.add(TournamentPlayer(tournament_id=tournament.id, player_id=pid))
    db.session.commit()
    
    final_tp_count = TournamentPlayer.query.filter_by(tournament_id=tournament.id).count()
    print(f"[CONFIRM_PLAYERS] Final TournamentPlayer count: {final_tp_count}", file=sys.stderr)
    
    # Delete any old player records that were replaced (they have no references now)
    for old_player_id in old_players_to_delete:
        old_player = Player.query.get(old_player_id)
        if old_player:
            # Make sure this player has no tournament participations, matches, or decks
            # (they shouldn't since we used the replacement ID everywhere)
            has_tournaments = TournamentPlayer.query.filter_by(player_id=old_player_id).first()
            has_matches = Match.query.filter(
                (Match.player1_id == old_player_id) | (Match.player2_id == old_player_id)
            ).first()
            has_decks = Deck.query.filter_by(player_id=old_player_id).first()
            
            if not has_tournaments and not has_matches and not has_decks:
                db.session.delete(old_player)
    
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


@app.route('/submit_deck_public/<token>', methods=['GET', 'POST'])
def submit_deck_public(token):
    """Public route for players to submit their decklist via shared link"""
    # Find the submission link by token
    link = DeckSubmissionLink.query.filter_by(submission_token=token).first()
    
    if not link:
        flash("Invalid or expired submission link.", "error")
        return render_template('submit_deck_public.html', submitted=False, player_name="Unknown", tournament={'name': 'Unknown', 'date': datetime.now()}, archetypes=[])
    
    if link.deck_submitted:
        flash("This link has already been used. The decklist has been submitted.", "info")
        return render_template('submit_deck_public.html', submitted=True, player_name=link.player_name, tournament=link.tournament, archetypes=[])
    
    tournament = link.tournament
    
    # Get all deck archetypes for dropdown
    archetypes = db.session.query(Deck.name).distinct().filter(Deck.name.isnot(None), Deck.name != '').order_by(Deck.name).all()
    archetype_names = [name[0] for name in archetypes]
    
    if request.method == 'POST':
        deck_name = html.escape(request.form.get("deck_name", "").strip())
        deck_list = request.form.get("deck_list", "").strip()
        
        if not deck_name or not deck_list:
            flash("Please provide both deck archetype and decklist.", "error")
            return render_template('submit_deck_public.html', submitted=False, player_name=link.player_name, tournament=tournament, archetypes=archetype_names)
        
        # Find or create the player
        player = ensure_player(link.player_name)
        
        if not player:
            flash("Failed to create player record.", "error")
            return render_template('submit_deck_public.html', submitted=False, player_name=link.player_name, tournament=tournament, archetypes=archetype_names)
        
        # Check if archetype exists and has an image
        archetype_image = None
        if deck_name:
            existing_archetype = Deck.query.filter_by(name=deck_name).filter(Deck.image_url.isnot(None)).first()
            if existing_archetype:
                archetype_image = existing_archetype.image_url
        
        # Create or update deck
        deck = Deck.query.filter_by(player_id=player.id, tournament_id=tournament.id).first()
        if deck:
            deck.name = deck_name
            deck.list_text = deck_list
            if not deck.image_url and archetype_image:
                deck.image_url = archetype_image
        else:
            deck = Deck(
                player_id=player.id,
                tournament_id=tournament.id,
                name=deck_name,
                list_text=deck_list,
                image_url=archetype_image
            )
            db.session.add(deck)
        
        # Mark link as used
        link.deck_submitted = True
        db.session.commit()
        
        return render_template('submit_deck_public.html', submitted=True, player_name=link.player_name, tournament=tournament, archetypes=archetype_names)
    
    # GET request - show the form
    return render_template('submit_deck_public.html', submitted=False, player_name=link.player_name, tournament=tournament, archetypes=archetype_names)




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

    # Check if in edit mode (has edit_token in session)
    edit_token_in_session = session.get(f'edit_{tournament_id}')
    is_editing = (edit_token_in_session and tournament.edit_token == edit_token_in_session)

    # 🚨 Security guard: block submission if tournament is finalized/discarded (unless in edit mode)
    if tournament.imported_from_text and not tournament.pending and not is_editing:
        flash("This tournament has already been finalized or discarded. Decks cannot be submitted.", "error")
        return redirect(url_for("players"))

    # === Prepare deck data ===
    user_provided_name = html.escape(request.form.get("deck_name", "").strip())
    deck_list = request.form.get("deck_list", "").strip()  # store raw text

    # Auto-detect archetype from decklist
    detected_archetype, similarity = detect_archetype_from_decklist(deck_list)
    
    # Always use detected archetype (either matched archetype or "Rogue")
    deck_name = detected_archetype
    if similarity > 0:
        print(f"[AUTO-DETECT] Matched deck to '{detected_archetype}' with {similarity:.2%} similarity", file=sys.stderr)
    else:
        print(f"[AUTO-DETECT] No archetype match, classified as 'Rogue'", file=sys.stderr)

    # === Save or update deck ===
    deck = Deck.query.filter_by(player_id=player_id, tournament_id=tournament_id).first()
    if deck:
        old_name = deck.name
        deck.name = deck_name
        deck.list_text = deck_list
        
        if old_name != deck_name:
            print(f"[AUTO-DETECT] Deck re-grouped from '{old_name}' to '{deck_name}'", file=sys.stderr)
        
        # Preserve existing image if archetype has one and current deck doesn't
        if not deck.image_url and deck_name:
            existing_archetype = Deck.query.filter_by(name=deck_name).filter(Deck.image_url.isnot(None)).first()
            if existing_archetype:
                deck.image_url = existing_archetype.image_url
    else:
        # Check if archetype exists and has an image
        archetype_image = None
        if deck_name:
            existing_archetype = Deck.query.filter_by(name=deck_name).filter(Deck.image_url.isnot(None)).first()
            if existing_archetype:
                archetype_image = existing_archetype.image_url
        
        deck = Deck(
            player_id=player_id,
            tournament_id=tournament_id,
            name=deck_name,
            list_text=deck_list,
            image_url=archetype_image
        )
        db.session.add(deck)

    db.session.commit()
    flash("Deck saved!", "success")

    return redirect(url_for('tournament_round', tid=tournament_id, round_num=1))



@app.route("/get_deck_archetypes", methods=["GET"])
@login_required
def get_deck_archetypes():
    """Return a JSON list of unique deck archetype names."""
    archetypes = db.session.query(Deck.name).distinct().filter(Deck.name.isnot(None), Deck.name != '').order_by(Deck.name).all()
    return jsonify([name[0] for name in archetypes])

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

    # Determine states:
    # - pending=True, confirm_token exists: New import awaiting confirmation
    # - pending=False, edit_token exists: Existing tournament being edited
    # - imported_from_text=False: Regular manual tournament (needs creator/admin access)
    is_new_import = tournament.pending and tournament.confirm_token
    is_editing = not tournament.pending and tournament.edit_token
    is_manual_tournament = not tournament.imported_from_text
    
    # DEBUG
    app.logger.info(f"tournament_round: tid={tid}, pending={tournament.pending}, confirm_token={tournament.confirm_token}, edit_token={tournament.edit_token}")
    app.logger.info(f"tournament_round: is_new_import={is_new_import}, is_editing={is_editing}, is_manual={is_manual_tournament}")
    
    # Token guard for new imports awaiting confirmation
    if is_new_import:
        session_key = f"tok_{tid}"
        url_token = request.args.get("token")
        session_token = session.get(session_key)

        if url_token and url_token == tournament.confirm_token:
            session[session_key] = url_token  # bind token to session
        elif not session_token or session_token != tournament.confirm_token:
            flash("Invalid or expired confirmation link.", "error")
            return redirect(url_for('players'))
    
    # Token guard for edit mode
    if is_editing:
        session_key = f"edit_{tid}"
        url_token = request.args.get("edit_token")
        session_token = session.get(session_key)

        # Check session first, then URL token
        if session_token and session_token == tournament.edit_token:
            # Already authenticated via session
            pass
        elif url_token and url_token == tournament.edit_token:
            session[session_key] = url_token
        else:
            flash("Invalid or expired edit link.", "error")
            return redirect(url_for('players'))
        
        # Redirect casual tournaments to their own edit page
        if tournament.casual:
            return redirect(url_for('edit_casual_tournament', tid=tid, edit_token=tournament.edit_token))
    
    # Authorization for manual tournaments (not imported)
    if is_manual_tournament:
        # Casual tournaments should use the casual edit/view page, not round view
        if tournament.casual:
            flash("Casual tournaments don't have round-by-round view.", "error")
            return redirect(url_for('tournament_standings', tid=tid))
        
        # Only the creator or admins can access
        if not current_user.is_authenticated:
            flash("You must be logged in to access this tournament.", "error")
            return redirect(url_for('players'))
        
        can_access = (current_user.is_admin or tournament.user_id == current_user.id)
        if not can_access:
            flash("You don't have permission to access this tournament.", "error")
            return redirect(url_for('players'))
    
    # Block unauthorized access to finalized imported tournaments (not in import/edit mode)
    if tournament.imported_from_text and not is_new_import and not is_editing:
        flash("This tournament has been finalized. View it from the tournaments page.", "error")
        return redirect(url_for('players'))

    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id) \
                          .filter(TournamentPlayer.tournament_id == tid).all()
    existing_matches = Match.query.filter_by(tournament_id=tid, round_num=round_num).all()

    # Build stores list for selector
    stores = []
    valid_store_ids = []
    if current_user.is_authenticated:
        if current_user.is_admin:
            all_stores = Store.query.order_by(Store.name.asc()).all()
            # Filter based on tournament type
            for store in all_stores:
                reset_store_tokens_if_needed(store)
                # In edit mode, allow all stores regardless of tokens
                if is_editing:
                    if tournament.premium and store.premium:
                        stores.append(store)
                        valid_store_ids.append(store.id)
                    elif not tournament.premium:
                        stores.append(store)
                        valid_store_ids.append(store.id)
                else:
                    # Normal mode requires tokens
                    if tournament.premium:
                        # Premium tournaments need premium store with tokens
                        if store.premium and store.premium_tokens > 0:
                            stores.append(store)
                            valid_store_ids.append(store.id)
                    else:
                        # Competitive tournaments need competitive tokens
                        if store.competitive_tokens > 0:
                            stores.append(store)
                            valid_store_ids.append(store.id)
        else:
            user_stores = stores_for_user(current_user)
            for store in user_stores:
                reset_store_tokens_if_needed(store)
                # In edit mode, allow all user stores regardless of tokens
                if is_editing:
                    if tournament.premium and store.premium:
                        stores.append(store)
                        valid_store_ids.append(store.id)
                    elif not tournament.premium:
                        stores.append(store)
                        valid_store_ids.append(store.id)
                else:
                    # Normal mode requires tokens
                    if tournament.premium:
                        if store.premium and store.premium_tokens > 0:
                            stores.append(store)
                            valid_store_ids.append(store.id)
                    else:
                        # Scorekeepers get competitive access even without tokens
                        if store.competitive_tokens > 0 or current_user.is_scorekeeper:
                            stores.append(store)
                            valid_store_ids.append(store.id)

    # Imported tournament preview workflow
    if tournament.imported_from_text:
        all_matches = Match.query.filter_by(tournament_id=tid).all()

        elo_changes = {p.id: 0 for p in players}
        for m in all_matches:
            if m.result == "bye" and m.player1_id and not m.player2_id:
                # Byes award 0 elo change
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

        # Calculate per-round elo changes
        per_round_elo_changes = calculate_per_round_elo_changes(tid, tournament)

        return render_template(
            'round.html',
            players=players,
            round_num=round_num,
            tid=tid,
            matches=existing_matches,
            tournament=tournament,
            standings=standings,
            all_matches=all_matches,
            per_round_elo_changes=per_round_elo_changes,
            confirm_token=session.get(f"tok_{tid}"),
            is_editing=is_editing,
            stores=stores
        )

    # Manual entry workflow
    if request.method == 'POST':
        # If editing, delete existing matches for this round first
        if is_editing:
            Match.query.filter_by(tournament_id=tid, round_num=round_num).delete()
            db.session.commit()
        
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

                # Only update ELO immediately if not in edit mode
                if not is_editing:
                    player1 = Player.query.get(p1_id)
                    player2 = Player.query.get(p2_id)
                    games_a, games_b = score_map
                    update_elo(player1, player2, games_a, games_b, tournament)

        db.session.commit()
        
        if is_editing:
            flash("Round updated! Remember to finalize edits when done.", "success")
        else:
            flash("Round submitted!", "success")
        return redirect(url_for('tournament_round', tid=tid, round_num=round_num))

    # For edit mode, fetch all matches to show round-by-round view
    all_matches = None
    per_round_elo_changes = {}
    if is_editing:
        all_matches = Match.query.filter_by(tournament_id=tid).all()
        per_round_elo_changes = calculate_per_round_elo_changes(tid, tournament)

    return render_template(
        'round.html',
        players=players,
        round_num=round_num,
        tid=tid,
        matches=existing_matches,
        tournament=tournament,
        confirm_token=session.get(f"tok_{tid}"),
        is_editing=is_editing,
        all_matches=all_matches,
        per_round_elo_changes=per_round_elo_changes,
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
            # Byes award 0 elo change
            continue

        if not m.player1_id or not m.player2_id:
            continue
        p1 = Player.query.get(m.player1_id)
        p2 = Player.query.get(m.player2_id)
        if not p1 or not p2:
            continue
        scores = result_to_scores(m.result) or (1, 1)
        update_elo(p1, p2, *scores, tournament)

    # Consume token if tournament has a store assigned
    # Only consume tokens for NEW tournaments (pending=True means first-time finalization)
    if tournament.store_id and tournament.pending:
        store = Store.query.get(tournament.store_id)
        if store:
            if tournament.premium and store.premium_tokens > 0:
                store.premium_tokens -= 1
            elif not tournament.premium and not tournament.casual and store.competitive_tokens > 0:
                store.competitive_tokens -= 1

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
    
    # Migration: Add image_url column to blog_posts if it doesn't exist
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engines['users'])
        
        if 'blog_posts' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('blog_posts')]
            
            if 'image_url' not in columns:
                print("[MIGRATION] Adding image_url column to blog_posts table", file=sys.stderr)
                with db.engines['users'].connect() as conn:
                    conn.execute(db.text('ALTER TABLE blog_posts ADD COLUMN image_url VARCHAR(500)'))
                    conn.commit()
                print("[MIGRATION] image_url column added successfully", file=sys.stderr)
    except Exception as e:
        print(f"[MIGRATION] Error checking/adding image_url column: {e}", file=sys.stderr)

# --- Misc ---
@app.route('/tournaments')
def tournaments_list():
    # Query only confirmed (non-pending) tournaments ordered by date
    tournaments = Tournament.query.filter_by(pending=False).order_by(Tournament.date.desc()).all()

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
        casual_tournaments=casual_tournaments,
        current_user=current_user
    )


@app.route("/tournament/<int:tid>/delete", methods=["POST"])
@login_required
def delete_tournament(tid):
    tournament = Tournament.query.get_or_404(tid)
    
    # Check permissions: admin or tournament creator only
    can_delete = (current_user.is_admin or 
                  tournament.user_id == current_user.id)
    
    if not can_delete:
        flash("You don't have permission to delete this tournament.", "error")
        return redirect(url_for("tournaments_list"))
    
    # Backup tournament data for recovery
    import json
    matches = Match.query.filter_by(tournament_id=tournament.id).all()
    decks = Deck.query.filter_by(tournament_id=tournament.id).all()
    tournament_players = TournamentPlayer.query.filter_by(tournament_id=tournament.id).all()
    
    backup = {
        'tournament': {
            'name': tournament.name,
            'date': tournament.date.isoformat() if tournament.date else None,
            'rounds': tournament.rounds,
            'player_count': tournament.player_count,
            'country': tournament.country,
            'casual': tournament.casual,
            'premium': tournament.premium,
            'store_id': tournament.store_id,
            'top_cut': tournament.top_cut
        },
        'matches': [{
            'round_num': m.round_num,
            'player1_id': m.player1_id,
            'player2_id': m.player2_id,
            'result': m.result
        } for m in matches],
        'decks': [{
            'player_id': d.player_id,
            'name': d.name,
            'list_text': d.list_text,
            'colors': d.colors,
            'image_url': d.image_url
        } for d in decks],
        'players': [tp.player_id for tp in tournament_players]
    }

    # --- Delete dependent records ---
    Match.query.filter_by(tournament_id=tournament.id).delete()
    Deck.query.filter_by(tournament_id=tournament.id).delete()
    TournamentPlayer.query.filter_by(tournament_id=tournament.id).delete()
    CasualPointsHistory.query.filter_by(tournament_id=tournament.id).delete()

    # Delete the tournament itself
    tournament_name = tournament.name
    db.session.delete(tournament)
    db.session.commit()
    
    # Log event with backup
    log_event(
        action_type='tournament_deleted',
        details=f"Deleted tournament: {tournament_name}",
        backup_data=json.dumps(backup),
        recoverable=True
    )

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
        
        # Helper: award casual points based on rules
        def award_casual_points_by_rank(num_players: int, rank: int) -> int:
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
        
        # Get player count from tournament
        players_in_tournament = TournamentPlayer.query.filter_by(tournament_id=tournament.id).count()

        for rank in range(1, top_cut+1):
            player_id = request.form.get(f"player_{rank}")
            deck_name = request.form.get(f"deck_name_{rank}")
            deck_list = request.form.get(f"deck_list_{rank}")
            if player_id:
                player = Player.query.get(player_id)
                if player:
                    # Award casual points based on rank and player count
                    pts = award_casual_points_by_rank(players_in_tournament, rank)
                    if pts > 0:
                        # Update player's total points
                        player.casual_points = (player.casual_points or 0) + pts
                        
                        # Create history record for per-store tracking
                        history_record = CasualPointsHistory(
                            player_id=player.id,
                            tournament_id=tournament.id,
                            store_id=tournament.store_id,
                            points=pts,
                            rank=rank,
                            awarded_at=datetime.utcnow()
                        )
                        db.session.add(history_record)
                    
                    # Save deck if provided
                    if deck_name and deck_list:
                        deck = Deck.query.filter_by(player_id=player_id, tournament_id=tournament.id).first()
                        if deck:
                            deck.name = deck_name
                            deck.list_text = deck_list
                        else:
                            deck = Deck(player_id=player_id, tournament_id=tournament.id,
                                        name=deck_name, list_text=deck_list)
                            db.session.add(deck)
        
        db.session.commit()
        
        # Log event
        log_event(
            action_type='tournament_finalized',
            details=f"Finalized Casual tournament: {tournament.name}",
            recoverable=False
        )
        
        flash("Casual tournament final standings saved!", "success")
        return redirect(url_for('players'))

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
        
        # For casual tournaments, get casual points from history
        casual_pts = 0
        if tournament.casual:
            history = CasualPointsHistory.query.filter_by(
                tournament_id=tid,
                player_id=p.id
            ).first()
            if history:
                casual_pts = history.points
        
        standings.append({
            "player": p,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "points": casual_pts if tournament.casual else points,
            "elo_delta": elo_changes[p.id],
            "deck": Deck.query.filter_by(player_id=p.id, tournament_id=tid).first()
        })

    standings.sort(key=lambda s: (s["points"], s["wins"]), reverse=True)
    
    # Check if current user can edit this tournament
    can_edit = False
    if current_user.is_authenticated:
        can_edit = (current_user.is_admin or 
                   tournament.user_id == current_user.id)
    
    # Get the user who submitted the tournament
    submitted_by = None
    if tournament.user_id:
        submitted_by = User.query.get(tournament.user_id)
    
    # Get deck submission links for this tournament
    deck_links = DeckSubmissionLink.query.filter_by(tournament_id=tid).all()
    deck_links_by_player = {link.player_name: link for link in deck_links}
    
    # Get model decklists for uniqueness calculation
    model_decklists = {}
    for s in standings:
        if s["deck"] and s["deck"].name:
            archetype_name = s["deck"].name
            if archetype_name not in model_decklists:
                model = ArchetypeModel.query.filter_by(archetype_name=archetype_name).first()
                model_decklists[archetype_name] = model.model_decklist if model else None
    
    return render_template('tournament_standings.html',
                           tournament=tournament,
                           standings=standings,
                           can_edit=can_edit,
                           submitted_by=submitted_by,
                           deck_links_by_player=deck_links_by_player,
                           model_decklists=model_decklists)




@app.route('/tournament/<int:tid>/edit', methods=['POST'])
@login_required
def edit_tournament(tid):
    """Generate edit token and redirect to appropriate editing interface"""
    tournament = Tournament.query.get_or_404(tid)
    
    # DEBUG: Print tournament properties
    print(f"\n=== EDIT TOURNAMENT DEBUG ===", file=sys.stderr)
    print(f"Tournament ID: {tid}", file=sys.stderr)
    print(f"  casual: {tournament.casual}", file=sys.stderr)
    print(f"  imported_from_text: {tournament.imported_from_text}", file=sys.stderr)
    print(f"  pending: {tournament.pending}", file=sys.stderr)
    print(f"============================\n", file=sys.stderr)
    
    # Check permissions
    can_edit = (current_user.is_admin or 
                tournament.user_id == current_user.id)
    
    if not can_edit:
        flash("You don't have permission to edit this tournament.", "error")
        return redirect(url_for('tournament_standings', tid=tid))
    
    # Don't allow editing if still pending confirmation
    if tournament.pending:
        flash("This tournament hasn't been confirmed yet. Please confirm it first.", "error")
        return redirect(url_for('tournament_standings', tid=tid))
    
    # Generate edit token (separate from confirm_token used for new tournaments)
    tournament.edit_token = secrets.token_urlsafe(16)
    db.session.commit()
    
    # Store edit token in session
    session[f"edit_{tid}"] = tournament.edit_token
    
    # Redirect based on tournament type
    # Manual tournaments should always be casual (manual competitive tournaments are deprecated)
    if not tournament.imported_from_text:
        # This is a manual tournament - should be casual
        if not tournament.casual:
            # Fix legacy data: mark as casual
            print(f"WARNING: Tournament {tid} is manual but not marked casual - fixing", file=sys.stderr)
            tournament.casual = True
            db.session.commit()
        
        print(f"Redirecting to edit_casual_tournament", file=sys.stderr)
        flash("Casual tournament is now in edit mode. Update the final standings.", "success")
        return redirect(url_for('edit_casual_tournament', tid=tid, edit_token=tournament.edit_token))
    else:
        # This is an imported tournament - use round-by-round editing
        print(f"Redirecting to tournament_round", file=sys.stderr)
        flash("Tournament is now in edit mode. Click players to change match results.", "success")
        return redirect(url_for('tournament_round', tid=tid, round_num=1, edit_token=tournament.edit_token))


@app.route('/tournament/<int:tid>/update_match_results', methods=['POST'])
@login_required
def update_match_results(tid):
    """Update match results during tournament editing"""
    tournament = Tournament.query.get_or_404(tid)
    
    # Check permissions and editing state
    can_edit = (current_user.is_admin or 
                tournament.user_id == current_user.id)
    
    if not can_edit:
        return jsonify({'success': False, 'error': 'Permission denied'})
    
    # Check if tournament is in edit mode
    if not tournament.edit_token:
        return jsonify({'success': False, 'error': 'Tournament is not in edit mode'})
    
    # Verify edit token from session
    session_key = f"edit_{tid}"
    if not session.get(session_key) or session.get(session_key) != tournament.edit_token:
        return jsonify({'success': False, 'error': 'Invalid edit session'})
    
    # Get changes from request
    data = request.get_json()
    changes = data.get('changes', {})
    
    # Update each match
    for match_id_str, winner_num in changes.items():
        match_id = int(match_id_str)
        match = Match.query.get(match_id)
        
        if not match or match.tournament_id != tid:
            continue
        
        # Update result based on winner selection
        # Assume 2-0 for simplicity, you can enhance this later
        if winner_num == 1:
            match.result = '2-0'
        elif winner_num == 2:
            match.result = '0-2'
        else:
            match.result = '1-1'  # Draw
    
    db.session.commit()
    
    return jsonify({'success': True})



@app.route('/tournament/<int:tid>/update_metadata', methods=['POST'])
def update_tournament_metadata(tid):
    """Update tournament name and date during import confirmation, edit mode, or for manual tournaments"""
    tournament = Tournament.query.get_or_404(tid)
    
    # Determine states
    is_new_import = tournament.pending and tournament.confirm_token
    is_editing = not tournament.pending and tournament.edit_token
    is_manual_tournament = not tournament.imported_from_text
    
    # Check if any valid access mode
    if not (is_new_import or is_editing or is_manual_tournament):
        flash("Tournament metadata can only be updated during import confirmation or edit mode.", "error")
        return redirect(url_for('players'))
    
    # Verify token for new imports
    if is_new_import:
        session_key = f"tok_{tid}"
        session_token = session.get(session_key)
        if not session_token or session_token != tournament.confirm_token:
            flash("Invalid or expired confirmation link.", "error")
            return redirect(url_for('players'))
    
    # Verify token for edit mode
    if is_editing:
        session_key = f"edit_{tid}"
        session_token = session.get(session_key)
        if not session_token or session_token != tournament.edit_token:
            flash("Invalid or expired edit link.", "error")
            return redirect(url_for('players'))
    
    # Authorization for manual tournaments
    if is_manual_tournament:
        if not current_user.is_authenticated:
            flash("You must be logged in to update this tournament.", "error")
            return redirect(url_for('players'))
        
        can_access = (current_user.is_admin or tournament.user_id == current_user.id)
        if not can_access:
            flash("You don't have permission to modify this tournament.", "error")
            return redirect(url_for('players'))
    
    # Update tournament metadata
    tournament_name = request.form.get('tournament_name', '').strip()
    tournament_date = request.form.get('tournament_date', '').strip()
    
    if tournament_name:
        tournament.name = tournament_name
    
    if tournament_date:
        # Convert string date (YYYY-MM-DD) to Python date object
        from datetime import datetime
        try:
            date_obj = datetime.strptime(tournament_date, '%Y-%m-%d').date()
            tournament.date = date_obj
        except ValueError:
            flash("Invalid date format.", "error")
            round_num = request.form.get('current_round', 1)
            if is_new_import:
                return redirect(url_for('tournament_round', tid=tid, round_num=round_num, token=tournament.confirm_token))
            elif is_editing:
                return redirect(url_for('tournament_round', tid=tid, round_num=round_num, edit_token=tournament.edit_token))
            else:  # manual tournament
                return redirect(url_for('tournament_round', tid=tid, round_num=round_num))
    
    db.session.flush()
    db.session.commit()
    
    flash("Tournament information updated successfully.", "success")
    
    # Redirect back to the current round view with appropriate token/mode
    round_num = request.form.get('current_round', 1)
    if is_new_import:
        return redirect(url_for('tournament_round', tid=tid, round_num=round_num, token=tournament.confirm_token))
    elif is_editing:
        return redirect(url_for('tournament_round', tid=tid, round_num=round_num, edit_token=tournament.edit_token))
    else:  # manual tournament
        return redirect(url_for('tournament_round', tid=tid, round_num=round_num))


@app.route('/tournament/<int:tid>/finalize_edit', methods=['POST'])
@login_required
def finalize_tournament_edit(tid):
    """Finalize tournament edits and recalculate all ELO from this point forward"""
    tournament = Tournament.query.get_or_404(tid)
    
    # Check permissions
    can_edit = (current_user.is_admin or 
                tournament.user_id == current_user.id)
    
    if not can_edit:
        flash("You don't have permission to finalize this tournament.", "error")
        return redirect(url_for('tournament_standings', tid=tid))
    
    # Clear the edit token to exit edit mode
    tournament.edit_token = None
    
    # Recalculate ELO for all tournaments from this timestamp forward
    recalculate_elo_from_timestamp(tournament.submitted_at)
    
    db.session.commit()
    
    flash("Tournament edits saved. ELO has been recalculated for all affected players.", "success")
    return redirect(url_for('tournament_standings', tid=tid))


def recalculate_elo_from_timestamp(from_timestamp):
    """Recalculate ELO for all players from a given timestamp forward"""
    # Get all tournaments from this point forward, ordered by submission time
    tournaments = Tournament.query.filter(
        Tournament.submitted_at >= from_timestamp,
        Tournament.pending == False
    ).order_by(Tournament.submitted_at.asc()).all()
    
    # Get all players
    all_players = Player.query.all()
    
    # Reset ELO to default for players involved in these tournaments
    affected_player_ids = set()
    for t in tournaments:
        player_ids = db.session.query(TournamentPlayer.player_id).filter_by(tournament_id=t.id).all()
        affected_player_ids.update([pid[0] for pid in player_ids])
    
    # Get tournaments before this timestamp to calculate starting ELO
    earlier_tournaments = Tournament.query.filter(
        Tournament.submitted_at < from_timestamp,
        Tournament.pending == False
    ).order_by(Tournament.submitted_at.asc()).all()
    
    # Reset affected players to default
    for player_id in affected_player_ids:
        player = Player.query.get(player_id)
        if player:
            player.elo = DEFAULT_ELO
            player.casual_elo = DEFAULT_ELO
    
    # Replay earlier tournaments to get correct starting ELO
    for t in earlier_tournaments:
        matches = Match.query.filter_by(tournament_id=t.id).order_by(Match.round_num).all()
        for m in matches:
            if m.result == "bye" or not m.player1_id or not m.player2_id:
                continue
            p1 = Player.query.get(m.player1_id)
            p2 = Player.query.get(m.player2_id)
            if not p1 or not p2:
                continue
            scores = result_to_scores(m.result) or (1, 1)
            update_elo(p1, p2, *scores, t)
    
    # Now replay tournaments from the edited timestamp forward
    for t in tournaments:
        matches = Match.query.filter_by(tournament_id=t.id).order_by(Match.round_num).all()
        for m in matches:
            if m.result == "bye" or not m.player1_id or not m.player2_id:
                continue
            p1 = Player.query.get(m.player1_id)
            p2 = Player.query.get(m.player2_id)
            if not p1 or not p2:
                continue
            scores = result_to_scores(m.result) or (1, 1)
            update_elo(p1, p2, *scores, t)


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

# --- TEMPORARY: Populate demo database ---
@app.route('/populate_demo_data')
@login_required
def populate_demo_route():
    if not current_user.is_admin:
        flash("Admin access required", "danger")
        return redirect(url_for('home'))
    
    # Show which databases we're using
    current_db_path = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
    current_users_path = app.config['SQLALCHEMY_BINDS']['users'].replace('sqlite:///', '')
    
    print(f"Using tournament DB: {current_db_path}", file=sys.stderr)
    print(f"Using users DB: {current_users_path}", file=sys.stderr)
    
    try:
        # Inline population to avoid import issues
        from datetime import timedelta
        import random
        
        PLAYER_NAMES = [
            "Alice Martinez", "Bob Johnson", "Carol Lee", "David Chen", "Emma Rodriguez",
            "Frank Wilson", "Grace Kim", "Henry Taylor", "Isabel Garcia", "Jack Anderson",
            "Kate Brown", "Liam Murphy", "Maya Patel", "Noah Davis", "Olivia White",
            "Peter Zhang", "Quinn O'Brien", "Rachel Singh", "Sam Mitchell", "Tina Lopez",
            "Uma Sharma", "Victor Nguyen", "Wendy Park", "Xavier Scott", "Yuki Tanaka",
            "Zoe Martin", "Alex Cooper", "Blake Reed", "Chloe Brooks", "Dylan Hayes"
        ]
        
        DECK_ARCHETYPES = [
            ("Mono-Red Aggro", "R"), ("Azorius Control", "WU"), ("Golgari Midrange", "BG"),
            ("Izzet Phoenix", "UR"), ("Rakdos Sacrifice", "BR"), ("Selesnya Tokens", "GW"),
            ("Dimir Control", "UB"), ("Gruul Stompy", "RG"), ("Orzhov Midrange", "WB"),
            ("Temur Energy", "URG"), ("Esper Control", "WUB"), ("Jund Midrange", "BRG"),
        ]
        
        STORE_NAMES = [
            ("Game Haven", "New York", "US"), ("Magic Emporium", "Los Angeles", "US"),
            ("Card Kingdom", "Seattle", "US"), ("Mana Source", "London", "GB"),
        ]
        
        COUNTRIES = ["US", "GB", "CA", "AU", "ES", "FR", "DE", "JP"]
        
        # Create stores
        stores = []
        for name, location, country in STORE_NAMES:
            store = Store.query.filter_by(name=name).first()
            if not store:
                store = Store(name=name, location=location, country=country, premium=random.choice([True, False]))
                db.session.add(store)
                stores.append(store)
        db.session.commit()
        stores = Store.query.all()
        
        # Create players
        players = []
        for name in PLAYER_NAMES:
            player = Player.query.filter_by(name=name).first()
            if not player:
                player = Player(name=name, elo=random.randint(800, 1400), country=random.choice(COUNTRIES), casual_points=random.randint(0, 50))
                db.session.add(player)
                players.append(player)
        db.session.commit()
        players = Player.query.all()
        
        # Create tournaments
        base_date = datetime.now() - timedelta(days=180)
        for i in range(10):
            tournament_date = base_date + timedelta(days=random.randint(0, 180))
            num_players = random.choice([8, 12, 16])
            rounds = 4 if num_players <= 16 else 5
            
            tournament = Tournament(
                name=f"Tournament #{i+1}",
                date=tournament_date.date(),
                rounds=rounds,
                player_count=num_players,
                country=random.choice(COUNTRIES),
                casual=random.choice([True, False]),
                premium=False,
                pending=False,
                store_id=random.choice(stores).id if stores else None
            )
            db.session.add(tournament)
            db.session.flush()
            
            # Add players to tournament
            tournament_players = random.sample(players, min(num_players, len(players)))
            for player in tournament_players:
                tp = TournamentPlayer(tournament_id=tournament.id, player_id=player.id)
                db.session.add(tp)
                
                # Add deck
                archetype = random.choice(DECK_ARCHETYPES)
                deck = Deck(player_id=player.id, tournament_id=tournament.id, name=archetype[0], colors=archetype[1])
                db.session.add(deck)
            
            # Create matches
            for round_num in range(1, rounds + 1):
                shuffled = tournament_players.copy()
                random.shuffle(shuffled)
                for j in range(0, len(shuffled), 2):
                    if j + 1 < len(shuffled):
                        result = random.choices(["2-0", "0-2", "1-1"], weights=[45, 45, 10])[0]
                        match = Match(tournament_id=tournament.id, round_num=round_num, player1_id=shuffled[j].id, player2_id=shuffled[j+1].id, result=result)
                    else:
                        match = Match(tournament_id=tournament.id, round_num=round_num, player1_id=shuffled[j].id, player2_id=None, result="bye")
                    db.session.add(match)
        
        db.session.commit()
        
        # Get counts
        tournament_count = Tournament.query.count()
        player_count = Player.query.count()
        store_count = Store.query.count()
        match_count = Match.query.count()
        
        flash(f"Successfully created {store_count} stores, {player_count} players, {tournament_count} tournaments, and {match_count} matches!", "success")
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(error_trace, file=sys.stderr)
        flash(f"Error populating demo data: {str(e)}", "danger")
    
    return redirect(url_for('tournaments_list'))

# --- Run locally ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))