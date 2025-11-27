import sqlite3
import os

# Fix pending flag for manual tournaments
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
db_path = os.path.join(DATA_DIR, 'tournament.db')

print(f'Updating database: {db_path}')

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Set pending=False for all non-imported tournaments
cursor.execute('''
    UPDATE tournament 
    SET pending = 0 
    WHERE imported_from_text = 0 AND pending = 1
''')

affected = cursor.rowcount
conn.commit()
conn.close()

print(f'Updated {affected} manual tournament(s) to pending=False')
