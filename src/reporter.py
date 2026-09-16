"""
reporter.py — Module 4
Opens ONE GitHub issue per finding (max 3 per repo).
Written in a natural, developer-to-developer tone.
No mention of AI, bots, automation, or confidence scores.
Follows repo issue templates and CONTRIBUTING.md where found.
"""

import re
import difflib
import base64
import hashlib
import requests
from github import Github, GithubException
from config import GITHUB_TOKEN, ENABLE_AUTO_FIX
from fixer import generate_fix
from build_check import verify_fix
from pr_creator import create_fix_pr


gh = Github(GITHUB_TOKEN)

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

MAX_ISSUES_PER_REPO = 3


# ────────────────────────────────────────────────────────────
# Repo metadata fetching
# ────────────────────────────────────────────────────────────

def fetch_file(owner: str, repo: str, path: str) -> str | None:
    """Generic helper to fetch a single file from a repo."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return base64.b64decode(resp.json()["content"]).decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


def fetch_issue_template(owner: str, repo: str) -> str | None:
    """
    Fetches the repo's bug report issue template.
    Tries common locations in order.
    """
    # Direct filename attempts
    for path in [
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/bug-report.md",
        ".github/ISSUE_TEMPLATE/bug.md",
        ".github/ISSUE_TEMPLATE/issue.md",
        ".github/ISSUE_TEMPLATE.md",
        ".github/bug_report.md",
    ]:
        content = fetch_file(owner, repo, path)
        if content:
            print(f"[Reporter] Found issue template: {path}")
            return content

    # Try listing the directory
    try:
        url  = f"https://api.github.com/repos/{owner}/{repo}/contents/.github/ISSUE_TEMPLATE"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            for f in resp.json():
                name = f.get("name", "").lower()
                if any(w in name for w in ["bug", "issue", "report"]):
                    r = requests.get(f.get("download_url", ""), timeout=10)
                    if r.status_code == 200:
                        print(f"[Reporter] Found issue template: {f['name']}")
                        return r.text
    except Exception:
        pass

    return None


def check_contributing(owner: str, repo: str) -> bool:
    """Returns True if repo has a CONTRIBUTING.md file."""
    for path in ["CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"]:
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=8)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
    return False


# ────────────────────────────────────────────────────────────
# Title generation
# ────────────────────────────────────────────────────────────

def format_title(finding: dict) -> str:
    """
    Plain, human-sounding title. No robot emoji, no severity labels.
    Varies slightly based on finding type to avoid all issues looking identical.
    """
    title = finding.get("title", "").strip()

    # Strip any emoji the LLM added
    title = re.sub(r'[^\x00-\x7F]', '', title).strip(" :-")

    if title:
        return title[0].upper() + title[1:]
    return "Potential issue found"


# ────────────────────────────────────────────────────────────
# Patch block — shown in the issue when a fix was generated + verified
# ────────────────────────────────────────────────────────────

def format_patch_block(file_path: str, original_content: str, patched_content: str) -> str:
    diff_lines = difflib.unified_diff(
        original_content.splitlines(keepends=True),
        patched_content.splitlines(keepends=True),
        fromfile=file_path,
        tofile=file_path,
    )
    diff_text = "".join(diff_lines)
    if not diff_text.strip():
        return ""
    return f"\n\n**Patch**\n\n```diff\n{diff_text}\n```"


# ────────────────────────────────────────────────────────────
# Body: fill repo template
# ────────────────────────────────────────────────────────────

def fill_template(template: str, finding: dict, fix: dict | None = None, original_content: str | None = None) -> str:
    """
    Fills a repo's issue template with finding data.
    Strips YAML front matter and fills HTML comment placeholders.
    """
    desc      = finding.get("description", "")
    fix       = finding.get("fix", "")
    file_path = finding.get("file_path", "")
    line_ref  = finding.get("line_reference", "")
    sev       = finding.get("severity", "medium")

    # Strip YAML front matter
    filled = re.sub(r'^---.*?---\s*', '', template, flags=re.DOTALL)

    def replace_comment(match):
        comment = match.group(0).lower()
        if any(w in comment for w in ["describe", "summary", "what happened", "what is", "bug"]):
            return desc
        if any(w in comment for w in ["reproduce", "steps", "how to"]):
            return (
                f"1. Look at `{line_ref}` in `{file_path}`\n"
                f"2. Trigger the code path described above\n"
                f"3. Observe: {desc[:150]}"
            )
        if any(w in comment for w in ["expected"]):
            return "The code should handle this case safely."
        if any(w in comment for w in ["actual", "instead", "happening", "observed"]):
            return desc
        if any(w in comment for w in ["fix", "suggest", "solution", "workaround"]):
            return fix
        if any(w in comment for w in ["version", "environment", "os", "platform", "node", "python"]):
            return "N/A"
        if any(w in comment for w in ["additional", "context", "info", "other"]):
            return f"Found in `{file_path}` at `{line_ref}`."
        return ""

    filled = re.sub(r'<!--.*?-->', replace_comment, filled, flags=re.DOTALL)

    # Check severity checkboxes if template has them
    for level in ["critical", "high", "medium", "low"]:
        if level == sev:
            filled = filled.replace(f"[ ] {level.capitalize()}", f"[x] {level.capitalize()}")
            filled = filled.replace(f"[ ] {level}", f"[x] {level}")

    # Clean up excess blank lines
    filled = re.sub(r'\n{3,}', '\n\n', filled).strip()

    # Always add file reference at the end
    filled += f"\n\n**File:** `{file_path}` — `{line_ref}`"

    if fix and original_content:
        filled += format_patch_block(file_path, original_content, fix["patched_content"])

    return filled


# ────────────────────────────────────────────────────────────
# Body: human-tone default (no template found)
# ────────────────────────────────────────────────────────────

def format_human_body(finding: dict, has_contributing: bool = False, fix: dict | None = None, original_content: str | None = None) -> str:
    """
    Natural, developer-to-developer issue body.
    No AI/bot mentions. No confidence scores. No markdown tables.
    Reads like someone who was reading the code and noticed something.
    Slightly varied openers so issues don't all look copy-pasted.
    """
    desc      = finding.get("description", "")
    file_path = finding.get("file_path", "")
    line_ref  = finding.get("line_reference", "")
    ftype     = finding.get("type", "bug")
    sev       = finding.get("severity", "medium")

    # Vary the opener based on type/severity/file to avoid templated feel
    # Use a hash of the file path to deterministically pick one
    # so the same file always gets the same opener (consistent)
    hash_val = int(hashlib.md5(file_path.encode()).hexdigest(), 16) % 6

    if ftype == "security" and sev in ("critical", "high"):
        openers = [
            f"Was going through `{file_path}` and found a security issue worth flagging.",
            f"Noticed something in `{file_path}` that looks like it could be exploited.",
            f"Found what looks like a security bug in `{file_path}`.",
        ]
    elif ftype == "security":
        openers = [
            f"Spotted something in `{file_path}` that could be worth tightening up.",
            f"Was reading `{file_path}` and noticed a potential security gap.",
            f"Found a minor security issue in `{file_path}` that might be worth addressing.",
        ]
    else:
        openers = [
            f"Was reading through `{file_path}` and noticed something that looked off.",
            f"Found what looks like a bug in `{file_path}`.",
            f"Spotted an issue in `{file_path}` while going through the code.",
        ]

    opener = openers[hash_val % len(openers)]

    # Contributing.md note — if repo has one, acknowledge it
    contributing_note = ""
    if has_contributing:
        contributing_note = "\n\nI've tried to follow your contribution guidelines — let me know if I've missed anything."

    suggested_fix = finding.get("fix", "")

    patch_block = ""
    closing = "Happy to open a PR if that would be useful."
    if fix and original_content:
        patch_block = format_patch_block(file_path, original_content, fix["patched_content"])
        closing = "Included a patch below, and opening a PR with this same change."

    body = f"""{opener}

**The issue**

{desc}

**Where**

`{line_ref}` in `{file_path}`

**Suggested fix**

{suggested_fix}

{closing}{contributing_note}{patch_block}"""

    return body


# ────────────────────────────────────────────────────────────
# Main issue opener
# ────────────────────────────────────────────────────────────

def open_issues(repo_full_name: str, findings: list[dict], file_lookup: dict[str, str] | None = None) -> list[dict]:
    """
    Opens one GitHub issue per finding (max MAX_ISSUES_PER_REPO).
    Uses repo's issue template if found, otherwise human-tone default.
    Only reports medium+ severity findings.

    If file_lookup (path -> original file content) is provided and
    ENABLE_AUTO_FIX is on, also generates + mechanically verifies a
    minimal fix for each finding, includes the patch in the issue body,
    and opens a PR with that same change. Fork -> branch -> PR is the
    only way to "do the fix" on a repo this bot doesn't own — GitHub
    won't allow a direct push, so a human maintainer always ends up
    reviewing and merging it, same as any other outside contribution.

    Returns a list of {"issue_url", "pr_url", "title"} dicts, one per
    issue actually opened.
    """
    if not findings:
        print(f"[Reporter] No findings for {repo_full_name} — skipping.")
        return []

    significant = [
        f for f in findings
        if f.get("severity") in ("critical", "high", "medium")
    ]

    if not significant:
        print(f"[Reporter] Only low-severity findings for {repo_full_name} — skipping.")
        return []

    to_report = significant[:MAX_ISSUES_PER_REPO]
    print(f"[Reporter] Opening {len(to_report)} issue(s) on {repo_full_name}...")

    try:
        repo = gh.get_repo(repo_full_name)
    except GithubException as e:
        print(f"[Reporter] Could not access {repo_full_name} → {e}")
        return []

    owner, repo_name = repo_full_name.split("/", 1)

    # Fetch repo metadata once
    template       = fetch_issue_template(owner, repo_name)
    has_contrib    = check_contributing(owner, repo_name)

    if template:
        print(f"[Reporter] Using issue template.")
    else:
        print(f"[Reporter] No template found — using default format.")

    if has_contrib:
        print(f"[Reporter] CONTRIBUTING.md found — will acknowledge it.")

    results = []

    for finding in to_report:
        file_path        = finding.get("file_path", "")
        original_content = file_lookup.get(file_path) if file_lookup else None

        # ── Generate + verify a fix before we even write the issue body ──
        fix = None
        if ENABLE_AUTO_FIX and original_content:
            fix = generate_fix(finding, original_content)
            if fix:
                ok, note = verify_fix(finding.get("language", ""), original_content, fix["patched_content"])
                print(f"[Reporter] Fix check for '{finding.get('title')}': "
                      f"{'passed' if ok else 'FAILED'} — {note}")
                if not ok:
                    fix = None

        title = format_title(finding)

        if template:
            body = fill_template(template, finding, fix=fix, original_content=original_content)
        else:
            body = format_human_body(finding, has_contributing=has_contrib, fix=fix, original_content=original_content)

        try:
            issue = repo.create_issue(title=title, body=body)
            print(f"[Reporter] ✅ Issue opened → {issue.html_url}")
        except GithubException as e:
            if e.status == 410:
                print(f"[Reporter] Issues disabled on {repo_full_name} — stopping.")
                break
            elif e.status == 403:
                print(f"[Reporter] No permission to open issues — stopping.")
                break
            else:
                print(f"[Reporter] GitHub error → {e.status}: {e.data}")
            continue
        except Exception as e:
            print(f"[Reporter] Unexpected error → {e}")
            continue

        # ── Fix passed verification and the issue exists — open the PR ──
        pr_url = None
        if fix:
            pr_url = create_fix_pr(repo_full_name, finding, fix, issue_url=issue.html_url)
            try:
                if pr_url:
                    issue.create_comment(f"Opened a PR with this fix: {pr_url}")
                else:
                    issue.create_comment(
                        "Tried to open a PR with the fix automatically but hit an error — "
                        "the patch above should still apply cleanly by hand."
                    )
            except Exception:
                pass

        results.append({"issue_url": issue.html_url, "pr_url": pr_url, "title": title})

    return results


# ── Quick test ───────────────────────────────────────────────
if __name__ == "__main__":
    sample = {
        "type":           "security",
        "severity":       "medium",
        "confidence":     0.95,
        "line_reference": "get_user",
        "title":          "SQL injection via f-string in get_user()",
        "description":    "User input is directly interpolated into a SQL query: `query = f\"SELECT * FROM users WHERE username = '{username}'\"`. An attacker can modify the query structure.",
        "fix":            "Use parameterized queries: `cursor.execute('SELECT * FROM users WHERE username = ?', (username,))`",
        "file_path":      "src/db.py",
        "language":       "Python",
    }
    print("=== TITLE ===")
    print(format_title(sample))
    print("\n=== BODY (no template, no contributing.md) ===")
    print(format_human_body(sample, has_contributing=False))
    print("\n=== BODY (with contributing.md) ===")
    print(format_human_body(sample, has_contributing=True))
