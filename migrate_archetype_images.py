"""
Combined migration: Remove deck.image_url and add ArchetypeModel.image_url

This migration:
1. Migrates existing deck images to archetype_models table
2. Adds image_url column to archetype_models
3. Removes image_url column from deck table

Usage:
    python migrate_archetype_images.py
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = 'data/tournament.db'

def backup_database():
    """Create a backup of the database before migration."""
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return False
    
    backup_path = f"{DB_PATH}.backup_archetype_images_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    import shutil
    shutil.copy2(DB_PATH, backup_path)
    print(f"✓ Database backed up to: {backup_path}")
    return True

def migrate():
    """Perform the complete migration."""
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return False
    
    print("Starting archetype image migration...")
    
    # Backup first
    if not backup_database():
        return False
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Step 1: Check if deck.image_url exists
        cursor.execute("PRAGMA table_info(deck)")
        deck_columns = [col[1] for col in cursor.fetchall()]
        deck_has_image = 'image_url' in deck_columns
        
        # Step 2: Check if archetype_models.image_url exists
        cursor.execute("PRAGMA table_info(archetype_models)")
        archetype_columns = [col[1] for col in cursor.fetchall()]
        archetype_has_image = 'image_url' in archetype_columns
        
        if not deck_has_image and archetype_has_image:
            print("✓ Migration already completed. No changes needed.")
            return True
        
        # Step 3: Add image_url to archetype_models if needed
        if not archetype_has_image:
            print("Adding image_url column to archetype_models...")
            cursor.execute("""
                ALTER TABLE archetype_models
                ADD COLUMN image_url VARCHAR(255)
            """)
            conn.commit()
            print("✓ Added image_url to archetype_models")
        
        # Step 4: Migrate images from deck to archetype_models if deck.image_url exists
        if deck_has_image:
            print("Migrating deck images to archetype_models...")
            
            # Get distinct archetype names with images from deck table
            cursor.execute("""
                SELECT name, image_url
                FROM deck
                WHERE name IS NOT NULL
                  AND image_url IS NOT NULL
                  AND image_url != ''
                GROUP BY name
                HAVING image_url = (
                    SELECT image_url
                    FROM deck d2
                    WHERE d2.name = deck.name
                      AND d2.image_url IS NOT NULL
                    ORDER BY d2.id DESC
                    LIMIT 1
                )
            """)
            
            deck_images = cursor.fetchall()
            migrated_count = 0
            
            for archetype_name, image_url in deck_images:
                # Check if archetype model exists
                cursor.execute("""
                    SELECT id FROM archetype_models
                    WHERE archetype_name = ?
                """, (archetype_name,))
                
                existing = cursor.fetchone()
                
                if existing:
                    # Update existing archetype model
                    cursor.execute("""
                        UPDATE archetype_models
                        SET image_url = ?
                        WHERE archetype_name = ?
                          AND (image_url IS NULL OR image_url = '')
                    """, (image_url, archetype_name))
                else:
                    # Create new archetype model
                    cursor.execute("""
                        INSERT INTO archetype_models (archetype_name, model_decklist, image_url, created_at, updated_at)
                        VALUES (?, '', ?, datetime('now'), datetime('now'))
                    """, (archetype_name, image_url))
                
                migrated_count += 1
            
            conn.commit()
            print(f"✓ Migrated {migrated_count} archetype images to archetype_models")
            
            # Step 5: Remove image_url from deck table
            print("Removing image_url column from deck table...")
            
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
            
            cursor.execute("""
                INSERT INTO deck_new (id, player_id, tournament_id, name, list_text, colors)
                SELECT id, player_id, tournament_id, name, list_text, colors
                FROM deck
            """)
            
            cursor.execute("DROP TABLE deck")
            cursor.execute("ALTER TABLE deck_new RENAME TO deck")
            
            conn.commit()
            print("✓ Removed image_url from deck table")
        
        print("✓ Migration completed successfully!")
        return True
        
    except Exception as e:
        conn.rollback()
        print(f"✗ Migration failed: {str(e)}")
        print("Database has been rolled back to pre-migration state.")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        conn.close()

if __name__ == '__main__':
    print("=" * 70)
    print("MIGRATION: Archetype Images")
    print("  - Migrate deck images to archetype_models")
    print("  - Remove redundant deck.image_url column")
    print("=" * 70)
    print()
    
    confirm = input("This will modify your database. Continue? (yes/no): ")
    if confirm.lower() != 'yes':
        print("Migration cancelled.")
        exit(0)
    
    success = migrate()
    
    print()
    print("=" * 70)
    if success:
        print("✓ Migration completed successfully!")
        print()
        print("Next steps:")
        print("  1. Archetype images are now in archetype_models table")
        print("  2. Individual decks no longer store redundant image copies")
        print("  3. Use admin panel to manage archetype images")
    else:
        print("✗ Migration failed. Check error messages above.")
    print("=" * 70)
