"""
Migration script to add token tracking columns to Store table
Run this once: python migrate_store_tokens.py
"""

from app import app, db, Store
from datetime import date

def migrate():
    with app.app_context():
        # Check if columns already exist
        engine = db.engine
        inspector = db.inspect(engine)
        columns = [c['name'] for c in inspector.get_columns('stores')]
        
        if 'competitive_tokens' in columns:
            print("✓ Migration already applied")
            return
        
        print("Adding token columns to Store table...")
        
        # Add columns using raw SQL
        with engine.connect() as conn:
            conn.execute(db.text("ALTER TABLE stores ADD COLUMN competitive_tokens INTEGER DEFAULT 5"))
            conn.execute(db.text("ALTER TABLE stores ADD COLUMN premium_tokens INTEGER DEFAULT 1"))
            conn.execute(db.text("ALTER TABLE stores ADD COLUMN last_token_reset DATE"))
            conn.commit()
        
        # Initialize tokens for existing stores
        stores = Store.query.all()
        for store in stores:
            if store.competitive_tokens is None:
                store.competitive_tokens = 5
            if store.premium_tokens is None:
                store.premium_tokens = 1 if store.premium else 0
            if store.last_token_reset is None:
                store.last_token_reset = date.today()
        
        db.session.commit()
        print(f"✓ Migration complete! Updated {len(stores)} stores")

if __name__ == '__main__':
    migrate()
