import ast
import importlib.util
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from file_write_detection import find_file_write_attempts
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from submission_sanity import validate_requirements_file, validate_submission


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
    upi = file_path.stem  # Extract UPI from filename

    if backup:
        backup_path = file_path.with_suffix(file_path.suffix + ".bak")
        shutil.copy2(file_path, backup_path)

    code = file_path.read_text(encoding="utf-8")

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        print(f"⚠️ [{upi}] Skipping, invalid Python: {e}")
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
        print(f"❌ [{upi}] Failed: {e}")
        # Clean up temp file if something failed
        os.remove(tmp_path)
    finally:
        # In case of crash, remove leftover tmp
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()


def remove_print_and_logging_from_dir(directory: str, backup=True):
    excluded_dirs = {"broken", "logging_violation", "__pycache__"}
    paths = [
        path
        for path in Path(directory).rglob("*.py")
        if not any(part in excluded_dirs for part in path.parts)
    ]
    paths.sort()

    total = len(paths)
    print(f"  [INFO] Stripping print/logging from {total} submissions")

    for idx, path in enumerate(paths, start=1):
        upi = path.stem
        print(f"    [{idx}/{total}] [{upi}] Processing...", end=" ")
        remove_print_and_logging_from_file(path, backup=backup)
        print("done")


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


def write_logging_violation_report(report_path: Path, upi: str, findings):
    lines = [
        f"Submission: {upi}.py",
        "Reason: detected file output/create code patterns.",
        "Findings:",
    ]
    lines.extend([f"- {finding}" for finding in findings])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def filter_broken(player_path, requirements_path):
    print(f"  [filter_broken] Starting validation of {player_path}...")
    py_files = sorted(Path(player_path).glob("*.py"))
    bad_files = []
    bad_file_paths = set()
    total = len(py_files)

    print(f"  Found {total} Python files to validate")

    print("  [CHECK] Syntax validation...")
    for idx, file in enumerate(py_files, start=1):
        upi = file.stem
        print(f"    [{idx}/{total}] [{upi}] Checking...", end=" ")
        err = check_syntax(file)
        if err:
            print(f"FAIL: {err}")
            bad_files.append((file, err))
            bad_file_paths.add(file)
            continue
        print("OK")

    print("  [CHECK] Requirements and import validation...")
    for idx, file in enumerate(py_files, start=1):
        if file in bad_file_paths:
            continue

        upi = file.stem
        print(f"    [{idx}/{total}] [{upi}] Validating...", end=" ")
        req_file = Path(requirements_path) / f"{file.stem}_requirements.txt"
        if not req_file.exists():
            err = f"Missing requirements file for {file.stem}"
            print(f"FAIL: {err}")
            bad_files.append((file, err))
            bad_file_paths.add(file)
            continue

        print(f"(install+import)", end=" ")
        validation_result = validate_submission(file, req_file)
        if not validation_result.ok:
            err = f"{validation_result.stage}: {validation_result.message}"
            print(f"FAIL")
            print(f"      Error: {err}")
            if validation_result.stdout:
                print(f"      stdout: {validation_result.stdout[:200]}")
            if validation_result.stderr:
                print(f"      stderr: {validation_result.stderr[:200]}")
            bad_files.append((file, err))
            bad_file_paths.add(file)
            continue

        print("OK")

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

    print("Fetching folder structure from Google Drive...")
    directory = read_folder(drive, "COMPSYS726 - Expert Agents", primary_folder_id)

    print_folders(directory)

    player_path = f"{Path(__file__).parent.parent}/scripts/players"

    existing_files = {f.name for f in Path(player_path).glob("*.py")}

    broken_player_path = f"{player_path}/broken"
    logging_violation_path = f"{player_path}/logging_violation"
    requirements_path = f"{player_path}/requirements"

    os.makedirs(requirements_path, exist_ok=True)
    os.makedirs(broken_player_path, exist_ok=True)
    os.makedirs(logging_violation_path, exist_ok=True)

    print(f"Player path: {player_path}")

    for folders in directory["folders"]:
        upi = folders["title"]

        if f"{upi}.py" in existing_files:
            print(f"[SKIP] {upi} already exists")
            continue

        print(f"[PROCESS] {upi}")

        files = folders["files"]

        if "requirements.txt" not in files or f"{upi}.py" not in files:
            print(f"[SKIP] {upi} missing files")
            continue

        requirements_id = files["requirements.txt"]["id"]
        pkm_expert_id = files[f"{upi}.py"]["id"]

        print(f"  [DOWNLOAD] fetching requirements and code")
        file = drive.CreateFile({"id": requirements_id})
        file.GetContentFile(f"{requirements_path}/{upi}_requirements.txt")

        file = drive.CreateFile({"id": pkm_expert_id})
        player_file = Path(player_path) / f"{upi}.py"
        file.GetContentFile(str(player_file))

        print(f"  [CHECK_WRITES] scanning for file write attempts")
        write_findings = find_file_write_attempts(player_file)
        if write_findings:
            print(
                f"  [VIOLATION] {upi} has file write code: moving to logging_violation"
            )
            for finding in write_findings:
                print(f"    - {finding}")

            violation_player_file = Path(logging_violation_path) / f"{upi}.py"
            shutil.move(player_file, str(violation_player_file))

            report_file = Path(logging_violation_path) / f"{upi}_violation.txt"
            write_logging_violation_report(report_file, upi, write_findings)

            req_file = Path(requirements_path) / f"{upi}_requirements.txt"
            if req_file.exists():
                shutil.move(req_file, Path(logging_violation_path) / req_file.name)

            continue

    print("[STAGE] Stripping print/logging statements from valid submissions...")
    remove_print_and_logging_from_dir(player_path, backup=False)

    print("[STAGE] Validating submissions (syntax, requirements, import smoke test)...")
    print("  [INFO] This stage validates all current submissions in players/")
    broken_files = filter_broken(player_path, requirements_path)
    for file, _ in broken_files:
        print(f"  [BROKEN] Moving {file.name} to broken/")
        shutil.move(file, f"{broken_player_path}/{file.name}")

    print("[STAGE] Merging requirements files...")
    merged_requirements = merge_all_requirements(requirements_path)

    print("[STAGE] Validating merged requirements...")
    merged_validation = validate_requirements_file(merged_requirements)
    if not merged_validation.ok:
        print(
            f"  [WARN] Merged requirements failed validation: {merged_validation.stage}: {merged_validation.message}"
        )
        if merged_validation.stdout:
            print(merged_validation.stdout)
        if merged_validation.stderr:
            print(merged_validation.stderr)
    else:
        print(
            "  [OK] Merged requirements validated successfully in a fresh temporary environment"
        )

    print("[COMPLETE] Pull results pipeline finished.")


if __name__ == "__main__":
    main()
