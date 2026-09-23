"""
fixer.py — Module 3c
Generates a concrete code fix for a verified finding.

Uses a strict search/replace format instead of a full-file rewrite or a
freeform diff, so the fix can be mechanically validated against the real
file before anything gets committed anywhere:

  - old_code must appear EXACTLY ONCE in the original file, verbatim.
  - If it doesn't (hallucinated snippet, paraphrased instead of copied,
    or ambiguous — matches more than once), the fix is rejected outright.

Same philosophy as verifier.py's line-reference check, just applied to
fixes instead of findings: don't trust it, check it against the actual
file content.
"""

import json

from analyzer import call_llm, sanitize_json_response


FIX_SYSTEM_PROMPT = """You are a senior software engineer writing a minimal, surgical fix for one confirmed bug.

Rules:
- Fix ONLY the exact issue described below. Do not refactor, rename, reformat, or "improve" anything else in the file.
- "old_code" must be an EXACT, VERBATIM substring of the file given to you — copy it character for character, including the original whitespace and indentation. Include a few surrounding lines if needed to make the match unambiguous, but keep it as short as possible.
- "new_code" is the replacement for that exact substring, and nothing more.
- Do not add new dependencies, new imports of packages not already used in the file, network calls, shell/process execution, or file-system side effects that aren't already part of the fix itself.
- If the correct fix requires a SPECIFIC external value you can't verify from the file itself or the issue text — a model identifier, an API version string, a config constant, an exact library option name — do not guess a plausible-looking one. A syntactically valid fix that references a wrong value is worse than no fix: it passes every mechanical check here and still ends up wrong in front of a maintainer. Decline instead (see below) and say what value would be needed.
- Respond ONLY with a valid JSON object. No markdown, no backticks, no explanation outside the JSON.

{
  "old_code": "<verbatim snippet copied from the file>",
  "new_code": "<replacement snippet>",
  "explanation": "<one or two sentences: what this changes and why it fixes the issue>"
}

If you cannot produce a safe, minimal, verbatim-anchored fix — including when it would require guessing an unverifiable specific value — respond with:
{"old_code": null, "new_code": null, "explanation": "<why not>"}
"""


def build_fix_prompt(finding: dict, file_content: str) -> str:
    return f"""File: {finding.get('file_path', '')}
Language: {finding.get('language', '')}

Confirmed issue: {finding.get('title', '')}
Description: {finding.get('description', '')}
Line reference: {finding.get('line_reference', '')}
Suggested direction: {finding.get('fix', '')}

Full file content:
---
{file_content}
---

Produce the minimal search/replace fix as specified in your instructions."""


def generate_fix(finding: dict, file_content: str) -> dict | None:
    """
    Asks the LLM for a minimal search/replace fix, then mechanically
    validates it against the real file content.

    Returns {"old_code", "new_code", "explanation", "patched_content"}
    on success, or None if generation/validation fails for any reason.
    """
    if not file_content:
        print(f"[Fixer] No file content available for {finding.get('file_path')} — skipping fix.")
        return None

    messages = [
        {"role": "system", "content": FIX_SYSTEM_PROMPT},
        {"role": "user", "content": build_fix_prompt(finding, file_content)},
    ]

    try:
        raw  = call_llm(messages)
        data = json.loads(sanitize_json_response(raw))
    except Exception as e:
        print(f"[Fixer] Could not get/parse a fix for '{finding.get('title')}' → {e}")
        return None

    old_code = data.get("old_code")
    new_code = data.get("new_code")

    if not old_code or new_code is None:
        print(f"[Fixer] Model declined to fix '{finding.get('title')}' — "
              f"{data.get('explanation', 'no reason given')}")
        return None

    occurrences = file_content.count(old_code)
    if occurrences != 1:
        print(f"[Fixer] REJECTED fix for '{finding.get('title')}' — old_code matched "
              f"{occurrences} time(s) in the file (need exactly 1). Likely hallucinated or ambiguous.")
        return None

    if old_code == new_code:
        print(f"[Fixer] REJECTED fix for '{finding.get('title')}' — no actual change proposed.")
        return None

    patched_content = file_content.replace(old_code, new_code, 1)

    return {
        "old_code":        old_code,
        "new_code":        new_code,
        "explanation":     data.get("explanation", ""),
        "patched_content": patched_content,
    }
