import shutil
from pathlib import Path

# Paths
base_dir = Path(__file__).parent
players_dir = base_dir / "players"
file_list_path = base_dir / "results/marking_results.txt"  # the text file
destination_dir = players_dir / "completed"
destination_dir.mkdir(exist_ok=True)

# Read file list
with file_list_path.open("r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split()[0]  # first column
        source_file = players_dir / f"{name}.py"
        if source_file.exists():
            shutil.move(source_file, destination_dir / source_file.name)
            print(f"Moved {source_file.name}")
        else:
            print(f"File not found: {source_file.name}")
