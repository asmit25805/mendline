"""
build_check.py — Module 3d
Gate that runs before any generated fix gets committed to a real repo.

Two layers:
  1. Syntax/compile check — real, using the language's own compiler or
     parser when it's available on PATH (GitHub Actions' ubuntu-latest
     ships most of these). Falls back to a conservative brace/paren
     balance heuristic if the toolchain isn't installed, rather than
     silently skipping the check.
  2. Safety-pattern scan — rejects a fix that introduces something like
     eval/exec/os.system/shell-outs/raw sockets that wasn't already
     present in the original file. A hallucinated "fix" is one thing;
     a hallucinated fix that also adds a shell-out is worse, and this
     is code headed for someone else's repo.

Honest scope note: this checks "does it parse/compile," not "do the
project's tests pass." Actually running each target repo's test suite
would mean installing that repo's full dependency tree — arbitrary
npm/pip/cargo/go packages, including postinstall scripts — inside the
same job that holds this bot's GitHub/API tokens. That's a supply-chain
risk in its own right, on top of being impractical across ten
languages and repos this bot doesn't control. So the gate below is a
real compile/parse check, not a test run — worth knowing so the PR
descriptions don't overclaim what was verified.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

EXT_BY_LANGUAGE = {
    "Python": ".py", "JavaScript": ".js", "TypeScript": ".ts",
    "Java": ".java", "Go": ".go", "Rust": ".rs", "C": ".c",
    "C++": ".cpp", "Ruby": ".rb", "PHP": ".php",
}

# binary -> args template; {file} is substituted with a temp file path.
# Java and Rust need full module/crate context to compile meaningfully,
# so a single file can't be genuinely compiled in isolation — they fall
# through to the heuristic check below instead of a fake pass/fail.
COMPILERS = {
    "Python":     ("python3", ["-m", "py_compile", "{file}"]),
    "JavaScript": ("node",    ["--check", "{file}"]),
    "TypeScript": ("node",    ["--check", "{file}"]),  # loose check only — full type-check needs project context
    "Ruby":       ("ruby",    ["-c", "{file}"]),
    "PHP":        ("php",     ["-l", "{file}"]),
    "Go":         ("gofmt",   ["-l", "{file}"]),        # parses + formats; won't catch missing-package errors
    "C":          ("gcc",     ["-fsyntax-only", "{file}"]),
    "C++":        ("g++",     ["-fsyntax-only", "{file}"]),
}

SUSPICIOUS_PATTERNS = [
    r"\beval\s*\(", r"\bexec\s*\(", r"os\.system\s*\(",
    r"subprocess\.(Popen|call|run)\s*\(", r"child_process",
    r"curl\s+.*\|\s*sh", r"wget\s+.*\|\s*sh",
    r"socket\.socket\s*\(", r"new\s+ActiveXObject",
    r"\.exec\s*\(.*(sh|bash|cmd)\b",
]


def _balanced_braces(content: str) -> bool:
    """Fallback heuristic when no compiler is available: brackets/braces/parens balance."""
    pairs   = {")": "(", "]": "[", "}": "{"}
    stack   = []
    in_str  = None
    escape  = False
    for ch in content:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            continue
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                return False
            stack.pop()
    return not stack


def check_syntax(language: str, content: str) -> tuple[bool, str]:
    """Returns (ok, note)."""
    if language not in COMPILERS:
        ok = _balanced_braces(content)
        return ok, "heuristic brace-balance check only — no single-file compiler configured for this language"

    binary, arg_template = COMPILERS[language]
    if not shutil.which(binary):
        ok = _balanced_braces(content)
        return ok, f"heuristic check only — '{binary}' not available in this environment"

    ext = EXT_BY_LANGUAGE.get(language, ".txt")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=ext, delete=False) as tf:
            tf.write(content)
            tmp_path = tf.name

        args   = [binary] + [a.format(file=tmp_path) for a in arg_template]
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)

        if result.returncode == 0:
            return True, f"{binary} syntax check passed"
        return False, f"{binary} rejected the fix: {(result.stderr or result.stdout).strip()[:400]}"
    except subprocess.TimeoutExpired:
        return False, f"{binary} timed out"
    except Exception as e:
        return False, f"syntax check error: {e}"
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def check_safety_patterns(original_content: str, patched_content: str) -> tuple[bool, str]:
    """Rejects a fix that introduces a suspicious pattern not already present in the original."""
    for pattern in SUSPICIOUS_PATTERNS:
        already_there = re.search(pattern, original_content)
        now_there      = re.search(pattern, patched_content)
        if now_there and not already_there:
            return False, f"fix introduces a new suspicious pattern ({pattern}) not present in the original file"
    return True, "no new suspicious patterns introduced"


def verify_fix(language: str, original_content: str, patched_content: str) -> tuple[bool, str]:
    """Full gate: safety scan first, then syntax check. Returns (ok, note-for-logging)."""
    safe_ok, safe_note = check_safety_patterns(original_content, patched_content)
    if not safe_ok:
        return False, safe_note

    syntax_ok, syntax_note = check_syntax(language, patched_content)
    if not syntax_ok:
        return False, syntax_note

    return True, syntax_note
