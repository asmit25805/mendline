"""
database.py — Module 5
SQLite database to track which repos have been audited.
Prevents duplicate issues on the same repo.
"""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(__file__).parent.parent / "audit_log.db"


def init_db():
    """Creates tables if they don't exist. Safe to call on every run."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scanned_repos (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name     TEXT    NOT NULL UNIQUE,
            scanned_at    TEXT    NOT NULL,
            findings_count INTEGER DEFAULT 0,
            issue_url     TEXT,
            stars         INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS findings_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_name   TEXT NOT NULL,
            file_path   TEXT,
            finding     TEXT,        -- stored as JSON string
            logged_at   TEXT NOT NULL
        )
    """)

    # Migrate older DBs that predate the auto-fix / PR feature.
    try:
        cursor.execute("ALTER TABLE scanned_repos ADD COLUMN pr_url TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.commit()
    conn.close()
    print(f"[Database] Initialized at {DB_PATH}")


def get_already_scanned() -> set[str]:
    """Returns a set of repo full_names that have already been scanned."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT full_name FROM scanned_repos")
    rows = cursor.fetchall()
    conn.close()
    return {row[0] for row in rows}


def mark_repo_scanned(repo_full_name: str, stars: int, findings: list[dict], issue_url: str | None, pr_url: str | None = None):
    """Records that a repo has been scanned with its findings."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    cursor.execute("""
        INSERT OR REPLACE INTO scanned_repos
        (full_name, scanned_at, findings_count, issue_url, stars, pr_url)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (repo_full_name, now, len(findings), issue_url, stars, pr_url))

    # Log each individual finding for your own records
    for finding in findings:
        cursor.execute("""
            INSERT INTO findings_log (repo_name, file_path, finding, logged_at)
            VALUES (?, ?, ?, ?)
        """, (repo_full_name, finding.get("file_path"), json.dumps(finding), now))

    conn.commit()
    conn.close()
    print(f"[Database] Logged {repo_full_name} with {len(findings)} finding(s).")


def get_stats() -> dict:
    """Returns summary stats about the audit history."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM scanned_repos")
    total_repos = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM scanned_repos WHERE issue_url IS NOT NULL")
    repos_with_issues = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM findings_log")
    total_findings = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM scanned_repos WHERE pr_url IS NOT NULL")
    repos_with_prs = cursor.fetchone()[0]

    cursor.execute("SELECT full_name, stars, findings_count, issue_url, pr_url FROM scanned_repos ORDER BY scanned_at DESC LIMIT 10")
    recent = cursor.fetchall()

    conn.close()

    return {
        "total_repos_scanned": total_repos,
        "repos_with_issues":   repos_with_issues,
        "repos_with_prs":      repos_with_prs,
        "total_findings":      total_findings,
        "recent_scans":        recent,
    }


def print_stats():
    """Prints a nice summary of your audit history."""
    stats = get_stats()
    print("\n── Audit History ──────────────────────────────")
    print(f"  Total repos scanned : {stats['total_repos_scanned']}")
    print(f"  Repos with issues   : {stats['repos_with_issues']}")
    print(f"  Repos with PRs      : {stats['repos_with_prs']}")
    print(f"  Total findings      : {stats['total_findings']}")
    print("\n  Recent scans:")
    for name, stars, count, url, pr_url in stats["recent_scans"]:
        issue_info = f"→ {url}" if url else "→ no issue (clean or low severity)"
        pr_info    = f"  [PR: {pr_url}]" if pr_url else ""
        print(f"    ★{stars:,}  {name}  [{count} findings]  {issue_info}{pr_info}")
    print()


# ── Quick test ───────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    already = get_already_scanned()
    print(f"Already scanned repos: {already or 'none yet'}")
    print_stats()
