#!/usr/bin/env python3
"""
Migration script to add share_token column to deck table
Run this before starting the app if you get: "no such column: deck.share_token"
"""

import sqlite3
import sys
import os

def migrate_database():
    """Add share_token column to deck table if it doesn't exist"""
    
    db_path = os.path.join(os.path.dirname(__file__), 'data', 'tournament.db')
    
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        print("It will be created automatically when you run the Flask app.")
        return
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if share_token column already exists
        cursor.execute("PRAGMA table_info(deck)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'share_token' in columns:
            print("✓ share_token column already exists in deck table")
            conn.close()
            return
        
        # Add the share_token column without UNIQUE constraint first
        print("Adding share_token column to deck table...")
        cursor.execute("""
            ALTER TABLE deck ADD COLUMN share_token VARCHAR(64)
        """)
        
        conn.commit()
        print("✓ Successfully added share_token column to deck table")
        conn.close()
        
    except Exception as e:
        print(f"✗ Migration failed: {e}")
        sys.exit(1)

if __name__ == '__main__':
    migrate_database()
