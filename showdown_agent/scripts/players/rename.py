from poke_env.battle import AbstractBattle
from poke_env.player import Player

"""
Define your team here. You can use the team builder on https://play.pokemonshowdown.com/teambuilder 

Create a team and then copy the text here. 

Make sure to keep the triple quotes around the team text.

Make sure to use the Uber Format
"""

team = """
Pikachu @ Focus Sash  
Ability: Static  
Tera Type: Electric  
EVs: 8 HP / 248 SpA / 252 Spe  
Timid Nature  
IVs: 0 Atk  
- Thunder Wave  
- Thunder  
- Reflect
- Thunderbolt  
"""


class CustomAgent(Player):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, team=team, **kwargs)

    def chose_move(self, battle: AbstractBattle):
        """
        DO NOT EDIT THIS FUNCTION.
        """
        me = battle.active_pokemon
        opp = battle.opponent_active_pokemon

        if me is None or opp is None:
            return self.choose_random_move(battle)

        return self._choose_move(battle)

    def _choose_move(self, battle: AbstractBattle):
        """
        DO EDIT THIS FUNCTION
        """
        return self.choose_random_move(battle)

    def teampreview(self, battle: AbstractBattle):
        """
        SET THE TEAM ORDER HERE
        """
        return "/team 1"
