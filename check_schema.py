import sqlite3

conn = sqlite3.connect('instance/tournament.db')
cursor = conn.cursor()
cursor.execute('PRAGMA table_info(tournament)')
columns = cursor.fetchall()

print('Tournament table columns:')
for col in columns:
    print(f'{col[1]:20} {col[2]:15} nullable={col[3]==0}')

# Check if edit_token exists
has_edit_token = any(col[1] == 'edit_token' for col in columns)
print(f'\nedit_token column exists: {has_edit_token}')

conn.close()
