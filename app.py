import io
from datetime import datetime
from typing import Dict, List

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
def read_rounds_from_txt(file_stream: io.BytesIO) -> Dict[str, str]:
    """
    Read a plain text file with all rounds pasted in, split by 'Round' headers.
    Returns a dict: { "Round 1": "...", "Round 2": "...", ... }.
    """
    file_stream.seek(0)
    text = file_stream.read().decode("utf-8", errors="replace")

    rounds: Dict[str, str] = {}
    current_round: str = None
    buffer: List[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Round "):
            if current_round and buffer:
                rounds[current_round] = "\n".join(buffer).strip()
                buffer = []
            current_round = stripped
        elif current_round:
            buffer.append(line)

    if current_round and buffer:
        rounds[current_round] = "\n".join(buffer).strip()

    return rounds


def extract_event_date_from_txt(file_stream: io.BytesIO):
    """
    Scan the TXT for an 'Event Date:' line and parse it.
    Supports US and EU day/month order.
    """
    file_stream.seek(0)
    text = file_stream.read().decode("utf-8", errors="replace")

    for line in text.splitlines():
        if "Event Date:" in line:
            date_str = line.split("Event Date:", 1)[1].strip()
            for fmt in ("%m/%d/%Y", "%d/%m/%Y"):
                try:
                    return datetime.strptime(date_str, fmt).date()
                except ValueError:
                    continue
    return None
# --- Routes ---

@app.route('/')
def index():
    """Homepage: list all tournaments."""
    tournaments = Tournament.query.order_by(Tournament.created_at.desc()).all()
    return render_template('index.html', tournaments=tournaments)


@app.route('/tournament/new', methods=['GET', 'POST'])
def new_tournament():
    """Form to create a new tournament by uploading a TXT file."""
    if request.method == 'POST':
        name = request.form.get('name')
        file = request.files.get('txt')

        if not name or not file:
            flash("Please provide a tournament name and upload a TXT file.", "error")
            return redirect(url_for('new_tournament'))

        # Read file into memory
        data = io.BytesIO(file.read())

        # Extract event date
        event_date = extract_event_date_from_txt(io.BytesIO(data.getvalue()))

        # Create tournament record
        tournament = Tournament(name=name, event_date=event_date)
        db.session.add(tournament)
        db.session.commit()

        # Parse rounds and save them
        rounds = read_rounds_from_txt(io.BytesIO(data.getvalue()))
        for idx, (round_name, content) in enumerate(rounds.items(), start=1):
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


@app.route('/tournament/debug_txt', methods=['POST'])
def debug_txt():
    """Upload a TXT file and preview how rounds are parsed."""
    file = request.files.get('txt')
    if not file or file.filename == "":
        flash("Please upload a TXT file.", "error")
        return redirect(url_for('new_tournament'))

    data = io.BytesIO(file.read())
    raw_rounds = {}
    try:
        raw_rounds = read_rounds_from_txt(data)
    except Exception as e:
        app.logger.error(f"TXT parsing error: {e}")

    return render_template('debug_txt.html', raw_rounds=raw_rounds)
# --- Database initialization ---
@app.before_first_request
def create_tables():
    """Ensure database tables exist before handling the first request."""
    db.create_all()


# --- Run the app ---
if __name__ == "__main__":
    # For local development; Render will use gunicorn
    app.run(debug=True, host="0.0.0.0", port=5000)
