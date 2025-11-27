"""
Temporary route to populate tournaments_demo.db with sample data.
Access this route once, then remove it from app.py.
"""
from datetime import datetime, timedelta
import random

def populate_demo_data(app, db, Player, Tournament, TournamentPlayer, Match, Deck, Store, DEFAULT_ELO):
    """Populate demo database with sample tournaments, players, matches, and decks."""
    
    with app.app_context():
        # First, create all tables if they don't exist
        print("Creating database tables...")
        db.create_all()
        db.create_all(bind_key='users')
        
        # Sample data
        PLAYER_NAMES = [
        "Alice Martinez", "Bob Johnson", "Carol Lee", "David Chen", "Emma Rodriguez",
        "Frank Wilson", "Grace Kim", "Henry Taylor", "Isabel Garcia", "Jack Anderson",
        "Kate Brown", "Liam Murphy", "Maya Patel", "Noah Davis", "Olivia White",
        "Peter Zhang", "Quinn O'Brien", "Rachel Singh", "Sam Mitchell", "Tina Lopez",
        "Uma Sharma", "Victor Nguyen", "Wendy Park", "Xavier Scott", "Yuki Tanaka",
        "Zoe Martin", "Alex Cooper", "Blake Reed", "Chloe Brooks", "Dylan Hayes"
    ]
    
    DECK_ARCHETYPES = [
        ("Mono-Red Aggro", "R", "uploads/red_aggro.jpg"),
        ("Azorius Control", "WU", "uploads/azorius_control.jpg"),
        ("Golgari Midrange", "BG", "uploads/golgari_mid.jpg"),
        ("Izzet Phoenix", "UR", "uploads/izzet_phoenix.jpg"),
        ("Rakdos Sacrifice", "BR", "uploads/rakdos_sac.jpg"),
        ("Selesnya Tokens", "GW", "uploads/selesnya_tokens.jpg"),
        ("Dimir Control", "UB", "uploads/dimir_control.jpg"),
        ("Gruul Stompy", "RG", "uploads/gruul_stompy.jpg"),
        ("Orzhov Midrange", "WB", "uploads/orzhov_mid.jpg"),
        ("Temur Energy", "URG", "uploads/temur_energy.jpg"),
        ("Esper Control", "WUB", "uploads/esper_control.jpg"),
        ("Jund Midrange", "BRG", "uploads/jund_mid.jpg"),
        ("Bant Spirits", "GWU", "uploads/bant_spirits.jpg"),
        ("Mono-Blue Tempo", "U", "uploads/mono_blue.jpg"),
        ("Mono-Black Devotion", "B", "uploads/mono_black.jpg"),
    ]
    
    STORE_NAMES = [
        ("Game Haven", "New York", "US"),
        ("Magic Emporium", "Los Angeles", "US"),
        ("Card Kingdom", "Seattle", "US"),
        ("Mana Source", "London", "GB"),
        ("Spell Slinger", "Toronto", "CA"),
        ("The Gathering Place", "Melbourne", "AU"),
    ]
    
    COUNTRIES = ["US", "GB", "CA", "AU", "ES", "FR", "DE", "JP", "BR"]
    
    print("Creating stores...")
    stores = []
    for name, location, country in STORE_NAMES:
        store = Store.query.filter_by(name=name).first()
        if not store:
            store = Store(
                name=name,
                location=location,
                country=country,
                premium=random.choice([True, False])
            )
            db.session.add(store)
            stores.append(store)
    db.session.commit()
    stores = Store.query.all()  # Refresh to get all stores
    print(f"Created {len(stores)} stores")
    
    print("Creating players...")
    players = []
    for name in PLAYER_NAMES:
        player = Player.query.filter_by(name=name).first()
        if not player:
            # Random ELO between 800 and 1400
            elo = random.randint(800, 1400)
            player = Player(
                name=name,
                elo=elo,
                country=random.choice(COUNTRIES),
                casual_points=random.randint(0, 50)
            )
            db.session.add(player)
            players.append(player)
    db.session.commit()
    players = Player.query.all()  # Refresh to get all players
    
    print(f"Created {len(players)} players")
    
    # Create tournaments over the past 6 months
    print("Creating tournaments...")
    base_date = datetime.now() - timedelta(days=180)
    
    for i in range(15):  # Create 15 tournaments
        tournament_date = base_date + timedelta(days=random.randint(0, 180))
        num_players = random.choice([8, 12, 16, 24, 32])
        rounds = 4 if num_players <= 16 else 5
        is_casual = random.choice([True, False])
        is_premium = random.choice([True, False]) if not is_casual else False
        
        tournament = Tournament(
            name=f"{"Casual" if is_casual else "Competitive"} Tournament #{i+1}",
            date=tournament_date.date(),
            rounds=rounds,
            player_count=num_players,
            country=random.choice(COUNTRIES),
            casual=is_casual,
            premium=is_premium,
            pending=False,
            store_id=random.choice(stores).id if stores else None,
            top_cut=8 if num_players >= 16 and not is_casual else None
        )
        db.session.add(tournament)
        db.session.flush()  # Get tournament ID
        
        # Select random players for this tournament
        tournament_players = random.sample(players, min(num_players, len(players)))
        
        # Add tournament players
        for player in tournament_players:
            tp = TournamentPlayer(tournament_id=tournament.id, player_id=player.id)
            db.session.add(tp)
            
            # Assign random deck
            archetype = random.choice(DECK_ARCHETYPES)
            deck = Deck(
                player_id=player.id,
                tournament_id=tournament.id,
                name=archetype[0],
                colors=archetype[1],
                image_url=archetype[2],
                list_text=f"// {archetype[0]} decklist\n4 Card Name\n3 Another Card\n// etc..."
            )
            db.session.add(deck)
        
        # Create matches for each round
        for round_num in range(1, rounds + 1):
            # Shuffle players for pairings
            shuffled = tournament_players.copy()
            random.shuffle(shuffled)
            
            # Create pairings
            for j in range(0, len(shuffled), 2):
                if j + 1 < len(shuffled):
                    p1 = shuffled[j]
                    p2 = shuffled[j + 1]
                    
                    # Random result
                    results = ["2-0", "0-2", "1-1"]
                    weights = [45, 45, 10]  # 45% win, 45% loss, 10% draw
                    result = random.choices(results, weights=weights)[0]
                    
                    match = Match(
                        tournament_id=tournament.id,
                        round_num=round_num,
                        player1_id=p1.id,
                        player2_id=p2.id,
                        result=result
                    )
                    db.session.add(match)
                else:
                    # Bye for odd player
                    match = Match(
                        tournament_id=tournament.id,
                        round_num=round_num,
                        player1_id=shuffled[j].id,
                        player2_id=None,
                        result="bye"
                    )
                    db.session.add(match)
        
        print(f"Created tournament {i+1}/15: {tournament.name} with {num_players} players")
    
    db.session.commit()
    print("Demo database populated successfully!")
    
    # Return summary
    final_tournament_count = Tournament.query.count()
    final_player_count = Player.query.count()
    final_store_count = Store.query.count()
    final_match_count = Match.query.count()
    
    return f"Successfully created {final_store_count} stores, {final_player_count} players, {final_tournament_count} tournaments, and {final_match_count} matches!"
