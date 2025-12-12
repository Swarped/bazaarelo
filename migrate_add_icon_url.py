"""
Migration script to add icon_url column to stores table
Run this once to update the database schema
"""
import sqlite3
import os

# Database paths
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tournament.db")
DEMO_DB_PATH = os.path.join(DATA_DIR, "tournament_demo.db")

def add_icon_url_column(db_path):
    """Add icon_url column to stores table if it doesn't exist"""
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # Check if column already exists
        cursor.execute("PRAGMA table_info(stores)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'icon_url' in columns:
            print(f"Column 'icon_url' already exists in {db_path}")
        else:
            # Add the column
            cursor.execute("ALTER TABLE stores ADD COLUMN icon_url VARCHAR(255)")
            conn.commit()
            print(f"✓ Added 'icon_url' column to stores table in {db_path}")
    
    except sqlite3.Error as e:
        print(f"Error updating {db_path}: {e}")
        conn.rollback()
    
    finally:
        conn.close()

if __name__ == "__main__":
    print("Adding icon_url column to stores table...")
    print()
    
    # Update production database
    print("Updating production database:")
    add_icon_url_column(DB_PATH)
    
    print()
    
    # Update demo database if it exists
    print("Updating demo database:")
    add_icon_url_column(DEMO_DB_PATH)
    
    print()
    print("Migration complete! You can now restart the Flask server.")
