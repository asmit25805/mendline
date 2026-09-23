"""
main.py — Entry Point
Orchestrates the full pipeline:
Fetch repos → Pull code + README → Classify → Analyze → Report → Log
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.fetcher        import fetch_trending_repos
from src.code_puller    import pull_code_files
from src.analyzer       import analyze_repo, classify_repo
from src.reporter       import open_issues
from src.issue_resolver import resolve_repo_issues, resolve_searched_issues
from src.database       import init_db, get_already_scanned, mark_repo_scanned, print_stats
from config import ENABLE_ISSUE_RESOLUTION, ENABLE_ISSUE_SEARCH


def run_audit():
    print("=" * 55)
    print("  🤖 MENDLINE — Starting Run")
    print("=" * 55)

    init_db()

    already_seen = get_already_scanned()
    print(f"\n[Main] {len(already_seen)} repo(s) already scanned — will skip these.\n")

    repos = fetch_trending_repos(already_seen)

    if not repos:
        print("[Main] No new repos to audit today from the trending fetcher.")

    handled_repos_this_run = set()

    for repo in repos:
        full_name = repo["full_name"]
        handled_repos_this_run.add(full_name)
        print(f"\n{'─' * 55}")
        print(f"  📦 Auditing: {full_name}  (★{repo['stars']:,})")
        print(f"{'─' * 55}")

        # Pull code files + README
        files, readme, file_tree = pull_code_files(repo)

        if not files:
            print(f"[Main] No analyzable files found in {full_name}. Marking as scanned to skip in future.")
            mark_repo_scanned(full_name, repo["stars"], [], None)
            continue

        # Classify repo using Gemini + README
        print("\n[Main] Classifying repo type...")
        repo_context = classify_repo(readme, file_tree)

        # Analyze files with full pipeline
        findings = analyze_repo(files, repo_context)

        # Original file contents, keyed by path — needed to generate + verify fixes
        file_lookup = {f["path"]: f["content"] for f in files}

        # Open one issue per finding (max 3) — with a verified fix + PR where possible
        results = open_issues(full_name, findings, file_lookup)
        issue_urls = [r["issue_url"] for r in results]
        pr_urls    = [r["pr_url"] for r in results if r.get("pr_url")]

        # Also look at the repo's OWN open issues and try to fix the unclaimed,
        # bug-shaped ones — separate from the self-discovery findings above.
        if ENABLE_ISSUE_RESOLUTION:
            resolved = resolve_repo_issues(full_name, file_tree)
            if resolved:
                print(f"[Main] Resolved {len(resolved)} existing issue(s) on {full_name}.")
                issue_urls.extend(f["issue_url"] for f in resolved)
                pr_urls.extend(f["pr_url"] for f in resolved if f.get("pr_url"))
                findings.extend(resolved)  # so they're captured in findings_log too

        if pr_urls:
            print(f"[Main] Opened {len(pr_urls)} PR(s) this run.")

        # Log to DB
        mark_repo_scanned(
            full_name,
            repo["stars"],
            findings,
            issue_urls[0] if issue_urls else None,
            pr_urls[0] if pr_urls else None,
        )

    # Cross-GitHub issue search — finds + fixes issues on repos beyond the
    # ones fetch_trending_repos happened to pick this run, since a repo
    # being "trending" says nothing about whether it has a fixable issue
    # in it. Runs regardless of whether the trending fetcher found anything.
    if ENABLE_ISSUE_SEARCH:
        print(f"\n{'=' * 55}")
        print("  🔎 Searching GitHub directly for fixable issues")
        print(f"{'=' * 55}")

        searched = resolve_searched_issues(already_seen, handled_repos_this_run)
        for entry in searched:
            repo_full_name = entry["repo_full_name"]
            repo_findings  = entry["findings"]
            issue_urls = [f["issue_url"] for f in repo_findings]
            pr_urls    = [f["pr_url"] for f in repo_findings if f.get("pr_url")]

            if pr_urls:
                print(f"[Main] Opened {len(pr_urls)} PR(s) on {repo_full_name} via search.")

            mark_repo_scanned(
                repo_full_name,
                entry["stars"],
                repo_findings,
                issue_urls[0] if issue_urls else None,
                pr_urls[0] if pr_urls else None,
            )

    print(f"\n{'=' * 55}")
    print("  ✅ Audit Run Complete")
    print(f"{'=' * 55}")
    print_stats()


if __name__ == "__main__":
    run_audit()
