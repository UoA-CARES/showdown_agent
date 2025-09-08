import ast
import importlib.util
import os
import sys
import traceback
from pathlib import Path

from poke_env import AccountConfiguration


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


def test_modules():
    folder_path = Path(__file__).parent / "players/broken"  # same folder as this script

    for file_path in folder_path.glob("*.py"):
        module_name = file_path.stem

        err = check_syntax(file_path)
        if err:
            print(f"[SYNTAX ERROR] {module_name} {err}")
            continue
        err = check_import(file_path)
        if err:
            print(f"[IMPORT ERROR] {module_name} {err}")
            continue

        module = load_module_from_file(file_path, module_name)

        # Get the class
        if hasattr(module, "CustomAgent"):
            # Check if the class is a subclass of Player

            player_name = module_name

            agent_class = getattr(module, "CustomAgent")

            account_config = AccountConfiguration(player_name, None)
            try:
                player = agent_class(
                    account_configuration=account_config,
                    battle_format="gen9ubers",
                )
            except Exception as e:
                print(f"[RUNTIME ERROR] {module_name}: {e}")
                continue

            print(f"[OK] {module_name} syntax, import, and instantiation succeeded")
    # try:
    #     # --- Check syntax ---
    #     code = file_path.read_text(encoding="utf-8")
    #     compile(code, str(file_path), "exec")  # will raise SyntaxError if invalid

    #     # --- Try importing / running module ---
    #     spec = importlib.util.spec_from_file_location(module_name, file_path)
    #     module = importlib.util.module_from_spec(spec)
    #     sys.modules[module_name] = module
    #     spec.loader.exec_module(module)

    #     print(f"[OK] {module_name} syntax and import succeeded")
    # except SyntaxError as se:
    #     print(f"[SYNTAX ERROR] {module_name}: {se}")
    # except Exception as e:
    #     print(f"[RUNTIME ERROR] {module_name}: {e}")
    #     traceback.print_exc()
    # finally:
    #     # Clean up to avoid keeping file handles or modules in memory
    #     if module_name in sys.modules:
    #         del sys.modules[module_name]
    #     try:
    #         del module
    #     except NameError:
    #         pass


if __name__ == "__main__":
    test_modules()
