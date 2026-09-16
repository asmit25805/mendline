"""
verifier.py — Module 3b
Verifies findings before they get reported.
Filters out hallucinated line references and confirms code actually exists.
"""

import ast
import re


def verify_line_reference(finding: dict, file_content: str) -> bool:
    """
    Checks that the line_reference in a finding actually exists in the file.
    Also checks that anything quoted in backticks in the description exists.
    """
    line_ref    = finding.get("line_reference", "").strip()
    content     = file_content
    content_lower = content.lower()

    if not line_ref:
        return False

    # Extract identifiers from line_reference
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{2,}', line_ref)
    meaningful = [t for t in tokens if t.lower() not in {
        "function", "method", "class", "variable", "line",
        "the", "and", "or", "in", "at", "on", "of", "to", "for",
    }]

    if meaningful:
        if not any(token.lower() in content_lower for token in meaningful):
            return False

    # Check backtick-quoted code in description actually exists
    quoted = re.findall(r'`([^`]+)`', finding.get("description", ""))
    for term in quoted:
        term_clean = term.strip().lower()
        # Skip very short terms or punctuation-only
        if len(term_clean) > 4 and re.search(r'[a-z]', term_clean):
            if term_clean not in content_lower:
                return False

    return True


def verify_python_syntax(content: str) -> bool:
    try:
        ast.parse(content)
        return True
    except SyntaxError:
        return False


def deduplicate_findings(all_findings: list[dict]) -> list[dict]:
    """
    Removes duplicate findings across files.
    Merges file paths when the same bug appears in multiple files.
    """
    if not all_findings:
        return []

    deduplicated = []
    seen_titles  = {}

    for finding in all_findings:
        title      = finding.get("title", "").strip().lower()
        norm_title = re.sub(r'[^a-z0-9 ]', '', title)
        norm_title = re.sub(
            r'\b(the|a|an|in|at|on|of|to|for|is|are|was)\b', '', norm_title
        )
        norm_title = re.sub(r'\s+', ' ', norm_title).strip()

        if norm_title in seen_titles:
            existing = deduplicated[seen_titles[norm_title]]
            new_path = finding.get("file_path", "")
            old_path = existing.get("file_path", "")
            if new_path and new_path != old_path:
                existing["file_path"]    = f"{old_path}, {new_path}"
                existing["description"] += f"\n\n*(Also found in: `{new_path}`)*"
        else:
            seen_titles[norm_title] = len(deduplicated)
            deduplicated.append(finding)

    removed = len(all_findings) - len(deduplicated)
    if removed:
        print(f"[Verifier] Removed {removed} duplicate finding(s).")

    return deduplicated


def verify_findings(findings: list[dict], file: dict) -> list[dict]:
    """
    Runs all verification checks on findings for a single file.
    Returns only findings that pass.
    """
    if not findings:
        return []

    content  = file["content"]
    language = file["language"]
    path     = file["path"]
    verified = []

    if language == "Python" and not verify_python_syntax(content):
        print(f"[Verifier] WARNING: {path} has a syntax error — findings may be unreliable.")

    for finding in findings:
        if verify_line_reference(finding, content):
            verified.append(finding)
        else:
            print(f"[Verifier] Rejected '{finding.get('title', '?')}' — "
                  f"line reference not found in {path}")

    removed = len(findings) - len(verified)
    if removed:
        print(f"[Verifier] {removed} finding(s) rejected for {path}.")

    return verified
