# node pokemon-showdown start --no-security


import asyncio
import csv
import importlib
import os
import random
import sys
from collections import defaultdict
from typing import Dict, List, Set, Tuple

import poke_env as pke
from poke_env import AccountConfiguration
from poke_env.player.player import Player


def convert_results_to_html(csv_file: str, html_file: str):
    with open(csv_file, newline="", encoding="utf-8") as infile:
        reader = csv.reader(infile, delimiter="\t")
        headers = next(reader)
        rows = list(reader)  # Load all rows once

        with open(html_file, "w", encoding="utf-8") as out:
            out.write("<!DOCTYPE html>\n<html>\n<head>\n")
            out.write("<meta charset='utf-8'>\n")
            out.write("<style>\n")
            out.write("table { border-collapse: collapse; width: 100%; }\n")
            out.write("th, td { border: 1px solid #ddd; padding: 8px; }\n")
            out.write("th { background-color: #f2f2f2; }\n")
            out.write(".bye { background-color: #ffe0e0; }\n")
            out.write(".green { background-color: #d4edda; }\n")  # green
            out.write(".red { background-color: #f8d7da; }\n")  # red
            out.write("</style>\n</head>\n<body>\n")
            out.write("<h2>Swiss Tournament Results</h2>\n")
            out.write("<table>\n")
            out.write(
                "<thead><tr>"
                + "".join(f"<th>{h}</th>" for h in headers)
                + "</tr></thead>\n"
            )
            out.write("<tbody>\n")

            # Determine which column is 'status'
            status_idx = headers.index("Status") if "Status" in headers else -1

            for row in rows:
                css_class = ""
                if status_idx != -1:
                    status = row[status_idx].strip().lower()
                    if "qualified" in status:
                        css_class = "green"
                    elif "eliminated" in status:
                        css_class = "red"
                elif row[-1].strip().lower() == "yes":
                    css_class = "bye"

                out.write(
                    f"<tr class='{css_class}'>"
                    + "".join(f"<td>{col}</td>" for col in row)
                    + "</tr>\n"
                )

            out.write("</tbody>\n</table>\n</body>\n</html>")


class Competitor:
    def __init__(self, id: int, username: str, agent: Player):
        self.id = id
        self.username = username
        self.agent = agent

        self.wins = 0
        self.losses = 0

        self.history: Set[int] = set()
        self.received_bye = False

    def is_active(self, win_cap: int, loss_cap: int) -> bool:
        return self.wins < win_cap and self.losses < loss_cap

    def __repr__(self):
        return f"Player({self.username}, W:{self.wins}, L:{self.losses})"

    def reset(self):
        self.wins = 0
        self.losses = 0
        self.history.clear()


def gather_players():
    player_folders = os.path.join(os.path.dirname(__file__), "players")

    players = []

    for module_name in os.listdir(player_folders):
        if module_name.endswith(".py"):
            module_path = f"{player_folders}/{module_name}"

            spec = importlib.util.spec_from_file_location(module_name, module_path)
            module = importlib.util.module_from_spec(spec)

            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # Get the class
            if hasattr(module, "CustomAgent"):
                # Check if the class is a subclass of Player
                agent_class = getattr(module, "CustomAgent")

                config_name = f"{module_name[:-3]}"
                account_config = AccountConfiguration(config_name, None)
                players.append(
                    agent_class(
                        account_configuration=account_config,
                        battle_format="gen9ubers",
                    )
                )

    return players


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


async def run_battle(p1: Competitor, p2: Competitor) -> Tuple[Competitor, Competitor]:
    players = [p1.agent, p2.agent]

    cross_evaluation_results = await pke.cross_evaluate(players, n_challenges=3)

    top_players = rank_players_by_victories(
        cross_evaluation_results, top_k=len(cross_evaluation_results)
    )

    winner = p1 if top_players[0][0] == p1.username else p2
    loser = p2 if winner == p1 else p1

    winner.wins += 1
    loser.losses += 1

    return winner, loser


def run_swiss_round(
    competitors: list[Competitor],
    results_file: str,
    summary_file: str,
    win_cap: int = 3,
    loss_cap: int = 2,
):
    round_num = 0

    print(
        f"🏆 Starting tournament with {len(competitors)} players (Win cap: {win_cap}, Loss cap: {loss_cap})"
    )

    for competitor in competitors:
        competitor.reset()

    with open(results_file, "a", encoding="utf-8") as file:
        file.write("Round\tGroup\tPlayer 1\tPlayer 2\tWinner\tBye\n")
        while True:
            # Get active players
            active_players = [
                competitor
                for competitor in competitors
                if competitor.is_active(win_cap, loss_cap)
            ]
            if len(active_players) < 2:
                break

            round_num += 1
            print(f"\n--- Round {round_num} ---")

            # Group players by (wins, losses)
            brackets: Dict[Tuple[int, int], List[Competitor]] = defaultdict(list)
            for competitor in active_players:
                brackets[(competitor.wins, competitor.losses)].append(competitor)

            for group_key in sorted(brackets.keys()):
                group = brackets[group_key]
                random.shuffle(group)
                unpaired = group[:]
                while len(unpaired) >= 2:
                    p1 = unpaired.pop(0)
                    # Find first player p2 not already played against p1
                    for i, p2 in enumerate(unpaired):
                        if p2.id not in p1.history:
                            unpaired.pop(i)
                            winner, loser = asyncio.run(run_battle(p1, p2))
                            print(
                                f"Group {group_key}: {p1.username} vs {p2.username} → Winner: {winner.username}"
                            )
                            file.write(
                                f"{round_num}\t{group_key}\t{p1.username}\t{p2.username}\t{winner.username}\tno\n"
                            )
                            break
                    else:
                        # No unique opponent available — just pair with next
                        p2 = unpaired.pop(0)
                        winner, loser = asyncio.run(run_battle(p1, p2))
                        print(
                            f"Group {group_key} (re-pair): {p1.username} vs {p2.username} → Winner: {winner.username}"
                        )
                        file.write(
                            f"{round_num}\t{group_key}\t{p1.username}\t{p2.username}\t{winner.username}\tno\n"
                        )

                # Bye if odd number
                if unpaired:
                    bye_player = unpaired.pop()
                    bye_player.wins += 1
                    print(
                        f"Group {group_key}: Player {bye_player.username} receives a BYE"
                    )
                    file.write(
                        f"{round_num}\t{group_key}\t{bye_player.username}\t' '\t {bye_player.username}\tyes\n"
                    )

    print("\n🏁 Final Results:")
    final_sorted = sorted(competitors, key=lambda p: (-p.wins, p.losses, p.id))

    with open(summary_file, "a", encoding="utf-8") as file:
        file.write("Player\tWins\tLosses 1\tStatus\n")
        for competitor in final_sorted:
            status = (
                "Qualified"
                if competitor.wins >= win_cap
                else ("Eliminated" if competitor.losses >= loss_cap else "")
            )
            print(
                f"Player {competitor.username} | W: {competitor.wins}, L: {competitor.losses} {status}"
            )
            file.write(
                f"{competitor.username}\t{competitor.wins}\t{competitor.losses}\t{status}\n"
            )

    return [p for p in final_sorted if p.wins >= win_cap]


def generate_bots(num_bots: int):
    bot_folders = os.path.join(os.path.dirname(__file__), "bots")
    bot_teams_folders = os.path.join(bot_folders, "teams")

    bots = []

    bot_to_add = "simple"
    team_file = "uber.txt"

    with open(
        os.path.join(bot_teams_folders, team_file), "r", encoding="utf-8"
    ) as file:
        bot_team = file.read()

    for i in range(num_bots):
        module_name = f"{bot_to_add}.py"
        module_path = os.path.join(bot_folders, module_name)

        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            print(f"⚠️ Could not load module {module_name}. Skipping.")
            raise ImportError(
                f"Could not load module {module_name}. Please check the file path."
            )

        module = importlib.util.module_from_spec(spec)

        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        # Get the class
        if hasattr(module, "CustomAgent"):
            # Check if the class is a subclass of Player
            agent_class = getattr(module, "CustomAgent")

            config_name = f"{module_name[:-3]}-{i+1}"
            account_config = AccountConfiguration(config_name, None)
            bots.append(
                agent_class(
                    team=bot_team,
                    account_configuration=account_config,
                    battle_format="gen9ubers",
                )
            )

    return bots


def bots_to_add_for_clean_halving(current_players: int, target_top_n: int) -> int:
    multiplier = 1
    while True:
        required_players = target_top_n * (2**multiplier)
        if required_players >= current_players:
            return required_players - current_players
        multiplier += 1


def run_swiss_phase(top_k: int, competitors: List[Competitor]):

    while len(competitors) > top_k:
        num_competitors = len(competitors)

        print(f"\n🏆 Starting a new tournament with {num_competitors} competitors")

        results_file = os.path.join(
            os.path.dirname(__file__),
            "results",
            f"swiss_results_{num_competitors}.txt",
        )
        if not os.path.exists(os.path.dirname(results_file)):
            os.makedirs(os.path.dirname(results_file))

        with open(results_file, "w", encoding="utf-8") as file:
            pass  # This opens the file in write mode, clearing it

        summary_file = os.path.join(
            os.path.dirname(__file__),
            "results",
            f"swiss_summary_{num_competitors}.txt",
        )
        if not os.path.exists(os.path.dirname(summary_file)):
            os.makedirs(os.path.dirname(summary_file))

        with open(summary_file, "w", encoding="utf-8") as file:
            pass  # This opens the file in write mode, clearing it

        cap = 3

        competitors = run_swiss_round(
            competitors, results_file, summary_file, win_cap=cap, loss_cap=cap
        )

        convert_results_to_html(
            results_file,
            os.path.join(
                os.path.dirname(__file__),
                "results",
                f"swiss_results_{num_competitors}.html",
            ),
        )

        convert_results_to_html(
            summary_file,
            os.path.join(
                os.path.dirname(__file__),
                "results",
                f"swiss_summary_{num_competitors}.html",
            ),
        )

    print(f"\nSWISS Rounds 🏆 Tournament Summary (Top {top_k}):")
    for competitor in competitors:
        print(
            f"Player {competitor.id:3d} {competitor.username} | W: {competitor.wins}, L: {competitor.losses}"
        )

    return competitors


def run_knockout_phase(players_ranked: list[Competitor]):
    """players_ranked: list of player IDs sorted from best (0) to worst (15)"""
    round_num = 1
    current_round = players_ranked

    results_file = os.path.join(
        os.path.dirname(__file__), "results", "knockout_results.txt"
    )
    if not os.path.exists(os.path.dirname(results_file)):
        os.makedirs(os.path.dirname(results_file))

    with open(results_file, "w", encoding="utf-8") as file:
        pass  # This opens the file in write mode, clearing it

    replay_dir = os.path.join(os.path.dirname(__file__), "replays")
    if not os.path.exists(replay_dir):
        os.makedirs(replay_dir)

    with open(results_file, "a", encoding="utf-8") as file:
        file.write("Top\tPlayer 1\tPlayer 2\tWinner\n")

        while len(current_round) > 1:
            print(f"\n=== Round {round_num}: Top {len(current_round)} players ===")

            for player in current_round:
                print(f"Player {player.id:3d} {player.username}")

            next_round = []
            num_matches = len(current_round) // 2

            current_dir = os.path.join(replay_dir, f"round_{round_num}")
            if not os.path.exists(current_dir):
                os.makedirs(current_dir)

            for i in range(num_matches):
                p1 = current_round[i]
                p2 = current_round[-(i + 1)]

                p1.agent._save_replays = (
                    current_dir + "/" + p1.username + "--vs--" + p2.username
                )

                winner, loser = asyncio.run(run_battle(p1, p2))
                print(
                    f"Match: {p1.username} vs {p2.username} → Winner: {winner.username}"
                )

                file.write(
                    f"{len(current_round)}\t{p1.username}\t{p2.username}\t{winner.username}\n"
                )

                next_round.append(winner)
            current_round = next_round
            round_num += 1

    convert_results_to_html(
        results_file,
        os.path.join(
            os.path.dirname(__file__),
            "results",
            "knockout_results.html",
        ),
    )

    return current_round[0]


def run_competition(
    players: List[Player],
    top_k: int = 16,
):
    competitors = [Competitor(i + 1, p.username, p) for i, p in enumerate(players)]

    if len(competitors) < top_k:
        print(f"⚠️ Not enough players found ({len(players)}) to start a tournament.")
        return

    bots_to_add = bots_to_add_for_clean_halving(len(competitors), top_k)

    print(f"🤖 Adding {bots_to_add} bots to make a clean halving for {top_k} players")

    bots = generate_bots(bots_to_add)

    bot_competitors = [
        Competitor(i + len(players) + 1, p.username, p) for i, p in enumerate(bots)
    ]

    competitors += bot_competitors

    top_k_competitors = run_swiss_phase(top_k, competitors)

    print("\n🏁 Knockout Rounds:")
    winner = run_knockout_phase(top_k_competitors)
    print(f"\n🏆 Final Winner: {winner.username} (ID: {winner.id})")


def main():

    players = gather_players()

    run_competition(players, top_k=16)


if __name__ == "__main__":
    main()
