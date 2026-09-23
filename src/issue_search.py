"""
issue_search.py — Module 2b
Searches GitHub's issue index directly for fixable-looking issues,
instead of only checking the issue tracker of whichever repos the
trending-repo fetcher (fetcher.py) happened to pick for self-discovery
bug-hunting.

fetcher.py picks repos by recency + stars — a good signal for "is this
repo worth auditing," but it says nothing about whether that repo's
OWN issue tracker has anything fixable in it this run. In production,
plenty of trending repos turn up zero candidate issues, or issues that
don't map to any locatable file. This searches for the issues
themselves, across GitHub, instead of hoping the repo we already
picked happens to have some.

GOTCHA (confirmed, not assumed): `stars:` is NOT a working qualifier
on GitHub's issue-search endpoint — it silently degrades into a
literal text token instead of an actual filter. Star filtering here
happens as a separate, ordinary REST call per candidate repo instead.
"""

import requests

from config import SKIP_ISSUE_LABELS, ISSUE_SEARCH_RESULTS, GITHUB_HEADERS as HEADERS

SEARCH_URL = "https://api.github.com/search/issues"


def build_query() -> str:
    """
    is:issue is:open no:assignee, minus every label in SKIP_ISSUE_LABELS.
    Deliberately does NOT include stars: — see module docstring.
    """
    exclusions = " ".join(
        f'-label:"{label}"' if " " in label else f"-label:{label}"
        for label in sorted(SKIP_ISSUE_LABELS)
    )
    return f"is:issue is:open no:assignee {exclusions}"


def search_candidate_issues(already_seen_repos: set[str]) -> list[dict]:
    """
    Returns [{"repo_full_name": ..., "number": ...}, ...], newest issue
    first, excluding repos already in already_seen_repos and excluding
    PRs (the issue-search endpoint returns both). Does NOT filter by
    star count — see module docstring for why that has to happen
    separately, per-repo.
    """
    params = {
        "q": build_query(),
        "sort": "created",
        "order": "desc",
        "per_page": min(ISSUE_SEARCH_RESULTS, 100),
    }

    try:
        resp = requests.get(SEARCH_URL, headers=HEADERS, params=params, timeout=15)
    except Exception as e:
        print(f"[IssueSearch] Search request failed → {e}")
        return []

    if resp.status_code != 200:
        print(f"[IssueSearch] GitHub search returned {resp.status_code}: {resp.text[:200]}")
        return []

    items = resp.json().get("items", [])
    candidates = []
    for item in items:
        if "pull_request" in item:
            continue  # search/issues returns PRs too; not what we want here
        repo_url = item.get("repository_url", "")
        repo_full_name = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else ""
        if not repo_full_name or repo_full_name in already_seen_repos:
            continue
        candidates.append({"repo_full_name": repo_full_name, "number": item.get("number")})

    print(f"[IssueSearch] {len(candidates)} candidate issue(s) found across GitHub (before star filter).")
    return candidates
