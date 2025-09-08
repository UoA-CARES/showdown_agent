# node pokemon-showdown start --no-security
import asyncio
import builtins
import contextlib
import gc
import importlib
import io
import logging
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List

import poke_env as pke
from poke_env import AccountConfiguration
from poke_env.player.player import Player
from tabulate import tabulate

logging.basicConfig(level=logging.ERROR)

# Global lock for file writing
file_lock = threading.Lock()


def rank_players_by_victories(results_dict, top_k=10):
    victory_scores = {}

    for player, opponents in results_dict.items():
        victories = [
            1 if (score is not None and score > 0.5) else 0
            for opp, score in opponents.items()
            if opp != player
        ]
        if victories:
            victory_scores[player] = sum(victories) / len(victories)
        else:
            victory_scores[player] = 0.0

    # Sort by descending victory rate
    sorted_players = sorted(victory_scores.items(), key=lambda x: x[1], reverse=True)

    return sorted_players[:top_k]


@contextlib.contextmanager
def silence_all_output():
    # Backup
    old_print = builtins.print
    old_stdout, old_stderr = sys.stdout, sys.stderr
    old_fd_out, old_fd_err = os.dup(1), os.dup(2)

    try:
        # Kill Python-level print
        builtins.print = lambda *a, **k: None
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")

        # Kill C-level / fd-level writes
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        os.close(devnull_fd)

        yield

    finally:
        # Restore everything
        builtins.print = old_print
        sys.stdout.close()
        sys.stderr.close()
        sys.stdout, sys.stderr = old_stdout, old_stderr
        os.dup2(old_fd_out, 1)
        os.dup2(old_fd_err, 2)
        os.close(old_fd_out)
        os.close(old_fd_err)


def load_module_from_file(path, name: str):
    """Load a Python module safely, closing the file immediately."""
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_file_location(name, path)
    )
    with open(path, "r", encoding="utf-8") as f:
        code = f.read()
    exec(code, module.__dict__)
    sys.modules[name] = module
    return module


def gather_players():
    player_folders = Path(__file__).parent / "players"
    replay_dir = Path(__file__).parent / "replays"

    players = []

    if os.path.exists(replay_dir):
        shutil.rmtree(replay_dir)
    os.makedirs(replay_dir)

    for module_file in player_folders.glob("*.py"):
        print(f"Loading {module_file}")
        with silence_all_output():
            module_name = module_file.stem
            module = load_module_from_file(module_file, module_name)

        # Get the class
        if hasattr(module, "CustomAgent"):
            # Check if the class is a subclass of Player

            player_name = module_name

            agent_class = getattr(module, "CustomAgent")

            agent_replay_dir = os.path.join(replay_dir, f"{player_name}")
            if not os.path.exists(agent_replay_dir):
                os.makedirs(agent_replay_dir)

            account_config = AccountConfiguration(player_name, None)
            player = agent_class(
                account_configuration=account_config,
                battle_format="gen9ubers",
            )

            player._save_replays = agent_replay_dir

            players.append(player)

    return players


def gather_bots(bot_id: int) -> List[Player]:
    bot_folders = Path(__file__).parent / "bots"
    bot_teams_folders = bot_folders / "teams"

    generic_bots = []

    # Load all teams first
    bot_teams = {}
    for team_file in bot_teams_folders.glob("*.txt"):
        with team_file.open("r", encoding="utf-8") as f:
            bot_teams[team_file.stem] = f.read()

    # Load all bot modules safely
    for module_file in bot_folders.glob("*.py"):
        module_name = module_file.stem
        module = load_module_from_file(module_file, module_name)

        if hasattr(module, "CustomAgent"):
            agent_class = getattr(module, "CustomAgent")

            for team_name, team in bot_teams.items():
                config_name = f"{module_name}-{team_name}-{bot_id}"
                account_config = AccountConfiguration(config_name, None)
                generic_bots.append(
                    agent_class(
                        team=team,
                        account_configuration=account_config,
                        battle_format="gen9ubers",
                    )
                )

    return generic_bots


async def cross_evaluate(agents: List[Player]):
    return await pke.cross_evaluate(agents, n_challenges=3)


def evalute_against_bots(players: List[Player]):
    print(f"{len(players)} are competing in this challenge")

    print("Running Cross Evaluations...")
    cross_evaluation_results = asyncio.run(cross_evaluate(players))

    print("Evaluations Complete")

    table = [["-"] + [p.username for p in players]]
    for p_1, results in cross_evaluation_results.items():
        table.append([p_1] + [cross_evaluation_results[p_1][p_2] for p_2 in results])

    # first row is headers
    headers = table[0]
    data = table[1:]

    # print(tabulate(data, headers=headers, floatfmt=".2f"))

    # print("Rankings")
    top_players = rank_players_by_victories(
        cross_evaluation_results, top_k=len(cross_evaluation_results)
    )

    return top_players, False


def assign_marks(rank: int) -> float:
    modifier = 1.0 if rank > 10 else 0.5

    top_marks = 10.0 if rank < 10 else 5.0

    mod_rank = rank % 10

    marks = top_marks - (mod_rank - 1) * modifier

    return 0.0 if marks < 0 else marks


def safe_write(results_file, text):
    """Write to the file safely from multiple threads."""
    with file_lock:
        with open(results_file, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def run_tournament(players, generic_bots, results_file):

    for player in players:
        agents = []
        print(f"Evaluating player: {player.username}")
        agents.append(player)
        agents.extend(generic_bots)

        agent_rankings, error = evalute_against_bots(agents)

        if error:
            print(f"Error evaluating {player.username}, skipping...")
            safe_write(results_file, f"{player.username} #Error 0")
            return True

        player_rank = len(agents) + 1
        player_mark = 0.0
        # print("Rank. Player - Win Rate - Mark")
        for rank, (agent, winrate) in enumerate(agent_rankings, 1):
            mark = assign_marks(rank)

            # print(f"{rank}. {agent} - {winrate:.2f} - {mark}")
            if agent == player.username:
                player_rank = rank
                player_mark = mark

        print(f"{player.username} ranked #{player_rank} with a mark of {player_mark}\n")

        safe_write(results_file, f"{player.username} #{player_rank} {player_mark}")

        return False


def move_file(player_name: str, success: bool):
    player_folders = os.path.join(os.path.dirname(__file__), "players")

    if success:
        new_location = os.path.join(player_folders, "completed")
    else:
        new_location = os.path.join(player_folders, "failed")

    os.makedirs(new_location, exist_ok=True)

    src_file = os.path.join(player_folders, f"{player_name}.py")
    dst_file = os.path.join(new_location, f"{player_name}.py")

    if os.path.exists(src_file):
        shutil.move(src_file, dst_file)
        print(f"Moved {player_name}.py -> {new_location}")
    else:
        print(f"⚠️ Source file not found: {src_file}")


def main():
    players = gather_players()
    generic_bots = gather_bots(1)

    results_file = os.path.join(
        os.path.dirname(__file__), "results", "marking_results.txt"
    )
    if not os.path.exists(os.path.dirname(results_file)):
        os.makedirs(os.path.dirname(results_file))

    for player in players:
        print(f"Starting tournament for {player.username}")
        outcome = run_tournament([player], generic_bots, results_file)
        move_file(player.username, not outcome)


if __name__ == "__main__":
    main()
