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

class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    round_num = db.Column(db.Integer, nullable=False)
    player1_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)
    player2_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)
    result = db.Column(db.String(10))  # e.g. "1-0", "2-1", "1-1"

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
            rounds = 7

        # Save tournament
        tournament = Tournament(date=datetime.strptime(date_str, "%Y-%m-%d"), rounds=rounds)
        db.session.add(tournament)
        db.session.commit()

        # Link players
        for p in player_objs:
            tp = TournamentPlayer(tournament_id=tournament.id, player_id=p.id)
            db.session.add(tp)
        db.session.commit()

        return redirect(url_for('tournament_round', tid=tournament.id, round_num=1))

    return render_template('new_tournament.html')

@app.route('/tournament/<int:tid>/round/<int:round_num>', methods=['GET', 'POST'])
def tournament_round(tid, round_num):
    tournament = Tournament.query.get_or_404(tid)
    players = Player.query.join(TournamentPlayer, Player.id == TournamentPlayer.player_id)\
                          .filter(TournamentPlayer.tournament_id == tid).all()

    if request.method == 'POST':
        matches = []
        for i in range(1, len(players)//2 + 1):
            p1_id = int(request.form.get(f'player1_{i}'))
            p2_id = int(request.form.get(f'player2_{i}'))
            result = request.form.get(f'result_{i}')

            match = Match(tournament_id=tid, round_num=round_num,
                          player1_id=p1_id, player2_id=p2_id, result=result)
            db.session.add(match)

            # Update Elo
            player1 = Player.query.get(p1_id)
            player2 = Player.query.get(p2_id)

            if result == "1-0":
                update_elo(player1, player2, 1, 0)
            elif result == "0-1":
                update_elo(player1, player2, 0, 1)
            elif result == "1-1":
                update_elo(player1, player2, 1, 1)
            elif result == "2-1":
                update_elo(player1, player2, 2, 1)
            elif result == "1-2":
                update_elo(player1, player2, 1, 2)

        db.session.commit()

        # Move to next round or finish
        if round_num < tournament.rounds:
            return redirect(url_for('tournament_round', tid=tid, round_num=round_num+1))
        else:
            return redirect(url_for('players'))

    return render_template('round.html', players=players, round_num=round_num, tid=tid)

# --- Ensure DB tables exist ---
with app.app_context():
    db.create_all()

# --- Run locally ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
