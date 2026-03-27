# Long-Context Eval Dataset — Issue Mining + Benchmark Harness

I built this project to do two things end to end:

1. Mine realistic bug-fix issues from real repositories
2. Benchmark coding agents on those mined issues in a reproducible way

The benchmark is not just pass/fail. I also track how well the agent navigates the codebase, avoids regressions, and uses context efficiently.

---

## What’s in this repo

```text
Long-Context-Eval-dataset/
├── mine_tasks.py
├── validate_tasks.py
├── dataset/
│   ├── tasks.jsonl
│   └── tasks/
├── harness/
│   ├── run_eval.py
│   ├── score.py
│   └── agent_runner.sh
└── results/
    └── baseline_results.json
```

---

## How I mine issues

`mine_tasks.py` scans merged PRs from target repos and turns strong candidates into task entries.

### Signals I use to find good issues

I rank/filter PRs using structural signals instead of keyword-only matching:

- **Change size:** minimum additions/deletions to avoid trivial fixes
- **Source + test coupling:** requires both source changes and test coverage
- **Cross-file reasoning:** estimates dependency hops across changed files
- **Context size estimate:** approximates how much code an agent must read
- **Issue intent:** title/body heuristics biased toward bug-fix style changes
- **File hygiene filters:** excludes docs/chore/release-only style PRs

From each candidate PR, the miner builds:

- `problem_statement.txt`
- metadata in `dataset/mined_tasks.jsonl`
- task folders under `dataset/tasks/<instance_id>/`

---

## How I validate mined issues

`validate_tasks.py` is the quality gate before I consider a mined issue benchmark-ready.

It checks whether each mined task is actually executable as an evaluation unit:

- repository/commit fields are valid
- referenced files and patches exist
- task structure is complete
- test expectations are coherent with the task definition

This prevents low-quality or broken mined entries from leaking into benchmark runs.

---

## Easy now, medium/hard next

Right now the mining heuristics are tuned to reliably surface easier bug-fix issues first.

In the next version, I will extend this to stronger medium/hard selection by tightening and enriching the scoring logic, including:

- higher cross-file hop thresholds
- larger expected context windows
- stronger architectural-change signals
- stricter regression-safety requirements
- optional manual review checkpoints for the top difficult candidates

The idea is to preserve quality while scaling difficulty, not just collect bigger patches.

---

## Benchmarking mined issues with the harness

Once issues are mined and validated, I plug them directly into the harness to generate benchmark results.

`harness/run_eval.py` orchestrates the full pipeline for each task:

### 1. Clone the target repo
Clones `https://github.com/<repo>` into `repos/<instance_id>` using a
blobless clone (`--filter=blob:none`) and checks out `base_commit`.
Already-cloned repos are skipped automatically.

### 2. Install dependencies
Runs `environment_setup.install` in the repo root. Use `--skip-install`
on reruns to avoid repeating this.

### 3. Load the problem statement
Reads `dataset/tasks/<task_dir>/problem_statement.txt` — this is the exact
prompt passed to the agent.

### 4. Run Gemini CLI
```bash
gemini \
  --prompt "<problem_statement>" \
  --no_interactive \
  --max_turns 30 \
  --output_format json
```

The following artifacts are captured into `results/runs/<instance_id>/`:

| File | Contents |
|------|----------|
| `agent_log.json` | Full structured agent output |
| `agent_stderr.txt` | Any error output from the agent process |
| `agent.patch` | `git diff HEAD` — everything the agent changed |
| `files_touched.txt` | `git diff --name-only HEAD` |
| `agent_stats.json` | Turns used, tokens used (if available in log) |

### 5. Reset, patch, and test
The repo is hard-reset to `base_commit`. Then:
1. `test_patch.diff` is applied — this adds the `FAIL_TO_PASS` tests
2. `agent.patch` is applied — the agent's fix
3. `test_cmd` runs via pytest with `--json-report`

This two-patch design means the test suite at evaluation time mirrors exactly
what will exist in the final benchmark tasks.

### 6. Score and aggregate
Per-task `result.json` is written, then all results are merged into
`results/baseline_results.json`.

---

## Scoring

Scoring is in `harness/score.py` and works in three layers.

### Layer 1 — Patch validity
```bash
git apply --check agent.patch
```
If the patch doesn't apply cleanly, `patch_valid=false` and test results
are left empty. This is its own failure mode worth tracking.

### Layer 2 — Test outcome extraction
Preferred: parse `pytest --json-report-file` output.  
Fallback: scan pytest stdout for `PASSED` / `FAILED` / `ERROR` markers.

### Layer 3 — Metrics
resolve_rate = |passed(FAIL_TO_PASS)| / |FAIL_TO_PASS|  
regression_rate = 1 - |passed(PASS_TO_PASS)| / |PASS_TO_PASS|  
resolved = all FAIL_TO_PASS passed AND all PASS_TO_PASS passed

Beyond pass/fail, I also track:
coverage_overlap = |files_touched ∩ files_expected| / |files_expected|

`coverage_overlap` is the key metric here. It shows whether the agent navigated
to the right parts of the codebase, not just whether it got lucky on output.

---

## Prerequisites

**System:** Linux or macOS with `git` and Python 3.11+

**Python packages:**
```bash
pip install pytest pytest-json-report pytest-asyncio PyGithub rich
```

**Gemini CLI:** Install and authenticate so `gemini` is available in `PATH`.
Authenticate via Google OAuth (not an API key) to stay on the free tier:
```bash
gemini auth login
gemini --help
```

---

## Typical workflow

```bash
# 1) Mine candidate issues
python mine_tasks.py

# 2) Validate mined tasks
python validate_tasks.py

# 3) Benchmark selected tasks
python harness/run_eval.py --tasks all --max-turns 30
```

You can also run subsets:

```bash
python harness/run_eval.py --tasks t001,t002 --max-turns 20
python harness/run_eval.py --tasks all --skip-install
```

---

## Outputs

### Global baseline
`results/baseline_results.json` — aggregate benchmark summary.

### Per-task run folder
`results/runs/<instance_id>/` contains all run artifacts, including
`result.json` with full per-task metrics.

---

## Troubleshooting

**`gemini` command not found**  
Install Gemini CLI globally (`npm install -g @google/gemini-cli`) and
confirm it's on `PATH`.

**Patch does not apply**  
Check `results/runs/<instance_id>/agent.patch`. The most common cause is
the repo not being at the expected base commit.

**Missing JSON test report**  
Make sure `pytest-json-report` is installed. The fallback stdout parser
activates automatically, but JSON is more reliable for multi-test outcomes.

**Slow reruns**  
Pass `--skip-install` once dependencies are already installed.

**Rate limiting on long runs**  
Long runs with high `--max-turns` may pause due to provider-side throttling.
I usually let them finish instead of interrupting mid-task.
