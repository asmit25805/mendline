"""
issue_resolver.py — Module 3e
Instead of only hunting for new bugs, this looks at a repo's OWN open
issues and tries to fix the ones that look like actionable, unclaimed
bug reports.

For each candidate issue:
  1. Pick the file(s) actually relevant to it, using the issue text
     itself plus the repo's real file tree — NOT the bug-hunting
     file-scoring heuristic in code_puller.py, which is tuned for
     spotting new security-shaped problems, not for locating whatever
     a specific reported issue happens to be about.
  2. Confirm the described problem actually exists in that file,
     anchored to a real line/snippet — same reject-on-hallucination
     check verifier.py already applies to self-found findings.
  3. Hand the confirmed finding to the same fixer -> build_check ->
     pr_creator pipeline used for self-found bugs. There's no separate,
     less-verified path just because the "finding" came from an issue
     instead of a scan.

Deliberately conservative about which issues it touches: skips
anything already assigned, anything with a PR already referencing it,
and anything labeled as a question/feature-request rather than a bug.
The goal is "fix things nobody's already working on," not "race
maintainers and contributors to their own issues."
"""

import json
import requests
from github import Github, GithubException

from config import (
    GITHUB_TOKEN, MAX_ISSUES_TO_RESOLVE, MAX_OPEN_ISSUES_SCANNED,
    SKIP_ISSUE_LABELS, SUPPORTED_EXTENSIONS, TRENDING_MIN_STARS,
    MAX_SEARCH_REPOS_PER_RUN, GITHUB_HEADERS as HEADERS,
)
from analyzer import call_llm, sanitize_json_response
from verifier import verify_line_reference
from code_puller import fetch_file_content, fetch_repo_tree, should_skip_path
from fixer import generate_fix
from build_check import verify_fix
from pr_creator import create_fix_pr
from issue_search import search_candidate_issues

gh = Github(GITHUB_TOKEN)


def has_linked_pr(owner: str, repo_name: str, issue_number: int) -> bool:
    """Checks the issue's timeline for a PR that already references it."""
    url = f"https://api.github.com/repos/{owner}/{repo_name}/issues/{issue_number}/timeline"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return False
        for event in resp.json():
            if event.get("event") == "cross-referenced":
                source = event.get("source", {}).get("issue", {})
                if source.get("pull_request"):
                    return True
    except Exception:
        pass
    return False


def is_good_candidate_issue(owner: str, repo_name: str, issue) -> bool:
    """
    The actual filter, shared by both discovery paths below (per-repo
    listing and cross-GitHub search): not a PR, not already assigned,
    not labeled as a question/feature/etc., has enough body to go on,
    and nothing already references it.
    """
    if issue.pull_request is not None:
        return False  # this "issue" is actually a PR
    if issue.assignees:
        return False  # someone's already on it
    labels = {l.name.lower() for l in issue.labels}
    if labels & SKIP_ISSUE_LABELS:
        return False
    if not issue.body or len(issue.body.strip()) < 30:
        return False  # too little to go on
    if has_linked_pr(owner, repo_name, issue.number):
        return False  # a PR already references this issue
    return True


def pick_candidate_issues(owner: str, repo_name: str, gh_repo) -> list:
    """Fetches a repo's own open issues and filters down to unclaimed, bug-shaped ones."""
    candidates, scanned = [], 0

    try:
        issues = gh_repo.get_issues(state="open", sort="created", direction="asc")
    except GithubException as e:
        print(f"[IssueResolver] Could not list issues → {e}")
        return []

    for issue in issues:
        scanned += 1
        if scanned > MAX_OPEN_ISSUES_SCANNED:
            break
        if is_good_candidate_issue(owner, repo_name, issue):
            candidates.append(issue)
            if len(candidates) >= MAX_ISSUES_TO_RESOLVE:
                break

    return candidates


FILE_SELECT_PROMPT = """Given a GitHub issue and a list of file paths in the repository, return the paths of the 1-3 files MOST likely to contain the code this issue is about.

Respond ONLY with valid JSON, no markdown: {"paths": ["path/one.py", "path/two.py"]}
If nothing in the list looks relevant, respond: {"paths": []}
"""


def select_relevant_files(issue, file_tree_paths: list[str]) -> list[str]:
    # Only show the model real source files — not docs/lockfiles/images/vendor
    # dirs — so the 400-path budget below is spent on things it could actually
    # pick, not padding.
    source_exts = tuple(SUPPORTED_EXTENSIONS.keys())
    source_paths = [
        p for p in file_tree_paths
        if p.lower().endswith(source_exts) and not should_skip_path(p)
    ]
    tree_sample = source_paths[:400]
    messages = [
        {"role": "system", "content": FILE_SELECT_PROMPT},
        {"role": "user", "content": (
            f"Issue title: {issue.title}\n\n"
            f"Issue body:\n{(issue.body or '')[:2000]}\n\n"
            f"File paths:\n" + "\n".join(tree_sample)
        )},
    ]
    try:
        raw   = call_llm(messages)
        data  = json.loads(sanitize_json_response(raw))
        paths = data.get("paths", [])
    except Exception as e:
        print(f"[IssueResolver] File selection failed for issue #{issue.number} → {e}")
        return []

    # Reject hallucinated paths — only keep ones that actually exist in the tree.
    valid = [p for p in paths if p in file_tree_paths]
    return valid[:3]


FINDING_FROM_ISSUE_PROMPT = """You are confirming whether a reported GitHub issue is actually reproducible in the given file, and locating it precisely.

Respond ONLY with valid JSON, no markdown:

{
  "confirmed": true,
  "line_reference": "<exact function/variable name that exists in the file and is the site of the problem>",
  "title": "<short title>",
  "description": "<what's wrong — quote the exact problematic line in backticks>",
  "fix": "<concrete direction for a fix>",
  "severity": "critical" | "high" | "medium" | "low",
  "type": "bug" | "security"
}

If you cannot confirm the issue is real and locatable in the given file, respond exactly:
{"confirmed": false}
"""


def build_finding_from_issue(issue, file_path: str, file_content: str, language: str) -> dict | None:
    messages = [
        {"role": "system", "content": FINDING_FROM_ISSUE_PROMPT},
        {"role": "user", "content": (
            f"Issue title: {issue.title}\n\n"
            f"Issue body:\n{(issue.body or '')[:3000]}\n\n"
            f"File: {file_path}\n\n"
            f"File content:\n---\n{file_content}\n---"
        )},
    ]
    try:
        raw  = call_llm(messages)
        data = json.loads(sanitize_json_response(raw))
    except Exception as e:
        print(f"[IssueResolver] Could not confirm issue #{issue.number} in {file_path} → {e}")
        return None

    if not data.get("confirmed"):
        return None

    finding = {
        "type":           data.get("type", "bug"),
        "severity":       data.get("severity", "medium"),
        "confidence":     1.0,  # gets checked against real code right below, not taken on faith
        "line_reference": data.get("line_reference", ""),
        "title":          data.get("title", issue.title),
        "description":    data.get("description", ""),
        "fix":            data.get("fix", ""),
        "file_path":      file_path,
        "language":       language,
    }

    if not verify_line_reference(finding, file_content):
        print(f"[IssueResolver] Rejected — line reference for issue #{issue.number} not found verbatim in {file_path}.")
        return None

    return finding


def attempt_fix_for_issue(owner: str, repo_name: str, issue, file_tree_paths: list[str]) -> dict | None:
    """
    Given one already-vetted issue, runs the select-file -> confirm ->
    fix -> verify -> PR chain. Returns the finding dict (annotated with
    repo_full_name / issue_number / issue_url / pr_url) on success, or
    None. Shared by resolve_repo_issues (per-repo discovery) and
    resolve_searched_issues (cross-GitHub search discovery) — the
    discovery mechanism differs, the "now actually fix it" logic doesn't.
    """
    paths = select_relevant_files(issue, file_tree_paths)
    if not paths:
        print(f"[IssueResolver] No relevant file found for issue #{issue.number} — skipping.")
        return None

    finding, file_content = None, None
    for path in paths:
        content = fetch_file_content(owner, repo_name, path)
        if not content:
            continue
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        language = SUPPORTED_EXTENSIONS.get(ext, "")
        candidate_finding = build_finding_from_issue(issue, path, content, language)
        if candidate_finding:
            finding, file_content = candidate_finding, content
            break

    if not finding:
        print(f"[IssueResolver] Could not confirm issue #{issue.number} against any candidate file — skipping.")
        return None

    fix = generate_fix(finding, file_content)
    if not fix:
        return None

    ok, note = verify_fix(finding.get("language", ""), file_content, fix["patched_content"])
    print(f"[IssueResolver] Fix check for issue #{issue.number}: {'passed' if ok else 'FAILED'} — {note}")
    if not ok:
        return None

    repo_full_name = f"{owner}/{repo_name}"
    pr_url = create_fix_pr(repo_full_name, finding, fix, issue_url=issue.html_url, closes_issue=issue.number)
    if pr_url:
        try:
            issue.create_comment(f"Opened a fix for this: {pr_url}")
        except Exception:
            pass
        print(f"[IssueResolver] ✅ Fixed issue #{issue.number} → {pr_url}")
    else:
        print(f"[IssueResolver] Fix verified for issue #{issue.number} but the PR could not be opened.")

    finding["repo_full_name"] = repo_full_name
    finding["issue_number"]   = issue.number
    finding["issue_url"]      = issue.html_url
    finding["pr_url"]         = pr_url
    return finding


def resolve_repo_issues(repo_full_name: str, file_tree_paths: list[str]) -> list[dict]:
    """
    Looks at the repo's own open issues, tries to fix the unclaimed
    bug-shaped ones, and opens a PR (+ a short comment on the issue)
    for anything that survives the same gate self-found fixes go
    through. Silent (no comment) on issues it can't confirm or fix —
    there's no value in telling someone "I tried and failed" on their
    own bug report.

    Returns a list of finding dicts (same shape as self-found findings)
    each annotated with issue_number / issue_url / pr_url, so callers
    can log them the same way.
    """
    owner, repo_name = repo_full_name.split("/", 1)

    try:
        gh_repo = gh.get_repo(repo_full_name)
    except GithubException as e:
        print(f"[IssueResolver] Could not access {repo_full_name} → {e}")
        return []

    candidates = pick_candidate_issues(owner, repo_name, gh_repo)
    if not candidates:
        print(f"[IssueResolver] No unclaimed, bug-shaped open issues found on {repo_full_name}.")
        return []

    print(f"[IssueResolver] {len(candidates)} candidate issue(s) on {repo_full_name}.")

    results = []
    for issue in candidates:
        finding = attempt_fix_for_issue(owner, repo_name, issue, file_tree_paths)
        if finding:
            results.append(finding)

    return results


def resolve_searched_issues(already_seen_repos: set[str], already_handled_repos: set[str]) -> list[dict]:
    """
    Cross-GitHub counterpart to resolve_repo_issues: instead of only
    checking the tracker of a repo fetcher.py already picked, this
    searches GitHub's issue index directly for promising, unclaimed
    issues wherever they are, and tries to fix a bounded number of them.

    already_seen_repos: repos Mendline has ever scanned — skipped, per
    the existing one-shot-per-repo rule (get_already_scanned()).
    already_handled_repos: repos already processed elsewhere THIS run
    (the trending-repo path), so this doesn't duplicate that work.

    Returns one entry per repo actually considered (passed the star
    filter, wasn't already handled elsewhere this run):
      [{"repo_full_name", "stars", "findings": [finding, ...]}, ...]
    findings is empty for a repo where nothing panned out — callers
    should log/mark these the same way a zero-finding repo from the
    trending path gets logged, so it isn't retried forever.
    """
    raw_candidates = search_candidate_issues(already_seen_repos)
    if not raw_candidates:
        return []

    by_repo: dict[str, list[int]] = {}
    for c in raw_candidates:
        by_repo.setdefault(c["repo_full_name"], []).append(c["number"])

    considered = []
    repos_tried = 0

    for repo_full_name, issue_numbers in by_repo.items():
        if repos_tried >= MAX_SEARCH_REPOS_PER_RUN:
            break
        if repo_full_name in already_handled_repos:
            continue

        owner, repo_name = repo_full_name.split("/", 1)

        try:
            gh_repo = gh.get_repo(repo_full_name)
        except GithubException:
            continue

        # stars: doesn't work as a search qualifier (see issue_search.py) —
        # this ordinary REST call is the actual filter.
        if gh_repo.stargazers_count < TRENDING_MIN_STARS:
            continue

        repos_tried += 1
        print(f"[IssueResolver] Found via search: {repo_full_name} "
              f"(★{gh_repo.stargazers_count:,}), {len(issue_numbers)} candidate issue(s).")

        try:
            tree = fetch_repo_tree(owner, repo_name, gh_repo.default_branch)
            file_tree_paths = [item["path"] for item in tree if item.get("type") == "blob"]
        except Exception as e:
            print(f"[IssueResolver] Could not fetch file tree for {repo_full_name} → {e}")
            file_tree_paths = []

        repo_findings = []
        if file_tree_paths:
            for number in issue_numbers[:MAX_ISSUES_TO_RESOLVE]:
                try:
                    issue = gh_repo.get_issue(number=number)
                except GithubException:
                    continue
                if not is_good_candidate_issue(owner, repo_name, issue):
                    continue  # query-side label exclusion is a nice-to-have, not a guarantee — recheck here
                finding = attempt_fix_for_issue(owner, repo_name, issue, file_tree_paths)
                if finding:
                    repo_findings.append(finding)

        if not repo_findings:
            print(f"[IssueResolver] Nothing fixable found via search on {repo_full_name}.")

        considered.append({
            "repo_full_name": repo_full_name,
            "stars":          gh_repo.stargazers_count,
            "findings":       repo_findings,
        })

    return considered
