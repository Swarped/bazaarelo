"""
Migration script to create player_achievements table
"""
import sqlite3
import os

def migrate():
    db_paths = [
        'data/tournament.db',
        'data/tournament_demo.db'
    ]
    
    for db_path in db_paths:
        if not os.path.exists(db_path):
            print(f"Skipping {db_path} (not found)")
            continue
            
        print(f"\nMigrating {db_path}...")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if table already exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='player_achievements'
        """)
        
        if cursor.fetchone():
            print(f"  ✓ Table 'player_achievements' already exists")
        else:
            # Create player_achievements table
            cursor.execute("""
                CREATE TABLE player_achievements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_id INTEGER NOT NULL,
                    store_id INTEGER NOT NULL,
                    achievement_type VARCHAR(50) NOT NULL,
                    tier VARCHAR(20) NOT NULL,
                    earned_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (player_id) REFERENCES player(id),
                    FOREIGN KEY (store_id) REFERENCES stores(id),
                    UNIQUE (player_id, store_id, achievement_type)
                )
            """)
            conn.commit()
            print(f"  ✓ Created 'player_achievements' table")
        
        # Check and add store_id column to casual_ranking_snapshot if table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='casual_ranking_snapshot'
        """)
        
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(casual_ranking_snapshot)")
            columns = [col[1] for col in cursor.fetchall()]
            
            if 'store_id' not in columns:
                cursor.execute("""
                    ALTER TABLE casual_ranking_snapshot 
                    ADD COLUMN store_id INTEGER REFERENCES stores(id)
                """)
                conn.commit()
                print(f"  ✓ Added 'store_id' column to casual_ranking_snapshot")
            else:
                print(f"  ✓ Column 'store_id' already exists in casual_ranking_snapshot")
        else:
            print(f"  ⚠ Table 'casual_ranking_snapshot' does not exist (skipping)")
        
        conn.close()
    
    print("\n✅ Migration complete!")

if __name__ == '__main__':
    migrate()
