"""
Recalculate all store achievements retroactively based on existing casual tournament data.
"""
import os
import sys

# Ensure we can import from the app
BASEDIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASEDIR)

from app import app, db, Store, Tournament, calculate_store_achievements

def recalculate_all_achievements():
    """Recalculate achievements for all stores that have casual tournaments"""
    with app.app_context():
        # Get all stores that have at least one casual tournament
        stores_with_casual = db.session.query(Store).join(
            Tournament, Store.id == Tournament.store_id
        ).filter(
            Tournament.casual == True,
            Tournament.pending == False
        ).distinct().all()
        
        if not stores_with_casual:
            print("No stores with casual tournaments found.")
            return
        
        print(f"Found {len(stores_with_casual)} stores with casual tournaments:")
        for store in stores_with_casual:
            print(f"  - {store.name} (ID: {store.id})")
        
        print("\nRecalculating achievements...")
        for store in stores_with_casual:
            print(f"Processing store: {store.name} (ID: {store.id})")
            calculate_store_achievements(store.id)
            print(f"  ✓ Achievements calculated for {store.name}")
        
        print("\n✅ All achievements recalculated successfully!")

if __name__ == '__main__':
    recalculate_all_achievements()
