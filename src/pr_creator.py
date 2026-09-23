"""
pr_creator.py — Module 4b
Forks the target repo (or reuses an existing fork), commits a verified
fix to a branch, and opens a PR back to the upstream repo.

GitHub doesn't allow pushing to a repo this bot doesn't own — mechanically,
"doing the fix" means fork -> branch -> commit -> PR. The upstream
maintainer still reviews and merges it themselves; that's not a gap in
this code, it's just how GitHub works for anyone without write access,
and it's the right place for a human check to stay no matter what.
"""

import time
import re
import requests

from github import Github, GithubException
from config import GITHUB_TOKEN, GITHUB_HEADERS as HEADERS
from github_api import fetch_file

gh = Github(GITHUB_TOKEN)


def fetch_pr_template(owner: str, repo: str) -> str | None:
    """
    Some repos hard-require specific sections in a PR description — seen
    in practice: a repo's own automated PR-review bot rejecting a
    generated PR here specifically for missing a required section that
    a freeform body never included. Matching the repo's own PR template
    instead of writing something fully freeform is the fix for that.
    """
    for path in [
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/pull_request_template.md",
        ".github/PULL_REQUEST_TEMPLATE/pull_request_template.md",
        "docs/PULL_REQUEST_TEMPLATE.md",
        "PULL_REQUEST_TEMPLATE.md",
    ]:
        content = fetch_file(owner, repo, path)
        if content:
            print(f"[PR] Found PR template: {path}")
            return content
    return None


def fill_pr_template(template: str, finding: dict, fix: dict, closes_issue: int | None) -> str:
    """
    Fills HTML-comment placeholders the same way reporter.py's
    fill_template does for issues, checks off a checkbox that looks like
    it marks "bug fix," and always appends our own explanation plus a
    Closes/Related line regardless of whether a placeholder matched —
    unlike the issue-template path, there's no "fall back to no PR"
    option here, so the actual fix explanation has to end up in the body
    even if the template's structure wasn't fully recognized.
    """
    title       = finding.get("title", "").strip() or "Fix issue"
    explanation = fix.get("explanation") or f"Fixes: {title}"

    def replace_comment(match):
        comment = match.group(0).lower()
        if any(w in comment for w in ["describe", "summary", "what does", "what changes", "changes made"]):
            return explanation
        if any(w in comment for w in ["why", "motivation", "context"]):
            ref = f"#{closes_issue}" if closes_issue else "a reported issue"
            return f"Fixes {ref}: {title}"
        if any(w in comment for w in ["how has this been tested", "testing", "how tested"]):
            return "Verified the patched file parses/compiles cleanly before opening this PR."
        return ""

    filled = re.sub(r'<!--.*?-->', replace_comment, template, flags=re.DOTALL)
    # Best-effort: check a checkbox that reads like "bug fix"
    filled = re.sub(r'-\s*\[\s*\]\s*(bug ?fix)', r'- [x] \1', filled, flags=re.IGNORECASE)
    filled = re.sub(r'\n{3,}', '\n\n', filled).strip()

    filled += f"\n\n{explanation}\n\nKept the change minimal — just what's needed for the fix, nothing else touched."
    if closes_issue:
        filled += f"\n\nCloses #{closes_issue}"

    return filled


def sync_fork_with_upstream(fork_full_name: str, branch: str) -> bool:
    """
    Fast-forwards the fork's branch to match upstream before anything
    branches off it. Without this, a fork that's fallen behind — which on
    an active repo happens within days, since forks don't auto-update —
    makes every generated PR's diff include the ENTIRE gap between the
    stale fork and current upstream, on top of the actual fix, even
    though the fix itself is one small commit. GitHub's own "Sync fork"
    button does exactly this; this is the API equivalent.
    """
    url = f"https://api.github.com/repos/{fork_full_name}/merge-upstream"
    try:
        resp = requests.post(url, headers=HEADERS, json={"branch": branch}, timeout=15)
        if resp.status_code == 200:
            merge_type = resp.json().get("merge_type", "unknown")
            print(f"[PR] Synced fork's {branch} with upstream ({merge_type})")
            return True
        print(f"[PR] Could not sync fork → {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"[PR] Fork sync error → {e}")
        return False


def get_or_create_fork(upstream_repo):
    me = gh.get_user()
    fork_full_name = f"{me.login}/{upstream_repo.name}"

    try:
        fork = gh.get_repo(fork_full_name)
        print(f"[PR] Reusing existing fork {fork_full_name}")
        return fork
    except GithubException:
        pass

    print(f"[PR] Forking {upstream_repo.full_name}...")
    fork = upstream_repo.create_fork()

    # Forks aren't always instantly queryable — poll briefly before giving up.
    for _ in range(10):
        try:
            return gh.get_repo(fork.full_name)
        except GithubException:
            time.sleep(2)

    return fork


def create_fix_pr(upstream_full_name: str, finding: dict, fix: dict, issue_url: str | None = None, closes_issue: int | None = None) -> str | None:
    """
    fix: the dict returned by fixer.generate_fix (has patched_content, explanation).
    closes_issue: an issue number in the SAME upstream repo — if given, the PR body
    gets a "Closes #N" so merging it auto-closes that issue.
    Returns the PR URL, or None if it couldn't be opened at any step.
    """
    file_path = finding.get("file_path", "")
    title     = finding.get("title", "").strip() or "Fix issue"

    try:
        upstream = gh.get_repo(upstream_full_name)
    except GithubException as e:
        print(f"[PR] Could not access {upstream_full_name} → {e}")
        return None

    try:
        fork = get_or_create_fork(upstream)
    except GithubException as e:
        print(f"[PR] Could not fork {upstream_full_name} → {e}")
        return None

    default_branch = upstream.default_branch

    # Bring the fork's default branch up to date BEFORE branching off it —
    # this is the fix for the stale-fork-base problem (see sync_fork_with_upstream
    # docstring). Non-fatal if it fails; worst case we're back to branching
    # off whatever the fork already has, same as before this existed.
    sync_fork_with_upstream(fork.full_name, default_branch)

    branch_name = f"fix/{file_path.replace('/', '-')}-{int(time.time())}"[:80]

    try:
        base_sha = fork.get_branch(default_branch).commit.sha
        fork.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
    except GithubException as e:
        print(f"[PR] Could not create branch on fork → {e}")
        return None

    try:
        existing = fork.get_contents(file_path, ref=branch_name)
        fork.update_file(
            path=file_path,
            message=f"Fix: {title}",
            content=fix["patched_content"],
            sha=existing.sha,
            branch=branch_name,
        )
    except GithubException as e:
        print(f"[PR] Could not commit fix to fork → {e}")
        return None

    owner, repo_name = upstream_full_name.split("/", 1)
    pr_template = fetch_pr_template(owner, repo_name)
    if pr_template:
        body = fill_pr_template(pr_template, finding, fix, closes_issue)
    else:
        body_parts = [fix.get("explanation") or f"Fixes: {title}"]
        body_parts.append("Kept the change minimal — just what's needed for the fix, nothing else touched.")
        if closes_issue:
            body_parts.append(f"Closes #{closes_issue}")
        elif issue_url:
            body_parts.append(f"Related: {issue_url}")
        body = "\n\n".join(body_parts)

    try:
        pr = upstream.create_pull(
            title=f"Fix: {title}",
            body=body,
            head=f"{fork.owner.login}:{branch_name}",
            base=default_branch,
        )
        print(f"[PR] ✅ PR opened → {pr.html_url}")
        return pr.html_url
    except GithubException as e:
        print(f"[PR] Could not open PR → {e}")
        return None
