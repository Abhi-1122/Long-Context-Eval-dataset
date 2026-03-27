#!/usr/bin/env python3
"""
Gemini CLI Benchmark Harness
Usage: python harness/run_eval.py [--tasks all|t001,t002] [--skip-install]
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

from score import score_task

BASE_DIR = Path(__file__).parent.parent
DATASET_FILE = BASE_DIR / "dataset" / "tasks.jsonl"
TASKS_DIR = BASE_DIR / "dataset"
RESULTS_DIR = BASE_DIR / "results"
REPOS_DIR = BASE_DIR / "repos"


def _task_folder(task: dict) -> str:
    return Path(task["problem_statement_file"]).parts[1]


def _task_short_id(task: dict) -> str:
    return _task_folder(task).split("_")[0]


def load_tasks(filter_ids=None) -> list[dict]:
    tasks = [json.loads(line) for line in open(DATASET_FILE) if line.strip()]
    if not filter_ids:
        return tasks

    filter_ids = [f.strip() for f in filter_ids if f.strip()]

    def match(task: dict) -> bool:
        instance_id = task["instance_id"]
        folder = _task_folder(task)
        short_id = _task_short_id(task)
        return any(
            token in instance_id
            or token == short_id
            or token == folder
            for token in filter_ids
        )

    return [task for task in tasks if match(task)]


def shallow_clone(repo: str, dest: Path):
    """Clone repo at current default branch HEAD."""
    if dest.exists():
        print(f"  [skip] {dest} already exists")
        return
    print(f"  [clone] {repo} → {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--filter=blob:none", f"https://github.com/{repo}", str(dest)],
        check=True,
    )


def install_deps(repo_path: Path, install_cmd: str):
    print(f"  [install] {install_cmd}")
    subprocess.run(install_cmd, shell=True, cwd=repo_path, check=True)


def run_agent(
    repo_path: Path,
    problem_statement: str,
    output_dir: Path,
    max_turns: int = 30,
    interactive: bool = False,
    approval_mode: str = "yolo",
) -> dict:
    """Run Gemini CLI inside the repo directory."""
    output_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"  [agent] running gemini CLI "
        f"({'interactive' if interactive else 'headless'}, max_turns hint={max_turns}) ..."
    )
    agent_log = output_dir / "agent_log.json"

    if interactive:
        proc = subprocess.run(
            [
                "gemini",
                "-i",
                problem_statement,
                "--approval-mode",
                approval_mode,
            ],
            cwd=repo_path,
            text=True,
            env={**os.environ, "HOME": str(Path.home())},
        )
        agent_log.write_text("{}")
        (output_dir / "agent_stderr.txt").write_text("")
    else:
        proc = subprocess.run(
            [
                "gemini",
                "-p",
                problem_statement,
                "--approval-mode",
                approval_mode,
                "--output-format",
                "json",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(Path.home())},
        )

        agent_log.write_text(proc.stdout or "{}")
        (output_dir / "agent_stderr.txt").write_text(proc.stderr or "")

    patch_proc = subprocess.run(["git", "diff", "HEAD"], cwd=repo_path, capture_output=True, text=True)
    patch_path = output_dir / "agent.patch"
    patch_path.write_text(patch_proc.stdout)

    touched_proc = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    (output_dir / "files_touched.txt").write_text(touched_proc.stdout)

    if interactive:
        stats = {"turns": "interactive", "tokens_used": "unknown"}
    else:
        try:
            log = json.loads(proc.stdout)
            stats = {
                "turns": len(log.get("turns", [])),
                "tokens_used": log.get("usage", {}).get("total_tokens", "unknown"),
            }
        except Exception:
            stats = {"turns": "unknown", "tokens_used": "unknown"}

    json.dump(stats, open(output_dir / "agent_stats.json", "w"), indent=2)
    return stats


def apply_and_test(repo_path: Path, task: dict, output_dir: Path) -> dict:
    """Apply agent patch + test patch on a CLEAN copy of the repo and run tests."""
    from score import run_tests

    patch_path = output_dir / "agent.patch"
    test_patch_path = TASKS_DIR / task["test_patch_file"]

    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path, check=True)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_path, check=True)

    return run_tests(
        repo_path=str(repo_path),
        test_cmd=task["environment_setup"]["test_cmd"],
        patch_path=str(patch_path),
        test_patch_path=str(test_patch_path),
    )


def evaluate_task(task: dict, args) -> dict:
    instance_id = task["instance_id"]
    repo_path = REPOS_DIR / instance_id
    output_dir = RESULTS_DIR / "runs" / instance_id

    print(f"\n{'=' * 60}")
    print(f"Task: {instance_id}  [{task['difficulty'].upper()}]")
    print(f"{'=' * 60}")

    shallow_clone(task["repo"], repo_path)

    if not args.skip_install:
        install_deps(repo_path, task["environment_setup"]["install"])

    problem_path = TASKS_DIR / task["problem_statement_file"]
    problem = problem_path.read_text()

    agent_stats = run_agent(
        repo_path,
        problem,
        output_dir,
        args.max_turns,
        args.interactive_agent,
        args.approval_mode,
    )

    test_output = apply_and_test(repo_path, task, output_dir)

    files_touched = (output_dir / "files_touched.txt").read_text().strip().splitlines()

    score = score_task(task, test_output["test_results"], agent_stats, test_output["patch_valid"])
    score["files_touched"] = files_touched
    score["files_expected"] = task["files_involved"]
    score["coverage_overlap"] = len(set(files_touched) & set(task["files_involved"])) / max(
        len(task["files_involved"]), 1
    )

    result_path = output_dir / "result.json"
    json.dump(score, open(result_path, "w"), indent=2)
    print(
        f"  [result] resolved={score['resolved']}  "
        f"resolve_rate={score['resolve_rate']}  "
        f"regressions={score['regression_rate']}"
    )
    return score


def main():
    parser = argparse.ArgumentParser(description="Gemini CLI Benchmark Harness")
    parser.add_argument("--tasks", default="all", help="all | comma-separated instance ids or task ids")
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument(
        "--interactive-agent",
        action="store_true",
        help="Run Gemini in interactive mode with live terminal output.",
    )
    parser.add_argument(
        "--approval-mode",
        default="yolo",
        choices=["default", "auto_edit", "yolo", "plan"],
        help="Gemini tool approval mode.",
    )
    args = parser.parse_args()

    filter_ids = None if args.tasks == "all" else args.tasks.split(",")
    tasks = load_tasks(filter_ids)
    print(f"Loaded {len(tasks)} task(s)")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    REPOS_DIR.mkdir(parents=True, exist_ok=True)

    all_scores = []
    for task in tasks:
        try:
            score = evaluate_task(task, args)
        except Exception as error:
            print(f"  [ERROR] {task['instance_id']}: {error}")
            score = {"instance_id": task["instance_id"], "error": str(error), "resolved": False}
        all_scores.append(score)

    summary = {
        "total_tasks": len(all_scores),
        "resolved": sum(1 for s in all_scores if s.get("resolved")),
        "resolve_rate": round(
            sum(s.get("resolve_rate", 0) for s in all_scores) / max(len(all_scores), 1), 3
        ),
        "avg_regression": round(
            sum(s.get("regression_rate", 0) for s in all_scores) / max(len(all_scores), 1), 3
        ),
        "by_difficulty": {
            difficulty: {
                "resolved": sum(
                    1
                    for s in all_scores
                    if s.get("difficulty") == difficulty and s.get("resolved")
                ),
                "total": sum(1 for s in all_scores if s.get("difficulty") == difficulty),
                "resolve_rate": round(
                    sum(
                        s.get("resolve_rate", 0)
                        for s in all_scores
                        if s.get("difficulty") == difficulty
                    )
                    / max(
                        sum(1 for s in all_scores if s.get("difficulty") == difficulty),
                        1,
                    ),
                    3,
                ),
            }
            for difficulty in ["easy", "medium", "hard"]
        },
        "instances": all_scores,
    }

    out_path = RESULTS_DIR / "baseline_results.json"
    json.dump(summary, open(out_path, "w"), indent=2)
    print(f"\n{'=' * 60}")
    print(f"BASELINE RESULTS → {out_path}")
    print(f"  Resolved:      {summary['resolved']}/{summary['total_tasks']}")
    print(f"  Resolve rate:  {summary['resolve_rate']}")
    print(f"  Avg regressions: {summary['avg_regression']}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
