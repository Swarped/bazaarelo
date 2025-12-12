"""
Migration script to remove image_url column from deck table.

This migration removes the redundant image_url field from the Deck model.
Deck images were being stored per-deck but were just copies of archetype images,
which is redundant. Archetype images should be managed separately.

Usage:
    python migrate_remove_deck_image_url.py
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = 'instance/tournament.db'

def backup_database():
    """Create a backup of the database before migration."""
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return False
    
    backup_path = f"{DB_PATH}.backup_before_deck_image_removal_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    import shutil
    shutil.copy2(DB_PATH, backup_path)
    print(f"✓ Database backed up to: {backup_path}")
    return True

def check_column_exists():
    """Check if image_url column exists in deck table."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA table_info(deck)")
    columns = cursor.fetchall()
    conn.close()
    
    column_names = [col[1] for col in columns]
    return 'image_url' in column_names

def migrate():
    """Remove image_url column from deck table."""
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return False
    
    # Check if column exists
    if not check_column_exists():
        print("✓ Column 'image_url' does not exist in deck table. Migration not needed.")
        return True
    
    print("Starting migration to remove deck.image_url column...")
    
    # Backup first
    if not backup_database():
        return False
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # SQLite doesn't support DROP COLUMN directly, so we need to:
        # 1. Create a new table without the image_url column
        # 2. Copy data from old table to new table
        # 3. Drop old table
        # 4. Rename new table to original name
        
        print("Creating new deck table without image_url column...")
        cursor.execute("""
            CREATE TABLE deck_new (
                id INTEGER PRIMARY KEY,
                player_id INTEGER NOT NULL,
                tournament_id INTEGER,
                name VARCHAR(120),
                list_text TEXT,
                colors VARCHAR(10),
                FOREIGN KEY (player_id) REFERENCES player (id),
                FOREIGN KEY (tournament_id) REFERENCES tournament (id)
            )
        """)
        
        print("Copying data from old deck table to new deck table...")
        cursor.execute("""
            INSERT INTO deck_new (id, player_id, tournament_id, name, list_text, colors)
            SELECT id, player_id, tournament_id, name, list_text, colors
            FROM deck
        """)
        
        print("Dropping old deck table...")
        cursor.execute("DROP TABLE deck")
        
        print("Renaming new deck table...")
        cursor.execute("ALTER TABLE deck_new RENAME TO deck")
        
        conn.commit()
        print("✓ Migration completed successfully!")
        print("✓ Removed image_url column from deck table")
        
        return True
        
    except Exception as e:
        conn.rollback()
        print(f"✗ Migration failed: {str(e)}")
        print("Database has been rolled back to pre-migration state.")
        return False
        
    finally:
        conn.close()

if __name__ == '__main__':
    print("=" * 60)
    print("MIGRATION: Remove deck.image_url column")
    print("=" * 60)
    print()
    
    confirm = input("This will modify your database. Continue? (yes/no): ")
    if confirm.lower() != 'yes':
        print("Migration cancelled.")
        exit(0)
    
    success = migrate()
    
    print()
    print("=" * 60)
    if success:
        print("✓ Migration completed successfully!")
    else:
        print("✗ Migration failed. Check error messages above.")
    print("=" * 60)
