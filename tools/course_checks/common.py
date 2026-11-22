"""Shared isolated reference-test runner for the S&AI coursework checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
LOCAL_JDK = Path("/opt/homebrew/opt/openjdk@21")


def java_environment() -> dict[str, str]:
    environment = dict(os.environ)
    if LOCAL_JDK.is_dir() and not environment.get("JAVA_HOME"):
        environment["JAVA_HOME"] = str(LOCAL_JDK)
        environment["PATH"] = str(LOCAL_JDK / "bin") + os.pathsep + environment.get("PATH", "")
    return environment


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_config(slug: str) -> tuple[Path, dict[str, Any]]:
    exercise = ROOT / "exercises" / "practice" / slug
    config_path = exercise / ".meta" / "config.json"
    if not config_path.is_file():
        raise RuntimeError(f"Missing exercise config: {config_path.relative_to(ROOT)}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    files = config.get("files")
    if not isinstance(files, dict):
        raise RuntimeError("config.json must contain a files object")
    for role, names in files.items():
        if not isinstance(names, list):
            raise RuntimeError(f"files.{role} must be an array")
        for name in names:
            candidate = (exercise / name).resolve()
            if not candidate.is_relative_to(exercise.resolve()):
                raise RuntimeError(f"files.{role} escapes the exercise: {name}")
            if not candidate.is_file():
                raise RuntimeError(f"files.{role} is missing: {name}")
    return exercise, config


def run_reference_tests(slug: str, evidence_dir: Path) -> dict[str, Any]:
    """Copy an exercise including dotfiles, install its reference as solution, and run Gradle."""
    exercise, config = load_config(slug)
    learner_files = [exercise / name for name in config["files"].get("solution", [])]
    before = {str(path.relative_to(ROOT)): sha256(path) for path in learner_files}
    evidence_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with tempfile.TemporaryDirectory(prefix=f"sai-{slug}-") as temporary:
        scratch = Path(temporary) / slug
        shutil.copytree(exercise, scratch, ignore=shutil.ignore_patterns("build", ".gradle"))
        example_files = config["files"].get("example", [])
        solution_files = config["files"].get("solution", [])
        if len(example_files) != len(solution_files):
            raise RuntimeError("example and solution arrays must have the same length")
        for example_name, solution_name in zip(example_files, solution_files):
            source = scratch / example_name
            destination = scratch / solution_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        wrapper = scratch / "gradlew"
        wrapper.chmod(wrapper.stat().st_mode | 0o111)
        process = subprocess.run(
            [str(wrapper), "test", "--no-daemon", "--console=plain"],
            cwd=scratch,
            capture_output=True,
            text=True,
            timeout=300,
            env={**java_environment(), "GRADLE_USER_HOME": "/private/tmp/sai-gradle-cache"},
        )
        test_count = failure_count = skipped_count = 0
        for report in (scratch / "build" / "test-results" / "test").glob("*.xml"):
            suite = ET.parse(report).getroot()
            test_count += int(suite.attrib.get("tests", 0))
            failure_count += int(suite.attrib.get("failures", 0)) + int(suite.attrib.get("errors", 0))
            skipped_count += int(suite.attrib.get("skipped", 0))
        after = {str(path.relative_to(ROOT)): sha256(path) for path in learner_files}
        if before != after:
            raise RuntimeError("Learner source files changed while the isolated checker ran")
        result = {
            "exercise": slug,
            "exit_code": process.returncode,
            "tests": test_count,
            "failures": failure_count,
            "skipped": skipped_count,
            "elapsed_seconds": round(time.time() - started, 3),
            "learner_hashes_unchanged": True,
            "stdout_tail": process.stdout[-3000:],
            "stderr_tail": process.stderr[-3000:],
        }
    (evidence_dir / "reference-test-results.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    if result["exit_code"] != 0 or result["tests"] == 0 or result["failures"] != 0:
        raise RuntimeError(f"Reference test run failed; see {evidence_dir / 'reference-test-results.json'}")
    return result


def record_versions(evidence_dir: Path) -> None:
    versions: dict[str, str] = {"python": subprocess.check_output(["python3", "--version"], text=True).strip()}
    for command, args in (("java", ["-version"]), ("git", ["--version"])):
        try:
            result = subprocess.run([command, *args], capture_output=True, text=True, timeout=10, env=java_environment())
            versions[command] = (result.stdout + result.stderr).strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            versions[command] = "unavailable"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "tool-versions.json").write_text(json.dumps(versions, indent=2) + "\n", encoding="utf-8")
