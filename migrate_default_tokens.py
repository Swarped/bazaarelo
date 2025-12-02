"""
Migration script to add default token columns to stores table.
Run this once to update the database schema.
"""

from app import app, db

def migrate():
    with app.app_context():
        try:
            # Add default_competitive_tokens and default_premium_tokens columns
            with db.engine.connect() as conn:
                # Check if columns already exist
                result = conn.execute(db.text("PRAGMA table_info(stores)"))
                columns = [row[1] for row in result]
                
                if 'default_competitive_tokens' not in columns:
                    print("Adding default_competitive_tokens column...")
                    conn.execute(db.text(
                        "ALTER TABLE stores ADD COLUMN default_competitive_tokens INTEGER DEFAULT 5"
                    ))
                    conn.commit()
                    print("✓ default_competitive_tokens column added")
                else:
                    print("✓ default_competitive_tokens column already exists")
                
                if 'default_premium_tokens' not in columns:
                    print("Adding default_premium_tokens column...")
                    conn.execute(db.text(
                        "ALTER TABLE stores ADD COLUMN default_premium_tokens INTEGER DEFAULT 1"
                    ))
                    conn.commit()
                    print("✓ default_premium_tokens column added")
                else:
                    print("✓ default_premium_tokens column already exists")
                
                # Set default values for existing stores that have NULL
                print("Setting default values for existing stores...")
                conn.execute(db.text(
                    "UPDATE stores SET default_competitive_tokens = 5 WHERE default_competitive_tokens IS NULL"
                ))
                conn.execute(db.text(
                    "UPDATE stores SET default_premium_tokens = 1 WHERE default_premium_tokens IS NULL"
                ))
                conn.commit()
                print("✓ Default values set for existing stores")
                
            print("\n✅ Migration completed successfully!")
            
        except Exception as e:
            print(f"\n❌ Migration failed: {e}")
            raise

if __name__ == '__main__':
    migrate()
