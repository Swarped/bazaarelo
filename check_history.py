from app import app, db, CasualPointsHistory, Tournament

with app.app_context():
    count = CasualPointsHistory.query.count()
    print(f"Total CasualPointsHistory records: {count}")
    
    if count > 0:
        records = CasualPointsHistory.query.order_by(CasualPointsHistory.awarded_at.desc()).limit(20).all()
        print("\nMost recent 20 records:")
        for r in records:
            tournament = Tournament.query.get(r.tournament_id)
            print(f"  Player {r.player_id}, Store {r.store_id}, Points {r.points}, Rank {r.rank}, Tournament {r.tournament_id} ({tournament.name if tournament else 'N/A'})")
        
        # Show unique store IDs
        store_ids = db.session.query(CasualPointsHistory.store_id).distinct().all()
        print(f"\nUnique store IDs in history: {[s[0] for s in store_ids]}")
    else:
        print("No records found - history table might not be populated yet")

