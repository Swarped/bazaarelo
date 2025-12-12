"""
Verify the achievements that were created
"""
import os
import sys

BASEDIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASEDIR)

from app import app, db, PlayerAchievement, Player, Store

def show_achievements():
    """Display all achievements"""
    with app.app_context():
        achievements = db.session.query(PlayerAchievement, Player, Store).join(
            Player, PlayerAchievement.player_id == Player.id
        ).outerjoin(
            Store, PlayerAchievement.store_id == Store.id
        ).filter(
            PlayerAchievement.achievement_type == 'store_domination'
        ).order_by(
            Store.name, PlayerAchievement.tier.desc(), Player.name
        ).all()
        
        if not achievements:
            print("No achievements found.")
            return
        
        print(f"Found {len(achievements)} achievements:\n")
        
        current_store = None
        for achievement, player, store in achievements:
            if store and store.name != current_store:
                current_store = store.name
                print(f"\n{current_store}:")
                print("-" * 50)
            
            tier_emoji = {'gold': '🥇', 'silver': '🥈', 'bronze': '🥉'}.get(achievement.tier, '')
            store_name = store.name if store else 'Unknown'
            print(f"  {tier_emoji} {achievement.tier.upper():8} - {player.name} (Player ID: {player.id})")

if __name__ == '__main__':
    show_achievements()
