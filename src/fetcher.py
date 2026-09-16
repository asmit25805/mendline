"""
fetcher.py — Module 1
Fetches trending GitHub repositories using the GitHub Search API.
Skips forks, archived repos, and repos with no auditable source code.
"""

import requests
from datetime import datetime, timedelta, timezone
from config import GITHUB_TOKEN, TRENDING_DAYS, TRENDING_MIN_STARS, REPOS_PER_RUN


HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Repos whose primary language is in this set have no auditable code
SKIP_LANGUAGES = {
    None,
    "Markdown",
    "HTML",
    "CSS",
    "Jupyter Notebook",
    "Shell",
    "Dockerfile",
    "Batchfile",
    "PowerShell",
    "Rich Text Format",
    "TeX",
    "YAML",
    "TOML",
    "JSON",
    "XML",
    "CSV",
    "Text",
}


def get_date_cutoff() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=TRENDING_DAYS)
    return cutoff.strftime("%Y-%m-%d")


def fetch_trending_repos(already_seen: set[str]) -> list[dict]:
    """
    Fetches trending repos from GitHub created in the last N days,
    sorted by stars. Skips already-seen, forks, archived, and
    repos whose primary language has no auditable source code.
    """
    cutoff_date = get_date_cutoff()

    url    = "https://api.github.com/search/repositories"
    params = {
        "q":        f"created:>{cutoff_date} stars:>{TRENDING_MIN_STARS}",
        "sort":     "stars",
        "order":    "desc",
        "per_page": 50,   # fetch more so we have options after filtering
    }

    print(f"[Fetcher] Searching repos created after {cutoff_date} with {TRENDING_MIN_STARS}+ stars...")

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"[Fetcher] ERROR: GitHub API request failed → {e}")
        return []

    data      = response.json()
    all_repos = data.get("items", [])
    print(f"[Fetcher] Found {len(all_repos)} repos. Filtering...")

    results = []
    for repo in all_repos:
        full_name = repo["full_name"]

        if full_name in already_seen:
            print(f"[Fetcher] Skipping {full_name} — already audited.")
            continue

        if repo.get("fork"):
            continue

        if repo.get("archived"):
            continue

        # Skip repos with no auditable source code language
        lang = repo.get("language")
        if lang in SKIP_LANGUAGES:
            print(f"[Fetcher] Skipping {full_name} — language '{lang}' has no auditable code.")
            continue

        results.append({
            "full_name":      full_name,
            "name":           repo["name"],
            "owner":          repo["owner"]["login"],
            "description":    repo.get("description", "No description"),
            "stars":          repo["stargazers_count"],
            "language":       lang,
            "url":            repo["html_url"],
            "default_branch": repo.get("default_branch", "main"),
        })

        if len(results) >= REPOS_PER_RUN:
            break

    print(f"[Fetcher] Selected {len(results)} repos to audit.")
    return results


# ── Quick test ───────────────────────────────────────────────
if __name__ == "__main__":
    repos = fetch_trending_repos(already_seen=set())
    if not repos:
        print("No repos found. Check your GITHUB_TOKEN in .env")
    else:
        print("\n── Trending Repos ──────────────────────────")
        for r in repos:
            print(f"  ★ {r['stars']:,}  {r['full_name']}  [{r['language']}]")
            print(f"     {r['description'][:80]}")
            print()
