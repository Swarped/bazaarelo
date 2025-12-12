"""
Migration script to add player claim functionality
- Adds claimed_by column to Player model
- Creates player_claims table in users database
"""
import sqlite3
import os
from datetime import datetime

BASEDIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASEDIR, "data")

# Database paths
TOURNAMENT_DB = os.path.join(DATA_DIR, "tournament.db")
TOURNAMENT_DEMO_DB = os.path.join(DATA_DIR, "tournament_demo.db")
USERS_DB = os.path.join(DATA_DIR, "users.db")
USERS_DEMO_DB = os.path.join(DATA_DIR, "users_demo.db")

def migrate_database(tournament_db_path, users_db_path, db_name):
    """Migrate a single pair of tournament and users databases"""
    print(f"\nMigrating {db_name}...")
    
    # === 1. Add claimed_by column to Player table (tournament.db) ===
    if os.path.exists(tournament_db_path):
        conn = sqlite3.connect(tournament_db_path)
        cursor = conn.cursor()
        
        try:
            # Check if column already exists
            cursor.execute("PRAGMA table_info(player)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if 'claimed_by' not in columns:
                print(f"  Adding claimed_by column to player table...")
                cursor.execute("ALTER TABLE player ADD COLUMN claimed_by VARCHAR(255)")
                conn.commit()
                print(f"  ✓ claimed_by column added")
            else:
                print(f"  ✓ claimed_by column already exists")
        except Exception as e:
            print(f"  ✗ Error adding claimed_by column: {e}")
        finally:
            conn.close()
    else:
        print(f"  ⚠ {tournament_db_path} not found (skipping)")
    
    # === 2. Create player_claims table (users.db) ===
    if os.path.exists(users_db_path):
        conn = sqlite3.connect(users_db_path)
        cursor = conn.cursor()
        
        try:
            # Check if table already exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='player_claims'")
            table_exists = cursor.fetchone() is not None
            
            if not table_exists:
                print(f"  Creating player_claims table...")
                cursor.execute("""
                    CREATE TABLE player_claims (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        player_id INTEGER NOT NULL,
                        player_name VARCHAR(120) NOT NULL,
                        user_id VARCHAR(255) NOT NULL,
                        status VARCHAR(20) DEFAULT 'pending',
                        submitted_at DATETIME NOT NULL,
                        reviewed_at DATETIME,
                        reviewed_by VARCHAR(255),
                        FOREIGN KEY (user_id) REFERENCES users(id)
                    )
                """)
                conn.commit()
                print(f"  ✓ player_claims table created")
            else:
                print(f"  ✓ player_claims table already exists")
        except Exception as e:
            print(f"  ✗ Error creating player_claims table: {e}")
        finally:
            conn.close()
    else:
        print(f"  ⚠ {users_db_path} not found (skipping)")

def main():
    print("=" * 60)
    print("Player Claims Migration")
    print("=" * 60)
    
    # Migrate production databases
    migrate_database(TOURNAMENT_DB, USERS_DB, "Production DBs")
    
    # Migrate demo databases
    migrate_database(TOURNAMENT_DEMO_DB, USERS_DEMO_DB, "Demo DBs")
    
    print("\n" + "=" * 60)
    print("✅ Migration complete!")
    print("=" * 60)

if __name__ == "__main__":
    main()
