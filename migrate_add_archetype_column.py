import sqlite3
import shutil
from datetime import datetime

def migrate():
    db_path = 'data/tournament.db'
    backup_path = f'data/tournament_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
    
    # Create backup
    print(f"Creating backup: {backup_path}")
    shutil.copy2(db_path, backup_path)
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # Add archetype column to deck table
        print("Adding archetype column to deck table...")
        cursor.execute("ALTER TABLE deck ADD COLUMN archetype VARCHAR(120)")
        
        # Migrate existing data: set archetype = name for all existing decks
        print("Migrating existing data: setting archetype = name...")
        cursor.execute("UPDATE deck SET archetype = name WHERE archetype IS NULL")
        
        conn.commit()
        print("Migration completed successfully!")
        
        # Show some sample data
        cursor.execute("SELECT id, name, archetype FROM deck LIMIT 10")
        print("\nSample migrated data:")
        for row in cursor.fetchall():
            print(f"  Deck {row[0]}: name='{row[1]}', archetype='{row[2]}'")
        
    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
        print(f"Database restored from backup: {backup_path}")
        raise
    finally:
        conn.close()

if __name__ == '__main__':
    migrate()
