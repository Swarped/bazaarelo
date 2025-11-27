import sqlite3
import os

# Add edit_token column to tournament table
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
db_path = os.path.join(DATA_DIR, 'tournament.db')

print(f'Updating database: {db_path}')

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    cursor.execute('ALTER TABLE tournament ADD COLUMN edit_token VARCHAR(64)')
    conn.commit()
    print('Successfully added edit_token column to tournament table')
except sqlite3.OperationalError as e:
    if 'duplicate column name' in str(e):
        print('edit_token column already exists')
    else:
        print(f'Error: {e}')

conn.close()

# Also update demo database if it exists
demo_db_path = os.path.join(DATA_DIR, 'tournament_demo.db')
if os.path.exists(demo_db_path):
    print(f'\nUpdating demo database: {demo_db_path}')
    conn = sqlite3.connect(demo_db_path)
    cursor = conn.cursor()
    try:
        cursor.execute('ALTER TABLE tournament ADD COLUMN edit_token VARCHAR(64)')
        conn.commit()
        print('Successfully added edit_token column to demo tournament table')
    except sqlite3.OperationalError as e:
        if 'duplicate column name' in str(e):
            print('edit_token column already exists in demo database')
        else:
            print(f'Error: {e}')
    conn.close()

