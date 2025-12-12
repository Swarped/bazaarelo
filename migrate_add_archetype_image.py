"""
Migration script to add image_url column to archetype_models table.

This migration adds an image_url field to ArchetypeModel so each archetype
can have ONE image stored centrally, rather than duplicating images across
individual deck records.

Usage:
    python migrate_add_archetype_image.py
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
    
    backup_path = f"{DB_PATH}.backup_before_archetype_image_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    import shutil
    shutil.copy2(DB_PATH, backup_path)
    print(f"✓ Database backed up to: {backup_path}")
    return True

def check_column_exists():
    """Check if image_url column already exists in archetype_models table."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("PRAGMA table_info(archetype_models)")
        columns = cursor.fetchall()
        conn.close()
        
        column_names = [col[1] for col in columns]
        return 'image_url' in column_names
    except:
        conn.close()
        return False

def migrate():
    """Add image_url column to archetype_models table."""
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return False
    
    # Check if column already exists
    if check_column_exists():
        print("✓ Column 'image_url' already exists in archetype_models table. Migration not needed.")
        return True
    
    print("Starting migration to add archetype_models.image_url column...")
    
    # Backup first
    if not backup_database():
        return False
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        print("Adding image_url column to archetype_models table...")
        cursor.execute("""
            ALTER TABLE archetype_models
            ADD COLUMN image_url VARCHAR(255)
        """)
        
        conn.commit()
        print("✓ Migration completed successfully!")
        print("✓ Added image_url column to archetype_models table")
        
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
    print("MIGRATION: Add archetype_models.image_url column")
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
