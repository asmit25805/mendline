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

from github import Github, GithubException
from config import GITHUB_TOKEN

gh = Github(GITHUB_TOKEN)


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
