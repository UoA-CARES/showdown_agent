import os
import socket
import subprocess
import sys
import shutil
import time
from pathlib import Path

from expert_main import move_file
from submission_sanity import create_submission_venv, install_requirements_in_venv

STUDENT_EVAL_TIMEOUT_SECONDS = 180
SHOWDOWN_READY_TIMEOUT_SECONDS = 20
SHOWDOWN_HOST = "127.0.0.1"
SHOWDOWN_PORT = int(os.environ.get("SHOWDOWN_PORT", "8000"))


def _venv_python_path(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _validated_sources():
    scripts_dir = Path(__file__).parent
    players_dir = scripts_dir / "players"
    requirements_dir = players_dir / "requirements"

    sources = []
    for module_path in sorted(players_dir.glob("*.py")):
        upi = module_path.stem
        req_path = requirements_dir / f"{upi}_requirements.txt"
        sources.append((upi, req_path))
    return sources


def _run_single_player_in_venv(
    venv_python: Path, upi: str, results_file: str
) -> tuple[int, str, str, bool]:
    print(f"Running expert_main for {upi} in isolated venv")
    code = (
        "import sys; "
        "from expert_main import run_single_player; "
        f"sys.exit(run_single_player({upi!r}, {results_file!r}))"
    )
    try:
        result = subprocess.run(
            [str(venv_python), "-c", code],
            check=False,
            capture_output=True,
            text=True,
            timeout=STUDENT_EVAL_TIMEOUT_SECONDS,
        )
        return result.returncode, result.stdout, result.stderr, False
    except subprocess.TimeoutExpired as exc:
        stdout_text = _normalize_subprocess_output(exc.stdout)
        stderr_text = _normalize_subprocess_output(exc.stderr)
        return 124, stdout_text, stderr_text, True


def _normalize_subprocess_output(output) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, (bytes, bytearray, memoryview)):
        return bytes(output).decode(errors="replace")
    return str(output)


def _has_runtime_traceback(output: str) -> bool:
    if not output:
        return False
    markers = (
        "Unhandled exception raised while handling message",
        "Traceback (most recent call last):",
    )
    return any(marker in output for marker in markers)


def _resolve_showdown_dir() -> Path:
    configured = os.environ.get("SHOWDOWN_DIR") or os.environ.get(
        "POKEMON_SHOWDOWN_DIR"
    )
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "compsys726" / "pokemon-showdown").resolve()


def _start_showdown_server(showdown_dir: Path):
    process = subprocess.Popen(
        ["node", "pokemon-showdown", "start", "--no-security"],
        cwd=str(showdown_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if process.poll() is not None:
        raise RuntimeError("Showdown server exited during startup")

    deadline = time.time() + SHOWDOWN_READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Showdown server exited during startup")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex((SHOWDOWN_HOST, SHOWDOWN_PORT)) == 0:
                return process

        time.sleep(0.25)

    _stop_showdown_server(process)
    raise RuntimeError(
        f"Showdown server not ready on {SHOWDOWN_HOST}:{SHOWDOWN_PORT} after {SHOWDOWN_READY_TIMEOUT_SECONDS}s"
    )


def _stop_showdown_server(process: subprocess.Popen):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main():
    scripts_dir = Path(__file__).parent
    results_file = scripts_dir / "results" / "marking_results.txt"
    results_file.parent.mkdir(parents=True, exist_ok=True)

    showdown_dir = _resolve_showdown_dir()
    if not showdown_dir.exists():
        print(f"Pokemon Showdown directory not found: {showdown_dir}")
        print("Set SHOWDOWN_DIR or POKEMON_SHOWDOWN_DIR to your showdown server path.")
        raise SystemExit(1)

    sources = _validated_sources()
    total = len(sources)

    print(f"Running isolated grader mode for {total} validated submissions")

    for idx, (upi, req_path) in enumerate(sources, start=1):
        print(f"[{idx}/{total}] [{upi}] creating temporary venv")

        if not req_path.exists():
            print(
                f"[{idx}/{total}] [{upi}] missing requirements file {req_path.name}; moving to failed"
            )
            move_file(upi, False)
            continue

        venv_dir = create_submission_venv()
        try:
            venv_python = _venv_python_path(venv_dir)
            install_result = install_requirements_in_venv(venv_python, req_path)
            if not install_result.ok:
                print(
                    f"[{idx}/{total}] [{upi}] requirements install failed ({install_result.stage})"
                )
                if install_result.stderr:
                    print(install_result.stderr[:500])
                move_file(upi, False)
                continue

            print(f"[{idx}/{total}] [{upi}] running expert_main in isolated env")
            showdown_process = None
            try:
                print(f"[{idx}/{total}] [{upi}] starting showdown server")
                showdown_process = _start_showdown_server(showdown_dir)
                return_code, run_stdout, run_stderr, timed_out = (
                    _run_single_player_in_venv(venv_python, upi, str(results_file))
                )
            except RuntimeError as exc:
                print(f"[{idx}/{total}] [{upi}] {exc}; moving to failed")
                move_file(upi, False)
                continue
            finally:
                if showdown_process is not None:
                    _stop_showdown_server(showdown_process)
                    print(f"[{idx}/{total}] [{upi}] stopped showdown server")

            runtime_failed = _has_runtime_traceback(
                run_stdout
            ) or _has_runtime_traceback(run_stderr)

            if return_code != 0 or runtime_failed or timed_out:
                print(f"[{idx}/{total}] [{upi}] evaluation failed; moving to failed")
                if timed_out:
                    print(
                        f"[{idx}/{total}] [{upi}] timed out after {STUDENT_EVAL_TIMEOUT_SECONDS}s"
                    )
                if runtime_failed:
                    print(
                        f"[{idx}/{total}] [{upi}] detected runtime traceback in battle loop"
                    )
                if return_code != 0:
                    print(f"[{idx}/{total}] [{upi}] exit code: {return_code}")
                excerpt = (run_stderr or run_stdout)[:600]
                if excerpt:
                    print(excerpt)
                move_file(upi, False)
            else:
                print(f"[{idx}/{total}] [{upi}] evaluation passed; moving to completed")
                move_file(upi, True)
        finally:
            shutil.rmtree(venv_dir, ignore_errors=True)
            print(f"[{idx}/{total}] [{upi}] removed temporary venv")


if __name__ == "__main__":
    main()
