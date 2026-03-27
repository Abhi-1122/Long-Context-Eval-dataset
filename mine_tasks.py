#!/usr/bin/env python3
"""
PR Miner for Long-Context Benchmark Tasks
Mines real merged PRs that enforce multi-file reasoning.

Usage:
    pip install PyGithub rich
    export GITHUB_TOKEN=ghp_your_token_here
    python mine_tasks.py
"""

import os, json, time, re
from dataclasses import dataclass, asdict
from typing import Optional
from github import Github, GithubException
from rich.console import Console
from rich.table import Table

console = Console()

# ── Config ──────────────────────────────────────────────────────────────────

TARGET_REPOS = [
    "pallets/flask",
    "tiangolo/fastapi",
    "psf/requests",
    "encode/httpx",
    "sqlalchemy/sqlalchemy",
    "pydantic/pydantic",
    "celery/celery",
    "aio-libs/aiohttp",
    "django/django",
    "pytest-dev/pytest",
]

FILTERS = {
    "min_additions":       80,
    "min_deletions":       20,
    "min_source_files":    2,
    "min_test_files":      1,
    "max_total_files":     15,
    "min_cross_file_hops": 2,
    "keywords_required":   ["fix", "bug", "error", "issue", "resolve", "closes"],
    "keywords_excluded":   ["docs", "readme", "typo", "chore", "bump", "release",
                            "changelog", "ci:", "style:", "refactor:"],
}

NUM_TASKS_TO_FIND = 2

# ── Data class ───────────────────────────────────────────────────────────────

@dataclass
class TaskCandidate:
    instance_id:           str
    repo:                  str
    pr_number:             int
    pr_title:              str
    pr_url:                str
    base_commit:           str
    fix_commit:            str
    problem_statement:     str
    source_files:          list
    test_files:            list
    all_files:             list
    additions:             int
    deletions:             int
    cross_file_hops:       int
    estimated_ctx_tokens:  int
    reasoning_depth:       int
    difficulty:            str
    language_stack:        list
    task_category:         str
    score:                 float

# ── Helpers ──────────────────────────────────────────────────────────────────

def is_test_file(filename):
    return any(x in filename for x in ["test_", "_test.", "/tests/", "/test/", "spec."])

def is_source_file(filename):
    exts = (".py", ".ts", ".js", ".go", ".rs", ".java", ".cpp", ".c", ".rb")
    return (filename.endswith(exts) and not is_test_file(filename)
            and not any(x in filename for x in ["setup.py", "conf.py", "migrations/"]))

def infer_language_stack(files):
    ext_map = {
        ".py": "Python",  ".ts": "TypeScript", ".js": "JavaScript",
        ".go": "Go",      ".rs": "Rust",        ".java": "Java",
        ".yml": "YAML",   ".yaml": "YAML",      ".toml": "TOML",
        ".json": "JSON",
    }
    langs = set()
    for f in files:
        for ext, lang in ext_map.items():
            if f.endswith(ext):
                langs.add(lang)
    return sorted(langs)

def infer_task_category(pr_title, pr_body):
    text = (pr_title + " " + (pr_body or "")).lower()
    if any(w in text for w in ["fix", "bug", "error", "regression", "crash"]):
        return "bug_fix"
    if any(w in text for w in ["add", "implement", "support", "feature"]):
        return "feature_integration"
    return "bug_fix"

def count_cross_file_hops(file_list):
    """Pairs of source files in the same package = likely imports between them."""
    hops = 0
    src = [f for f in file_list if is_source_file(f)]
    for i, f1 in enumerate(src):
        for f2 in src[i+1:]:
            p1 = f1.split('/')[:-1]
            p2 = f2.split('/')[:-1]
            if p1 and p2 and p1[0] == p2[0]:
                hops += 1
    return hops

def estimate_tokens(files_meta):
    total_lines = sum(f.additions + f.deletions for f in files_meta)
    return (total_lines * 60) // 4

def infer_difficulty(hops, tokens, n_source):
    if hops <= 2 and tokens < 20000 and n_source <= 3:
        return "easy"
    if hops <= 5 and tokens < 60000 and n_source <= 6:
        return "medium"
    return "hard"

def compute_reasoning_depth(hops, tokens):
    d = 1
    if hops >= 2:   d += 1
    if hops >= 4:   d += 1
    if tokens >= 20000: d += 1
    if tokens >= 60000: d += 1
    return min(d, 5)

def score_candidate(c):
    s = 0.0
    s += min(c.cross_file_hops, 8) * 2.0
    s += min(len(c.source_files), 6) * 1.5
    s += min(c.additions / 50, 4) * 1.0
    s += 1.0 if c.task_category == "bug_fix" else 0.5
    s += min(c.estimated_ctx_tokens / 10000, 5)
    s -= max(len(c.all_files) - 10, 0) * 0.5
    return round(s, 2)

def build_problem_statement(pr, source_files, test_files):
    body = (pr.body or '').strip()
    body = re.sub(r'<!--.*?-->', '', body, flags=re.DOTALL)
    body = re.sub(r'!\[.*?\]\(.*?\)', '', body)
    body = body[:2000].strip()
    files_hint = chr(10).join(f'- {f}' for f in source_files[:8])
    return f'{pr.title}\n\n{body}\n\nFiles likely relevant:\n{files_hint}'

# ── Core miner ───────────────────────────────────────────────────────────────

def mine_repo(repo_name, g, max_prs=200):
    candidates = []
    console.print(f'\n[bold cyan]Mining {repo_name}[/bold cyan]')
    try:
        repo = g.get_repo(repo_name)
    except GithubException as e:
        console.print(f'  [red]Could not access {repo_name}: {e}[/red]')
        return []

    pulls = repo.get_pulls(state='closed', sort='updated', direction='desc')
    checked = 0
    for pr in pulls:
        if checked >= max_prs:
            break
        checked += 1
        if not pr.merged:
            continue
        title_lower = (pr.title or '').lower()
        body_lower  = (pr.body  or '').lower()
        combined    = title_lower + ' ' + body_lower
        if not any(k in combined for k in FILTERS['keywords_required']):
            continue
        if any(k in title_lower for k in FILTERS['keywords_excluded']):
            continue
        if pr.additions < FILTERS['min_additions']:
            continue
        if pr.deletions < FILTERS['min_deletions']:
            continue
        try:
            files_meta = list(pr.get_files())
        except GithubException:
            continue
        all_filenames    = [f.filename for f in files_meta]
        source_files     = [f.filename for f in files_meta if is_source_file(f.filename)]
        test_files_list  = [f.filename for f in files_meta if is_test_file(f.filename)]
        if len(source_files)   < FILTERS['min_source_files']:
            continue
        if len(test_files_list)< FILTERS['min_test_files']:
            continue
        if len(all_filenames)  > FILTERS['max_total_files']:
            continue
        hops = count_cross_file_hops(source_files)
        if hops < FILTERS['min_cross_file_hops']:
            continue
        tokens   = estimate_tokens(files_meta)
        depth    = compute_reasoning_depth(hops, tokens)
        diff     = infer_difficulty(hops, tokens, len(source_files))
        langs    = infer_language_stack(all_filenames)
        category = infer_task_category(pr.title, pr.body)
        problem  = build_problem_statement(pr, source_files, test_files_list)
        instance_id = f"{repo_name.replace('/', '__')}__{category}__{pr.number:04d}"
        c = TaskCandidate(
            instance_id=instance_id, repo=repo_name, pr_number=pr.number,
            pr_title=pr.title, pr_url=pr.html_url,
            base_commit=pr.base.sha, fix_commit=pr.merge_commit_sha or '',
            problem_statement=problem, source_files=source_files,
            test_files=test_files_list, all_files=all_filenames,
            additions=pr.additions, deletions=pr.deletions,
            cross_file_hops=hops, estimated_ctx_tokens=tokens,
            reasoning_depth=depth, difficulty=diff,
            language_stack=langs, task_category=category, score=0.0,
        )
        c.score = score_candidate(c)
        candidates.append(c)
        console.print(
            f'  [green]✓[/green] PR #{pr.number} | '
            f'src={len(source_files)} hops={hops} score={c.score} | {pr.title[:55]}'
        )
        time.sleep(0.5)
    return candidates

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        console.print('[red]Set GITHUB_TOKEN environment variable first.[/red]')
        console.print('  export GITHUB_TOKEN=ghp_your_token_here')
        return
    g = Github(token)
    console.print(f'[bold]Authenticated:[/bold] {g.get_user().login}')
    rate_limit = g.get_rate_limit()
    core = getattr(rate_limit, 'core', None)
    if core is None:
        resources = getattr(rate_limit, 'resources', None)
        core = getattr(resources, 'core', None) if resources is not None else None
    remaining = getattr(core, 'remaining', '?')
    limit = getattr(core, 'limit', 5000)
    console.print(f'[bold]Rate limit:[/bold] {remaining}/{limit}')
    all_candidates = []
    for repo_name in TARGET_REPOS:
        candidates = mine_repo(repo_name, g, max_prs=200)
        all_candidates.extend(candidates)
        if len(all_candidates) >= 20:
            break
    if not all_candidates:
        console.print('[red]No candidates found. Try loosening filters.[/red]')
        return
    all_candidates.sort(key=lambda c: c.score, reverse=True)
    selected = all_candidates[:NUM_TASKS_TO_FIND]
    table = Table(title=f'Top {NUM_TASKS_TO_FIND} Task Candidates')
    for col, style in [('Instance ID','cyan'),('PR','blue'),('Difficulty','magenta'),
                        ('Src Files','right'),('Hops','right'),('~Tokens','right'),
                        ('Score','green'),('Title','')]:
        table.add_column(col, style=style if style not in ('right','') else None,
                         justify='right' if style == 'right' else 'left')
    for c in selected:
        table.add_row(
            c.instance_id, f'#{c.pr_number}', c.difficulty,
            str(len(c.source_files)), str(c.cross_file_hops),
            f'{c.estimated_ctx_tokens:,}', str(c.score), c.pr_title[:40],
        )
    console.print(table)
    os.makedirs('dataset', exist_ok=True)
    out_path = 'dataset/mined_tasks.jsonl'
    with open(out_path, 'w') as f:
        for c in selected:
            task = {
                'instance_id':      c.instance_id,
                'repo':             c.repo,
                'pr_number':        c.pr_number,
                'pr_url':           c.pr_url,
                'base_commit':      c.base_commit,
                'fix_commit':       c.fix_commit,
                'problem_statement':c.problem_statement,
                'files_involved':   c.source_files,
                'test_files':       c.test_files,
                'FAIL_TO_PASS':     [],
                'PASS_TO_PASS':     [],
                'task_category':    c.task_category,
                'difficulty':       c.difficulty,
                'reasoning_depth':  c.reasoning_depth,
                'cross_file_hops':  c.cross_file_hops,
                'context_requirement': (
                    'short'  if c.estimated_ctx_tokens < 10000 else
                    'medium' if c.estimated_ctx_tokens < 40000 else
                    'long'   if c.estimated_ctx_tokens < 80000 else 'extreme'
                ),
                'estimated_context_tokens': c.estimated_ctx_tokens,
                'language_stack':   c.language_stack,
                'score':            c.score,
                'environment_setup': {
                    'install': 'pip install -e \'.[dev]\'',
                    'test_cmd': 'pytest -x -v',
                    'python': '3.11'
                },
                '_next_step': 'Run validate_tasks.py to confirm FAIL_TO_PASS'
            }
            f.write(json.dumps(task) + '\n')
    console.print(f'[bold green]✓ Written {len(selected)} tasks to {out_path}[/bold green]')
    for c in selected:
        task_dir = f'dataset/tasks/{c.instance_id}'
        os.makedirs(task_dir, exist_ok=True)
        with open(f'{task_dir}/problem_statement.txt', 'w') as f:
            f.write(c.problem_statement)
    console.print('[bold yellow]Next:[/bold yellow] run validate_tasks.py')

if __name__ == '__main__':
    main()