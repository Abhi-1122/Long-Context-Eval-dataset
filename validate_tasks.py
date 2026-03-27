#!/usr/bin/env python3
"""
validate_tasks.py — confirms each mined task is actually valid:
  1. Tests FAIL at base_commit (before fix)
  2. Tests PASS at fix_commit (after fix)
  3. Fills in FAIL_TO_PASS and PASS_TO_PASS in the task schema

Usage:
    python validate_tasks.py
"""

import os, json, subprocess, tempfile, shutil
from pathlib import Path
from rich.console import Console

console = Console()
MINED_TASKS  = Path('dataset/mined_tasks.jsonl')
VALID_TASKS  = Path('dataset/tasks.jsonl')

def run(cmd, cwd, check=False):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)

def get_pytest_nodeids(stdout):
    """Parse pytest -v output for test node IDs and their outcomes."""
    results = {}
    for line in stdout.splitlines():
        line = line.strip()
        if ' PASSED' in line:
            results[line.split()[0]] = 'passed'
        elif ' FAILED' in line:
            results[line.split()[0]] = 'failed'
        elif ' ERROR' in line:
            results[line.split()[0]] = 'error'
    return results

def validate_task(task):
    """
    Clone the repo into a temp dir, checkout base_commit,
    run tests to find which fail, then checkout fix_commit,
    run tests to confirm they pass.
    """
    repo      = task['repo']
    base_sha  = task['base_commit']
    fix_sha   = task['fix_commit']
    test_files= task.get('test_files', [])
    test_cmd  = task['environment_setup']['test_cmd']
    install   = task['environment_setup']['install']

    if not fix_sha:
        console.print(f'  [red]No fix_commit — skipping[/red]')
        return None

    tmpdir = tempfile.mkdtemp(prefix='bench_validate_')
    console.print(f'  [dim]Working in {tmpdir}[/dim]')

    try:
        # ── Clone ────────────────────────────────────────────────────────
        console.print(f'  Cloning {repo}...')
        r = run(['git', 'clone', '--filter=blob:none',
                 f'https://github.com/{repo}', tmpdir], cwd='/')
        if r.returncode != 0:
            console.print(f'  [red]Clone failed: {r.stderr[:200]}[/red]')
            return None

        # ── Install deps ─────────────────────────────────────────────────
        console.print(f'  Installing deps...')
        run(install, cwd=tmpdir)

        # ── Run tests at base_commit ──────────────────────────────────────
        console.print(f'  Checking out base_commit {base_sha[:8]}...')
        run(['git', 'checkout', base_sha], cwd=tmpdir, check=True)

        if test_files:
            test_target = ' '.join(test_files)
            base_cmd = f'{test_cmd} {test_target} -v'
        else:
            base_cmd = f'{test_cmd} -v'

        console.print(f'  Running tests at base_commit...')
        base_result = run(base_cmd.split(), cwd=tmpdir)
        base_outcomes = get_pytest_nodeids(base_result.stdout)

        failing_at_base = [nid for nid, outcome in base_outcomes.items()
                           if outcome in ('failed', 'error')]
        passing_at_base = [nid for nid, outcome in base_outcomes.items()
                           if outcome == 'passed']

        console.print(f'  Base: {len(failing_at_base)} fail, {len(passing_at_base)} pass')

        if not failing_at_base:
            console.print('  [yellow]No tests fail at base_commit — task may not be valid[/yellow]')

        # ── Run tests at fix_commit ───────────────────────────────────────
        console.print(f'  Checking out fix_commit {fix_sha[:8]}...')
        run(['git', 'checkout', fix_sha], cwd=tmpdir, check=True)

        console.print(f'  Running tests at fix_commit...')
        fix_result   = run(base_cmd.split(), cwd=tmpdir)
        fix_outcomes = get_pytest_nodeids(fix_result.stdout)

        passing_at_fix = [nid for nid, outcome in fix_outcomes.items()
                          if outcome == 'passed']

        # ── Determine FAIL_TO_PASS and PASS_TO_PASS ───────────────────────
        # FAIL_TO_PASS: failed at base, passed at fix
        fail_to_pass = [nid for nid in failing_at_base if nid in passing_at_fix]
        # PASS_TO_PASS: passed at base, still pass at fix
        pass_to_pass = [nid for nid in passing_at_base if nid in passing_at_fix]

        is_valid = len(fail_to_pass) > 0
        console.print(
            f'  Fix: {len(fail_to_pass)} FAIL_TO_PASS, {len(pass_to_pass)} PASS_TO_PASS | '
            f'valid={is_valid}'
        )

        return {
            'FAIL_TO_PASS': fail_to_pass,
            'PASS_TO_PASS': pass_to_pass[:10],  # cap to 10 regression guards
            'valid': is_valid,
            'base_test_outcomes': base_outcomes,
            'fix_test_outcomes':  fix_outcomes,
        }

    except Exception as e:
        console.print(f'  [red]Validation error: {e}[/red]')
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def main():
    if not MINED_TASKS.exists():
        console.print(f'[red]{MINED_TASKS} not found. Run mine_tasks.py first.[/red]')
        return

    tasks = [json.loads(l) for l in open(MINED_TASKS) if l.strip()]
    console.print(f'[bold]Validating {len(tasks)} task(s)...[/bold]')

    valid_tasks = []
    for task in tasks:
        console.print(f'\n[bold cyan]Task: {task["instance_id"]}[/bold cyan]')
        result = validate_task(task)
        if result and result['valid']:
            task['FAIL_TO_PASS'] = result['FAIL_TO_PASS']
            task['PASS_TO_PASS'] = result['PASS_TO_PASS']
            task.pop('_next_step', None)
            valid_tasks.append(task)
            console.print(f'  [green]✓ Valid task — added to tasks.jsonl[/green]')
        else:
            console.print(f'  [red]✗ Invalid — excluded from final dataset[/red]')

    VALID_TASKS.parent.mkdir(parents=True, exist_ok=True)
    with open(VALID_TASKS, 'w') as f:
        for task in valid_tasks:
            f.write(json.dumps(task) + '\n')

    console.print(f'\n[bold green]Done: {len(valid_tasks)}/{len(tasks)} valid tasks[/bold green]')
    console.print(f'Written to {VALID_TASKS}')
    if valid_tasks:
        console.print('[bold yellow]Next:[/bold yellow] run harness/run_eval.py')

if __name__ == '__main__':
    main()