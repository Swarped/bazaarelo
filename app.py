import io
from datetime import datetime
from typing import List

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy

# --- Flask app setup ---
app = Flask(__name__)
app.secret_key = "replace-with-secure-secret"  # use env var in production

# --- Database setup ---
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///tournaments.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


# --- Models ---
class Tournament(db.Model):
    __tablename__ = "tournament"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    event_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    rounds = db.relationship(
        "Round",
        backref="tournament",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="Round.round_number.asc()"
    )


class Round(db.Model):
    __tablename__ = "round"

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournament.id"), nullable=False)
    round_number = db.Column(db.Integer, nullable=False)
    raw_text = db.Column(db.Text, nullable=False)


# --- Helpers ---
def split_rounds_from_text(all_text: str) -> List[str]:
    """
    Split pasted text into rounds. Each round starts with a line beginning 'Round '.
    Returns a list of round contents.
    """
    rounds = []
    buffer = []
    current_round = None

    for line in all_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Round "):
            if current_round and buffer:
                rounds.append("\n".join(buffer).strip())
                buffer = []
            current_round = stripped
        elif current_round:
            buffer.append(line)

    if current_round and buffer:
        rounds.append("\n".join(buffer).strip())

    return rounds

# --- Routes ---

@app.route('/')
def index():
    """Homepage: list all tournaments and provide link to add new one."""
    tournaments = Tournament.query.order_by(Tournament.created_at.desc()).all()
    return render_template('index.html', tournaments=tournaments)


@app.route('/tournament/new', methods=['GET', 'POST'])
def new_tournament():
    """
    Create a new tournament.
    Option A: Manual entry (name + date + players).
    Option B: Paste all rounds text.
    """
    if request.method == 'POST':
        name = request.form.get('name')
        date_str = request.form.get('date')
        pasted_text = request.form.get('rounds_text')

        if not name:
            flash("Tournament name is required.", "error")
            return redirect(url_for('new_tournament'))

        # Parse date if provided
        event_date = None
        if date_str:
            try:
                event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid date format.", "error")

        # Create tournament record
        tournament = Tournament(name=name, event_date=event_date)
        db.session.add(tournament)
        db.session.commit()

        # If pasted text provided, split into rounds
        if pasted_text and pasted_text.strip():
            rounds = split_rounds_from_text(pasted_text)
            for idx, content in enumerate(rounds, start=1):
                r = Round(tournament_id=tournament.id, round_number=idx, raw_text=content)
                db.session.add(r)
            db.session.commit()

        flash("Tournament created successfully!", "success")
        return redirect(url_for('view_tournament', tournament_id=tournament.id))

    return render_template('new_tournament.html')


@app.route('/tournament/<int:tournament_id>')
def view_tournament(tournament_id):
    """View details of a single tournament, including its rounds."""
    tournament = Tournament.query.get_or_404(tournament_id)
    return render_template('view_tournament.html', tournament=tournament)

# --- Database initialization ---
with app.app_context():
    db.create_all()


# --- Run the app ---
if __name__ == "__main__":
    # For local development; Render will use gunicorn
    app.run(debug=True, host="0.0.0.0", port=5000)
