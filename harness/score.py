import json
import shlex
import subprocess
import tempfile
from pathlib import Path


def run_tests(repo_path: str, test_cmd: str, patch_path: str, test_patch_path: str) -> dict:
    """Apply patches and run tests, return structured results."""
    result = {"patch_valid": False, "test_results": {}, "stdout": "", "stderr": ""}

    # Check patch applies cleanly
    check = subprocess.run(
        ["git", "apply", "--check", patch_path],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    result["patch_valid"] = check.returncode == 0

    if not result["patch_valid"]:
        result["stderr"] = check.stderr
        return result

    try:
        # Apply test patch first, then agent patch
        subprocess.run(["git", "apply", test_patch_path], cwd=repo_path, check=True)
        subprocess.run(["git", "apply", patch_path], cwd=repo_path, check=True)

        report_path = Path(tempfile.gettempdir()) / f"pytest_report_{Path(repo_path).name}.json"

        # Run tests with JSON report
        test_result = subprocess.run(
            shlex.split(test_cmd)
            + ["--json-report", f"--json-report-file={report_path}", "-v"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        result["stdout"] = test_result.stdout
        result["stderr"] = test_result.stderr

        try:
            report = json.load(open(report_path))
            result["test_results"] = {t["nodeid"]: t["outcome"] for t in report.get("tests", [])}
        except Exception:
            result["test_results"] = parse_pytest_stdout(test_result.stdout)
    except subprocess.CalledProcessError as error:
        result["stderr"] = str(error)

    return result


def parse_pytest_stdout(stdout: str) -> dict:
    results = {}
    for line in stdout.splitlines():
        if " PASSED" in line:
            results[line.split()[0]] = "passed"
        elif " FAILED" in line:
            results[line.split()[0]] = "failed"
        elif " ERROR" in line:
            results[line.split()[0]] = "error"
    return results


def score_task(instance: dict, test_results: dict, agent_stats: dict, patch_valid: bool) -> dict:
    passed = {k for k, v in test_results.items() if v == "passed"}

    fail_to_pass = set(instance["FAIL_TO_PASS"])
    pass_to_pass = set(instance["PASS_TO_PASS"])

    f2p_passed = fail_to_pass & passed
    p2p_passed = pass_to_pass & passed

    resolve_rate = len(f2p_passed) / len(fail_to_pass) if fail_to_pass else 0
    regression = 1 - (len(p2p_passed) / len(pass_to_pass)) if pass_to_pass else 0
    resolved = fail_to_pass.issubset(passed) and pass_to_pass.issubset(passed)

    return {
        "instance_id": instance["instance_id"],
        "difficulty": instance["difficulty"],
        "reasoning_depth": instance["reasoning_depth"],
        "cross_file_hops": instance["cross_file_hops"],
        "estimated_ctx_tokens": instance["estimated_context_tokens"],
        "patch_valid": patch_valid,
        "resolved": resolved,
        "resolve_rate": round(resolve_rate, 3),
        "regression_rate": round(regression, 3),
        "fail_to_pass_detail": {t: (t in passed) for t in fail_to_pass},
        "pass_to_pass_detail": {t: (t in passed) for t in pass_to_pass},
        "agent_turns": agent_stats.get("turns", "unknown"),
        "agent_tokens_used": agent_stats.get("tokens_used", "unknown"),
        "files_touched": [],
    }
