"""
main.py — Entry Point

Daily job: find trending repos, then look at each repo's OWN open issues
and try to confirm + fix the unclaimed, bug-shaped ones (fork -> branch ->
PR). This does NOT scan repos cold for new bugs anymore — that
self-discovery path (fetch code -> LLM finds bugs -> open new issues) has
been removed on purpose. The reasoning: cold bug-hunting turns up nothing
on most repos on most days, while a repo with any open issues at all gives
this something real to work on every run.

Everything here is now issue_resolver.py's job. code_puller.pull_code_files
(scored top-25-file fetch, for self-discovery) and analyzer.analyze_repo /
classify_repo (bug-finding + repo classification) are no longer called from
here — they still exist in the codebase but are unused dead code for this
build. reporter.py's open_issues() (posts NEW issues for self-found bugs)
is likewise never invoked now.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.fetcher        import fetch_trending_repos
from src.code_puller    import fetch_repo_tree
from src.issue_resolver import resolve_repo_issues
from src.database       import init_db, get_already_scanned, mark_repo_scanned, print_stats


def run_audit():
    print("=" * 55)
    print("  🤖 MENDLINE — Starting Run (issue-fix mode)")
    print("=" * 55)

    init_db()

    already_seen = get_already_scanned()
    print(f"\n[Main] {len(already_seen)} repo(s) already scanned — will skip these.\n")

    repos = fetch_trending_repos(already_seen)

    if not repos:
        print("[Main] No new repos to check today. Done.")
        print_stats()
        return

    for repo in repos:
        full_name = repo["full_name"]
        print(f"\n{'─' * 55}")
        print(f"  📦 Checking: {full_name}  (★{repo['stars']:,})")
        print(f"{'─' * 55}")

        # Only need the path list — issue_resolver uses it to figure out
        # which file an issue is probably about. No content/README fetch,
        # no file scoring: that machinery existed for self-discovery, which
        # this build doesn't do.
        print(f"\n[Main] Fetching file tree for {full_name}...")
        tree = fetch_repo_tree(repo["owner"], repo["name"], repo["default_branch"])
        file_tree = [item["path"] for item in tree if item.get("type") == "blob"]

        if not file_tree:
            print(f"[Main] Could not read a file tree for {full_name} — marking as scanned to skip in future.")
            mark_repo_scanned(full_name, repo["stars"], [], None)
            continue

        resolved = resolve_repo_issues(full_name, file_tree)

        issue_urls = [f["issue_url"] for f in resolved]
        pr_urls    = [f["pr_url"] for f in resolved if f.get("pr_url")]

        if pr_urls:
            print(f"[Main] Opened {len(pr_urls)} PR(s) on {full_name} this run.")
        elif not resolved:
            print(f"[Main] Nothing fixable found on {full_name} this run.")

        mark_repo_scanned(
            full_name,
            repo["stars"],
            resolved,
            issue_urls[0] if issue_urls else None,
            pr_urls[0] if pr_urls else None,
        )

    print(f"\n{'=' * 55}")
    print("  ✅ Run Complete")
    print(f"{'=' * 55}")
    print_stats()


if __name__ == "__main__":
    run_audit()
