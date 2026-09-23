"""
reporter.py — Module 4
Opens ONE GitHub issue per finding (max 3 per repo).
Written in a natural, developer-to-developer tone.
No mention of AI, bots, automation, or confidence scores.
Follows repo issue templates and CONTRIBUTING.md where found.
"""

import re
import difflib
import hashlib
import requests
import yaml
from github import Github, GithubException
from config import GITHUB_TOKEN, ENABLE_AUTO_FIX, GITHUB_HEADERS as HEADERS
from github_api import fetch_file
from fixer import generate_fix
from build_check import verify_fix
from pr_creator import create_fix_pr


gh = Github(GITHUB_TOKEN)

MAX_ISSUES_PER_REPO = 3


# ────────────────────────────────────────────────────────────
# Repo metadata fetching
# ────────────────────────────────────────────────────────────


def fetch_issue_template(owner: str, repo: str) -> tuple[str, str] | None:
    """
    Fetches the repo's bug report issue template.
    Returns (content, format) where format is "markdown" or "yaml" —
    GitHub's newer Issue Forms (.yml/.yaml, now the default GitHub
    suggests when you create a template via its UI) have a completely
    different structure than a plain Markdown template with HTML-comment
    placeholders, and need a different filler (see fill_yaml_template).
    Getting this distinction wrong is exactly how a maintainer ends up
    seeing the raw YAML source posted as an issue body instead of an
    actual report — this happened in practice before this distinction
    existed here.
    """
    # Direct filename attempts, markdown first (existing well-tested path),
    # then the YAML Issue Forms equivalents.
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
            return content, "markdown"

    for path in [
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yaml",
        ".github/ISSUE_TEMPLATE/bug-report.yml",
        ".github/ISSUE_TEMPLATE/bug.yml",
    ]:
        content = fetch_file(owner, repo, path)
        if content:
            print(f"[Reporter] Found issue form: {path}")
            return content, "yaml"

    # Try listing the directory
    try:
        url  = f"https://api.github.com/repos/{owner}/{repo}/contents/.github/ISSUE_TEMPLATE"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            for f in resp.json():
                name = f.get("name", "").lower()
                if not any(w in name for w in ["bug", "issue", "report"]):
                    continue
                fmt = "yaml" if name.endswith((".yml", ".yaml")) else "markdown" if name.endswith(".md") else None
                if fmt is None:
                    continue  # not a format we know how to fill — skip rather than guess
                r = requests.get(f.get("download_url", ""), timeout=10)
                if r.status_code == 200:
                    print(f"[Reporter] Found issue template: {f['name']}")
                    return r.text, fmt
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
    Fills a repo's Markdown issue template with finding data.
    Strips YAML front matter and fills HTML comment placeholders.
    """
    desc          = finding.get("description", "")
    suggested_fix = finding.get("fix", "")
    file_path     = finding.get("file_path", "")
    line_ref      = finding.get("line_reference", "")
    sev           = finding.get("severity", "medium")

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
            return suggested_fix
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


def fill_yaml_template(template_yaml: str, finding: dict, fix: dict | None = None, original_content: str | None = None) -> str | None:
    """
    Fills a GitHub Issue Forms YAML template. These can't be filled the
    way a Markdown template is — there's no HTML-comment placeholder to
    match, just structured form fields. This renders a plain Markdown
    body with one section per recognizable field, using each field's own
    label to decide what finding data belongs there.

    Returns None on anything unexpected (missing 'body' key, a field
    with no recognizable purpose, nothing fillable at all) rather than
    guessing — the caller falls back to the well-tested human-tone
    default in that case. This function exists specifically to fix a
    real failure mode: without it, fetch_issue_template could return a
    .yml Issue Form and this code would have no way to actually fill it,
    so the raw YAML source ended up posted as the issue body — which is
    exactly what happened on at least two repos before this existed.
    """
    try:
        parsed = yaml.safe_load(template_yaml)
    except Exception as e:
        print(f"[Reporter] Could not parse issue form YAML → {e}")
        return None

    if not isinstance(parsed, dict) or not isinstance(parsed.get("body"), list):
        return None

    desc          = finding.get("description", "")
    suggested_fix = finding.get("fix", "")
    file_path     = finding.get("file_path", "")
    line_ref      = finding.get("line_reference", "")

    def content_for(label: str, description_text: str) -> str | None:
        text = f"{label} {description_text}".lower()
        if any(w in text for w in ["what happened", "describe", "summary", "bug"]):
            return desc
        if any(w in text for w in ["reproduce", "steps", "how to"]):
            return (
                f"1. Look at `{line_ref}` in `{file_path}`\n"
                f"2. Trigger the code path described above\n"
                f"3. Observe: {desc[:150]}"
            )
        if "expected" in text:
            return "The code should handle this case safely."
        if any(w in text for w in ["actual", "instead", "observed"]):
            return desc
        if any(w in text for w in ["fix", "suggest", "solution", "workaround"]):
            return suggested_fix
        if any(w in text for w in ["version", "environment", "platform"]):
            return "N/A"
        return None  # unrecognized field — leave it out rather than guess

    sections = []
    for field in parsed["body"]:
        if not isinstance(field, dict) or field.get("type") in ("markdown", None):
            continue  # static boilerplate text in the form, not a real field to fill
        attrs = field.get("attributes") or {}
        label = attrs.get("label", "")
        if not label:
            continue
        answer = content_for(label, attrs.get("description", ""))
        if answer:
            sections.append(f"## {label}\n\n{answer}")

    if not sections:
        return None  # nothing recognizable to fill — don't post an empty shell

    filled = "\n\n".join(sections)
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
    template_result = fetch_issue_template(owner, repo_name)
    has_contrib     = check_contributing(owner, repo_name)

    if template_result:
        print("[Reporter] Using issue template.")
    else:
        print("[Reporter] No template found — using default format.")

    if has_contrib:
        print("[Reporter] CONTRIBUTING.md found — will acknowledge it.")

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

        body = None
        if template_result:
            template_content, template_format = template_result
            if template_format == "yaml":
                body = fill_yaml_template(template_content, finding, fix=fix, original_content=original_content)
            else:
                body = fill_template(template_content, finding, fix=fix, original_content=original_content)
            if body is None:
                print("[Reporter] Template didn't fill cleanly — falling back to default format.")

        if body is None:
            body = format_human_body(finding, has_contributing=has_contrib, fix=fix, original_content=original_content)

        try:
            issue = repo.create_issue(title=title, body=body)
            print(f"[Reporter] ✅ Issue opened → {issue.html_url}")
        except GithubException as e:
            if e.status == 410:
                print(f"[Reporter] Issues disabled on {repo_full_name} — stopping.")
                break
            elif e.status == 403:
                print("[Reporter] No permission to open issues — stopping.")
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
