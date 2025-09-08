import ast
import importlib.util
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import astor
from poke_env import AccountConfiguration
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
import tempfile


class PrintAndLoggingRemover(ast.NodeTransformer):
    def visit_Expr(self, node):
        """
        Replace expressions that are print() or logging.*() calls with 'pass'
        """
        if isinstance(node.value, ast.Call) and (
            (isinstance(node.value.func, ast.Name) and node.value.func.id == "print")
            or (
                isinstance(node.value.func, ast.Attribute)
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == "logging"
            )
        ):
            return ast.Pass()  # <- insert a pass statement
        return self.generic_visit(node)


def remove_print_and_logging_from_file(file_path: Path, backup=True):
    if backup:
        backup_path = file_path.with_suffix(file_path.suffix + ".bak")
        shutil.copy2(file_path, backup_path)

    code = file_path.read_text(encoding="utf-8")

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        print(f"⚠️ Skipping {file_path}, invalid Python: {e}")
        return

    tree = PrintAndLoggingRemover().visit(tree)
    ast.fix_missing_locations(tree)
    new_code = ast.unparse(tree)  # Python 3.9+

    # Write to a temporary file first
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(new_code)

        # Replace original only after success
        os.replace(tmp_path, file_path)
    except Exception as e:
        print(f"❌ Failed on {file_path}: {e}")
        # Clean up temp file if something failed
        os.remove(tmp_path)
    finally:
        # In case of crash, remove leftover tmp
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()


def remove_print_and_logging_from_dir(directory: str, backup=True):
    for path in Path(directory).rglob("*.py"):
        print(f"Processing {path}")
        remove_print_and_logging_from_file(path, backup=backup)


def gather_players():
    player_folders = os.path.join(os.path.dirname(__file__), "players")

    broken_player_path = f"{player_folders}/broken"

    for module_name in os.listdir(player_folders):
        if module_name.endswith(".py"):
            module_path = f"{player_folders}/{module_name}"

            spec = importlib.util.spec_from_file_location(module_name, module_path)
            module = importlib.util.module_from_spec(spec)

            # f = io.StringIO()
            # with contextlib.redirect_stdout(f):
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # Get the class
            if hasattr(module, "CustomAgent"):
                # Check if the class is a subclass of Player

                player_name = f"{module_name[:-3]}"

                agent_class = getattr(module, "CustomAgent")

                account_config = AccountConfiguration(player_name, None)
                try:
                    player = agent_class(
                        account_configuration=account_config,
                        battle_format="gen9ubers",
                    )
                except Exception as e:
                    print(f"Error creating player instance for {player_name}: {e}")
                    print(f"Removing broken file: {module_name}")
                    shutil.move(module_path, f"{broken_player_path}/{module_name}")


def parse_line(line):
    """Parse a requirements line into (package, version)"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None, None
    if "==" in line:
        pkg, ver = line.split("==", 1)
        return pkg.strip().lower(), ver.strip()
    return line.lower(), None


def merge_requirements(files):
    packages = defaultdict(set)

    for f in files:
        for line in Path(f).read_text().splitlines():
            pkg, ver = parse_line(line)
            if pkg:
                packages[pkg].add(ver)

    merged = []
    conflicts = []

    for pkg, versions in sorted(packages.items()):
        versions = {v for v in versions if v is not None}
        if len(versions) > 1:
            conflicts.append((pkg, versions))
            merged.append(
                f"{pkg}=={sorted(versions)[-1]}  # CONFLICT: {', '.join(versions)}"
            )
        elif len(versions) == 1:
            merged.append(f"{pkg}=={versions.pop()}")
        else:
            merged.append(pkg)  # no version specified

    return merged, conflicts


def read_folder(drive, title, file_id):
    folder = {}

    folder["title"] = title
    folder["files"] = {}
    folder["folders"] = []

    drive_list = drive.ListFile(
        {"q": f"'{file_id}' in parents and trashed=false"}
    ).GetList()

    for f in drive_list:
        if f["mimeType"] == "application/vnd.google-apps.folder":
            folder["folders"].append(read_folder(drive, f["title"], f["id"]))
        else:
            folder["files"][f["title"]] = {
                "id": f["id"],
                "title": f["title"],
                "title1": f["alternateLink"],
            }

    return folder


def print_folders(directory, tab=0):
    tabs = " " * tab

    for _, file in directory["files"].items():
        message = f"{tabs}File: {file['title']}, id: {file['id']}"
        print(f"{message}")

    for folder in directory["folders"]:
        message = f"{tabs}Folder: {folder['title']}"
        print(f"{message}")
        print_folders(folder, tab=tab + 5)


def check_syntax(file_path: Path):
    """Check for syntax errors by parsing with ast."""
    try:
        source = file_path.read_text()
        ast.parse(source, filename=str(file_path))
        return None  # no syntax error
    except SyntaxError as e:
        return f"SyntaxError: {e}"


def check_import(file_path: Path):
    """Try importing the file as a module (without polluting sys.modules)."""
    try:
        spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # may raise ImportError, etc.
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def filter_broken(player_path):
    py_files = Path(player_path).glob("*.py")
    bad_files = []

    for file in py_files:
        err = check_syntax(file)
        if err:
            print(f"Syntax error in {file}: {err}")
            bad_files.append((file, err))
            continue

        err = check_import(file)
        if err:
            print(f"Import error in {file}: {err}")
            bad_files.append((file, err))

    return bad_files


def merge_all_requirements(player_path):
    req_files = list(Path(player_path).glob("*_requirements.txt"))
    merged, conflicts = merge_requirements(req_files)

    if conflicts:
        print("Conflicts detected in requirements:")
        for pkg, versions in conflicts:
            print(f"  {pkg}: {', '.join(versions)}")

    merged_path = Path(player_path) / "merged_requirements.txt"
    merged_path.write_text("\n".join(merged) + "\n")
    print(f"Merged requirements written to {merged_path}")
    return merged_path


def main():
    gauth = GoogleAuth()
    gauth.LocalWebserverAuth()

    drive = GoogleDrive(gauth)

    # COMPSYS726 - Assignment 1 Folder
    primary_folder_id = "1CLYDBXYuLHfna8Uj4lCH4H4y6uBmJekD"

    directory = read_folder(drive, "COMPSYS726 - Expert Agents", primary_folder_id)

    print_folders(directory)

    player_path = f"{Path(__file__).parent.parent}/scripts/players"

    existing_files = {f.name for f in Path(player_path).glob("*.py")}

    broken_player_path = f"{player_path}/broken"
    requirements_path = f"{player_path}/requirements"

    os.makedirs(requirements_path, exist_ok=True)
    os.makedirs(broken_player_path, exist_ok=True)

    print(f"Player path: {player_path}")

    for folders in directory["folders"]:
        upi = folders["title"]

        if f"{upi}.py" in existing_files:
            print(f"Skipping {upi}, already exists")
            continue

        print(f"Title: {upi}")

        files = folders["files"]

        if "requirements.txt" not in files or f"{upi}.py" not in files:
            print(f"Skipping {upi}, missing files")
            continue

        requirements_id = files["requirements.txt"]["id"]
        pkm_expert_id = files[f"{upi}.py"]["id"]

        file = drive.CreateFile({"id": requirements_id})
        file.GetContentFile(f"{requirements_path}/{upi}_requirements.txt")

        file = drive.CreateFile({"id": pkm_expert_id})
        file.GetContentFile(f"{player_path}/{upi}.py")

    remove_print_and_logging_from_dir(player_path, backup=False)

    broken_files = filter_broken(player_path)
    for file, _ in broken_files:
        print(f"Removing broken file: {file}")
        shutil.move(file, f"{broken_player_path}/{file.name}")

    gather_players()

    merge_all_requirements(requirements_path)


if __name__ == "__main__":
    # main()
    player_folders = os.path.join(os.path.dirname(__file__), "players")

    broken = filter_broken(f"{player_folders}/broken")

    for file, err in broken:
        print(f"{file.name}")
