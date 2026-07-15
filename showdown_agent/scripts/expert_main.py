# node pokemon-showdown start --no-security
import argparse
import asyncio
import importlib
import logging
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import List

import poke_env as pke
from poke_env import AccountConfiguration
from poke_env.player.player import Player

logging.basicConfig(level=logging.ERROR)

# Global lock for file writing
file_lock = threading.Lock()

BOT_STYLE_ORDER = ["simple", "max_damage", "random"]
BOT_TEAM_ORDER = ["uber", "ou", "uu", "ru", "nu"]
MATCH_TIMEOUT_SECONDS = 90


class RuntimeBattleErrorDetector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.triggered = False

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if (
            "Unhandled exception raised while handling message" in message
            or "Traceback (most recent call last):" in message
        ):
            self.triggered = True


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


def gather_players_filtered(player_names: set[str]) -> List[Player]:
    player_folders = Path(__file__).parent / "players"
    replay_dir = Path(__file__).parent / "replays"

    players = []

    if os.path.exists(replay_dir):
        shutil.rmtree(replay_dir)
    os.makedirs(replay_dir)

    for module_file in sorted(player_folders.glob("*.py")):
        module_name = module_file.stem
        if module_name not in player_names:
            continue

        print(f"Loading {module_file}")
        module = load_module_from_file(module_file, module_name)

        if hasattr(module, "CustomAgent"):
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


def gather_bots(bot_id_start: int = 1) -> List[Player]:
    bot_folders = Path(__file__).parent / "bots"
    bot_teams_folders = bot_folders / "teams"

    generic_bots = []
    bot_id = bot_id_start

    for style_name in BOT_STYLE_ORDER:
        module_file = bot_folders / f"{style_name}.py"
        module = load_module_from_file(module_file, style_name)

        if not hasattr(module, "CustomAgent"):
            continue

        agent_class = getattr(module, "CustomAgent")

        for team_name in BOT_TEAM_ORDER:
            team_file = bot_teams_folders / f"{team_name}.txt"
            with team_file.open("r", encoding="utf-8") as f:
                team = f.read()

            config_name = f"{style_name}-{team_name}-{bot_id}"
            account_config = AccountConfiguration(config_name, None)
            generic_bots.append(
                agent_class(
                    team=team,
                    account_configuration=account_config,
                    battle_format="gen9ubers",
                )
            )
            bot_id += 1

    return generic_bots


async def cross_evaluate(agents: List[Player]):
    return await pke.cross_evaluate(agents, n_challenges=3)


async def _cross_evaluate_with_timeout(agents: List[Player], timeout_seconds: int):
    return await asyncio.wait_for(cross_evaluate(agents), timeout=timeout_seconds)


def evaluate_player_vs_bot(player: Player, bot: Player) -> tuple[float, bool]:
    try:
        # Keep each matchup isolated and bounded in runtime.
        cross_evaluation_results = asyncio.run(
            _cross_evaluate_with_timeout([player, bot], MATCH_TIMEOUT_SECONDS)
        )
        score = cross_evaluation_results[player.username][bot.username]
        return (0.0 if score is None else score), False
    except asyncio.TimeoutError:
        print(
            f"  warning: timeout after {MATCH_TIMEOUT_SECONDS}s for {player.username} vs {bot.username}; counting as loss"
        )
        return 0.0, True
    except Exception as exc:
        print(
            f"  warning: evaluation failed for {player.username} vs {bot.username} ({type(exc).__name__}); counting as loss"
        )
        return 0.0, True


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


def run_tournament(player: Player, generic_bots: List[Player], results_file: str):
    print(f"Evaluating player: {player.username}")

    bots_beaten = 0
    runtime_error_detector = RuntimeBattleErrorDetector()
    player_logger = logging.getLogger(player.username)
    player_logger.addHandler(runtime_error_detector)
    player_logger.setLevel(logging.ERROR)

    try:
        for bot in generic_bots:
            print(f"{player.username}  vs {bot.username}: starting evaluation")
            player_winrate, hard_failure = evaluate_player_vs_bot(player, bot)

            if hard_failure or runtime_error_detector.triggered:
                print(
                    f"  error: runtime failure detected for {player.username}; aborting remaining matches"
                )
                return True

            did_beat_bot = player_winrate > 0.5
            if did_beat_bot:
                bots_beaten += 1

            print(
                f"  vs {bot.username}: winrate={player_winrate} {'(beat)' if did_beat_bot else ''}"
            )
    finally:
        player_logger.removeHandler(runtime_error_detector)

    # Rank among fixed ladder: all bots + player (e.g. 15 bots -> rank 1..16)
    player_rank = len(generic_bots) + 1 - bots_beaten
    player_mark = assign_marks(player_rank)

    print(
        f"{player.username} ranked #{player_rank} with a mark of {player_mark} ({bots_beaten}/{len(generic_bots)} bots beaten)\n"
    )

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


def run_single_player(player_name: str, results_file: str) -> int:
    players = gather_players_filtered({player_name})
    if not players:
        print(f"No valid player module found for {player_name}")
        return 1

    player = players[0]
    generic_bots = gather_bots(1)

    print(f"Starting tournament for {player.username}")
    had_runtime_error = run_tournament(player, generic_bots, results_file)
    return 1 if had_runtime_error else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one student agent against the fixed bot ladder."
    )
    parser.add_argument(
        "--upi",
        required=True,
        help="Student UPI/module name from scripts/players (without .py)",
    )
    args = parser.parse_args()

    results_file = os.path.join(
        os.path.dirname(__file__), "results", "marking_results.txt"
    )
    if not os.path.exists(os.path.dirname(results_file)):
        os.makedirs(os.path.dirname(results_file))

    upi = args.upi.removesuffix(".py")
    return run_single_player(upi, results_file)


if __name__ == "__main__":
    main()
