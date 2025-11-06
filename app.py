from flask import Flask, request, render_template, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
import os
from datetime import datetime

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///test.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Models ---
class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    elo = db.Column(db.Integer, default=1400)

class Tournament(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    rounds = db.Column(db.Integer, nullable=False)

class TournamentPlayer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)

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
        player_names = request.form.get('players').splitlines()
        player_names = [name.strip() for name in player_names if name.strip()]

        # Ensure players exist
        player_objs = []
        for name in player_names:
            player = Player.query.filter_by(name=name).first()
            if not player:
                player = Player(name=name, elo=1400)
                db.session.add(player)
                db.session.commit()
            player_objs.append(player)

        # Calculate rounds
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
            rounds = 7  # expand later

        # Save tournament
        tournament = Tournament(date=datetime.strptime(date_str, "%Y-%m-%d"), rounds=rounds)
        db.session.add(tournament)
        db.session.commit()

        # Link players
        for p in player_objs:
            tp = TournamentPlayer(tournament_id=tournament.id, player_id=p.id)
            db.session.add(tp)
        db.session.commit()

        return f"Tournament created with {num_players} players and {rounds} rounds!"

    return render_template('new_tournament.html')

# --- Ensure DB tables exist ---
with app.app_context():
    db.create_all()

# --- Run locally ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
