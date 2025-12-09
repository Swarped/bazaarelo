"""
Script to create the archetype_models table
"""
from app import app, db, ArchetypeModel

with app.app_context():
    db.create_all()
    print("✓ archetype_models table created successfully")
