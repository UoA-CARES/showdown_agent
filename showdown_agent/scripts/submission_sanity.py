"""Shared submission sanity helpers for student validation and pull-time checks."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from file_write_detection import find_file_write_attempts

PIP_TIMEOUT_SECONDS = 240
SMOKE_TIMEOUT_SECONDS = 60


@dataclass
class ValidationResult:
    ok: bool
    stage: str
    message: str
    stdout: str = ""
    stderr: str = ""
    venv_dir: str = ""


@dataclass
class InstallResult:
    ok: bool
    stage: str
    message: str
    stdout: str = ""
    stderr: str = ""


def _venv_python_path(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def create_submission_venv() -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="showdown-submission-sanity-"))
    subprocess.run([sys.executable, "-m", "venv", str(temp_dir)], check=True)
    return temp_dir


def _run_pip(
    venv_python: Path, pip_args: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(venv_python), "-m", "pip", *pip_args],
        check=False,
        capture_output=True,
        text=True,
        timeout=PIP_TIMEOUT_SECONDS,
    )


def install_requirements_in_venv(
    venv_python: Path,
    requirements_file: Path,
) -> InstallResult:
    try:
        upgrade = _run_pip(venv_python, ["install", "--upgrade", "pip"])
    except subprocess.TimeoutExpired:
        return InstallResult(
            ok=False,
            stage="pip-upgrade-timeout",
            message=f"Timed out upgrading pip after {PIP_TIMEOUT_SECONDS}s",
        )

    if upgrade.returncode != 0:
        return InstallResult(
            ok=False,
            stage="pip-upgrade-failed",
            message="Failed to upgrade pip in temporary environment",
            stdout=upgrade.stdout,
            stderr=upgrade.stderr,
        )

    # Ignore setuptools pins from student files; they are not required for runtime.
    filtered_lines: list[str] = []
    pinned_versions: dict[str, set[str]] = defaultdict(set)
    for raw_line in requirements_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            filtered_lines.append(raw_line)
            continue

        pkg_name = line.split("==", 1)[0].split(">=", 1)[0].split("<=", 1)[0]
        pkg_name = pkg_name.strip().lower()
        if pkg_name == "setuptools":
            continue

        if "==" in line:
            pinned_versions[pkg_name].add(line.split("==", 1)[1].strip())

        filtered_lines.append(raw_line)

    conflict_messages: list[str] = []
    for pkg_name, versions in sorted(pinned_versions.items()):
        if len(versions) > 1:
            conflict_messages.append(
                f"{pkg_name} has multiple pinned versions: {', '.join(sorted(versions))}"
            )

    if conflict_messages:
        return InstallResult(
            ok=False,
            stage="requirements-conflict",
            message="Conflicting pinned requirements found",
            stderr=" | ".join(conflict_messages),
        )

    tmp_requirements = (
        Path(tempfile.mkdtemp(prefix="showdown-submission-reqs-")) / "requirements.txt"
    )
    tmp_requirements.write_text("\n".join(filtered_lines) + "\n", encoding="utf-8")

    try:
        try:
            install = _run_pip(
                venv_python,
                ["install", "-r", str(tmp_requirements)],
            )
        except subprocess.TimeoutExpired:
            return InstallResult(
                ok=False,
                stage="install-timeout",
                message=(
                    f"Timed out installing requirements after {PIP_TIMEOUT_SECONDS}s"
                ),
            )

        if install.returncode != 0:
            return InstallResult(
                ok=False,
                stage="pip-install-failed",
                message="pip could not install the requirements file",
                stdout=install.stdout,
                stderr=install.stderr,
            )
    finally:
        shutil.rmtree(tmp_requirements.parent, ignore_errors=True)

    return InstallResult(
        ok=True,
        stage="ok",
        message="Requirements installed successfully",
        stdout=install.stdout,
        stderr=install.stderr,
    )


def validate_submission(
    source_file: Path,
    requirements_file: Path,
) -> ValidationResult:
    write_findings = find_file_write_attempts(source_file)
    if write_findings:
        return ValidationResult(
            ok=False,
            stage="file-write-violation",
            message="Submission contains file output/create code patterns",
            stderr=" | ".join(write_findings),
        )

    venv_dir = create_submission_venv()
    venv_python = _venv_python_path(venv_dir)
    try:
        install_result = install_requirements_in_venv(
            venv_python,
            requirements_file,
        )
        if not install_result.ok:
            return ValidationResult(
                ok=False,
                stage=install_result.stage,
                message=install_result.message,
                stdout=install_result.stdout,
                stderr=install_result.stderr,
                venv_dir=str(venv_dir),
            )

        smoke_test = textwrap.dedent(f"""
            import importlib.util
            import sys

            module_name = {source_file.stem!r}
            source_path = {str(source_file)!r}
            spec = importlib.util.spec_from_file_location(module_name, source_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f'Could not load {{source_path}}')

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            print('OK')
            """)

        smoke = subprocess.run(
            [str(venv_python), "-c", smoke_test],
            check=False,
            capture_output=True,
            text=True,
            timeout=SMOKE_TIMEOUT_SECONDS,
        )

        if smoke.returncode != 0:
            return ValidationResult(
                ok=False,
                stage="smoke-import-failed",
                message="Submission failed import/initialization smoke test",
                stdout=smoke.stdout,
                stderr=smoke.stderr,
                venv_dir=str(venv_dir),
            )

        return ValidationResult(
            ok=True,
            stage="ok",
            message="Submission passed install and smoke test",
            stdout=smoke.stdout,
            stderr=smoke.stderr,
            venv_dir=str(venv_dir),
        )
    except subprocess.TimeoutExpired:
        return ValidationResult(
            ok=False,
            stage="smoke-timeout",
            message=(
                f"Submission import/initialization timed out after {SMOKE_TIMEOUT_SECONDS}s"
            ),
            stdout="",
            stderr="",
            venv_dir=str(venv_dir),
        )
    finally:
        shutil.rmtree(venv_dir, ignore_errors=True)


def validate_requirements_file(
    requirements_file: Path,
) -> ValidationResult:
    """Install a requirements file in a temporary venv without importing a submission."""
    venv_dir = create_submission_venv()
    venv_python = _venv_python_path(venv_dir)
    try:
        install_result = install_requirements_in_venv(
            venv_python,
            requirements_file,
        )
        if not install_result.ok:
            return ValidationResult(
                ok=False,
                stage=install_result.stage,
                message=install_result.message,
                stdout=install_result.stdout,
                stderr=install_result.stderr,
                venv_dir=str(venv_dir),
            )

        return ValidationResult(
            ok=True,
            stage="ok",
            message="Requirements file installed successfully",
            stdout=install_result.stdout,
            stderr=install_result.stderr,
            venv_dir=str(venv_dir),
        )
    finally:
        shutil.rmtree(venv_dir, ignore_errors=True)


def validate_submission_cli(agent_file: str, requirements_file: str) -> int:
    result = validate_submission(Path(agent_file), Path(requirements_file))
    print(f"[{result.stage}] {result.message}")
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return 0 if result.ok else 1


def validate_requirements_cli(requirements_file: str) -> int:
    result = validate_requirements_file(Path(requirements_file))
    print(f"[{result.stage}] {result.message}")
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    import argparse

    script_dir = Path(__file__).parent

    parser = argparse.ArgumentParser(
        description="Validate a student submission in a temporary virtual environment."
    )
    parser.add_argument("--agent-file", help="Path to the student's Python file")
    parser.add_argument(
        "--requirements-file",
        required=True,
        help="Path to the student's requirements.txt file",
    )
    args = parser.parse_args()

    agent_file_path = Path(args.agent_file) if args.agent_file else None
    if agent_file_path and not agent_file_path.is_absolute():
        agent_file_path = script_dir / agent_file_path

    requirements_file_path = Path(args.requirements_file)
    if not requirements_file_path.is_absolute():
        requirements_file_path = script_dir / requirements_file_path

    if args.agent_file:
        raise SystemExit(
            validate_submission_cli(str(agent_file_path), str(requirements_file_path))
        )
    raise SystemExit(validate_requirements_cli(str(requirements_file_path)))
