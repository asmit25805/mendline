"""
analyzer.py — Module 3
Sends code files to an LLM for analysis.
Falls back automatically if each provider fails or hits a rate limit.

Provider roles:
  OpenRouter (baidu/cobuddy:free) → primary, all per-file analysis + README classification
  Groq                            → fallback for file analysis
  Gemini                          → last resort only

Note: OpenRouter's free tier is request-capped (50-1,000/day depending on
credit history), not token-capped — much tighter than Cerebras's old 1M
tokens/day. A busy run can burn through that budget on its own, which is
exactly what the fallback chain below is for.
"""

import json
import ast
import re
import requests
from groq import Groq
from google import genai

from config import (
    OPENROUTER_API_KEY, GROQ_API_KEY, GEMINI_API_KEY,
    OPENROUTER_MODEL, MIN_CONFIDENCE
)
from verifier import verify_findings, deduplicate_findings


groq_client     = Groq(api_key=GROQ_API_KEY)
gemini_client   = genai.Client(api_key=GEMINI_API_KEY)


# ── Project type → what NOT to flag ─────────────────────────
PROJECT_TYPE_RULES = {
    "cli":        "NEVER flag stdin handling, sync fs calls, event loop blocking, or local env variables. These are not issues in CLI tools.",
    "library":    "NEVER flag missing input validation at the top level — library callers are responsible for that.",
    "web_server": "Flag all input validation, authentication, SQL injection, XSS, CSRF, and insecure defaults. This code handles untrusted input.",
    "unknown":    "Be conservative. Only flag issues that would cause an obvious, observable failure.",
}


SYSTEM_PROMPT_TEMPLATE = """You are a senior software engineer doing a careful code review.
Your job is to find real, reproducible bugs and security issues only.

Project type: {project_type}
Project description: {project_description}
Special rules for this project: {project_rules}

You must respond ONLY with a valid JSON object — no explanation, no markdown, no backticks.

{{
  "findings": [
    {{
      "type": "bug" | "security",
      "severity": "critical" | "high" | "medium" | "low",
      "confidence": <float between 0.0 and 1.0>,
      "line_reference": "<exact function name or variable name that exists in the code>",
      "title": "<short one-line title>",
      "description": "<what is wrong — must include the exact problematic line quoted in backticks>",
      "fix": "<concrete fix with example code>"
    }}
  ]
}}

STRICT RULES:
- Only report "bug" or "security" findings. Never performance or suggestions.
- Only report confidence >= 0.92. If unsure, return {{ "findings": [] }}.
- line_reference MUST be an exact function or variable name that exists in the code.
- description MUST quote the exact problematic line using backticks. No quote = rejected.
- NEVER report a finding based on truncated code near a cut-off point.
- Before reporting: does this cause a real, observable failure in normal use? If not, skip it.
- Max 2 findings per file. If clean or unsure, return {{ "findings": [] }}

SEVERITY RULES — be conservative, never sensationalist:
- Default to "low" unless there is a clear, direct path to exploitation by an external attacker.
- "medium" requires user-controlled input reaching the vulnerable code with a realistic attack path.
- "high" requires no authentication and direct, meaningful impact on data or availability.
- "critical" is reserved only for RCE, authentication bypass, or mass data exfiltration.
- NEVER rate "high" or "critical" if the attacker must already have local access, control over config files, or be a trusted operator.
- Defense-in-depth hardenings (validating already-trusted input, adding extra checks) are always "low"."""


SECOND_PASS_PROMPT = """You are a strict code reviewer verifying potential bugs.
For each finding, decide: REAL bug or FALSE POSITIVE?

A finding is REAL only if:
- The exact code quoted in backticks in the description actually exists in the file
- The bug causes an observable failure in normal use
- It is not a style issue, best practice, or theoretical concern

Respond ONLY with valid JSON:
{{ "confirmed": [<list of finding titles that are REAL bugs>] }}

If none are real, return {{ "confirmed": [] }}"""


README_CLASSIFICATION_PROMPT = """You are analyzing a GitHub repository to understand what kind of project it is.

Read the README and file tree below, then respond ONLY with valid JSON — no markdown, no backticks:

{{
  "project_type": "cli" | "web_server" | "library" | "unknown",
  "project_description": "<one sentence describing what this project does>",
  "handles_untrusted_input": true | false,
  "is_security_critical": true | false
}}

README:
{readme}

File tree (first 50 files):
{file_tree}"""


# ────────────────────────────────────────────────────────────
# Provider calls
# ────────────────────────────────────────────────────────────

def sanitize_json_response(raw: str) -> str:
    """
    Strips markdown fences and removes control characters
    that cause json.loads() to fail.
    """
    # Strip markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    # Remove control characters except newline/tab which are valid in JSON strings
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', raw)

    return raw


def call_openrouter(messages: list[dict]) -> str:
    response = requests.post(
        url="https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://github.com/mendline",  # optional, for OpenRouter's rankings
            "X-OpenRouter-Title": "Mendline",                  # optional, ditto
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": messages,
            "max_tokens": 3000,
            "temperature": 0.1,
        },
        timeout=60,
    )
    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter returned {response.status_code}: {response.text[:300]}")

    data = response.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content")
    if not content:
        raise RuntimeError("OpenRouter returned empty response")
    return content.strip()


def call_groq(messages: list[dict]) -> str:
    # llama-3.3-70b-versatile was deprecated by Groq on 2026-08-16.
    # openai/gpt-oss-120b is Groq's official recommended replacement
    # (available on free/developer tier, same as the model it replaces).
    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=messages,
        max_tokens=3000,
        temperature=0.1,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("Groq returned empty response")
    return content.strip()


def call_gemini(messages: list[dict]) -> str:
    """Gemini as last resort — combines system + user into one prompt."""
    system_text = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_text   = next((m["content"] for m in messages if m["role"] == "user"), "")
    full_prompt = f"{system_text}\n\n{user_text}"
    # gemini-2.0-flash was retired by Google; gemini-3.6-flash is the
    # current flash-tier model.
    response    = gemini_client.models.generate_content(
        model="gemini-3.6-flash",
        contents=full_prompt,
    )
    if not response.text:
        raise RuntimeError("Gemini returned empty response")
    return response.text.strip()


def call_llm(messages: list[dict]) -> str:
    """OpenRouter → Groq → Gemini."""
    try:
        result = call_openrouter(messages)
        print("[Analyzer] Provider: OpenRouter ✅")
        return result
    except Exception as e:
        print(f"[Analyzer] OpenRouter failed → {e}")

    try:
        result = call_groq(messages)
        print("[Analyzer] Provider: Groq ✅ (fallback)")
        return result
    except Exception as e:
        print(f"[Analyzer] Groq failed → {e}")

    try:
        result = call_gemini(messages)
        print("[Analyzer] Provider: Gemini ✅ (last resort)")
        return result
    except Exception as e:
        print(f"[Analyzer] Gemini also failed → {e}")

    raise RuntimeError("All three providers failed.")


# ────────────────────────────────────────────────────────────
# README classification — goes through the full fallback chain
# Unlike the old Cerebras setup (1M tokens/day, so classification could
# safely hit it directly without touching the scarce Gemini fallback),
# OpenRouter's free tier is now the TIGHT-quota provider (request-capped,
# not token-capped). So classification uses call_llm() and gets the same
# OpenRouter → Groq → Gemini fallback as everything else, instead of
# calling one provider directly and giving up to defaults on failure.
# ────────────────────────────────────────────────────────────

def classify_repo(readme: str, file_tree: list[str]) -> dict:
    """
    Classifies repo type using an LLM + README + file tree.
    Returns project_type, description, etc.
    Falls back to safe defaults if classification fails.
    """
    default = {
        "project_type":          "unknown",
        "project_description":   "Unknown project",
        "handles_untrusted_input": False,
        "is_security_critical":  False,
    }

    if not readme and not file_tree:
        return default

    tree_str       = "\n".join(file_tree[:50])
    readme_trimmed = readme[:3000] if readme else "No README found."

    prompt = README_CLASSIFICATION_PROMPT.format(
        readme=readme_trimmed,
        file_tree=tree_str,
    )

    messages = [
        {"role": "system", "content": "You analyze GitHub repos and return JSON only. No markdown, no backticks."},
        {"role": "user",   "content": prompt},
    ]

    try:
        raw    = call_llm(messages)
        raw    = sanitize_json_response(raw)
        result = json.loads(raw)
        print(f"[Analyzer] Repo classified as: {result.get('project_type', 'unknown')} — {result.get('project_description', '')}")
        return result
    except Exception as e:
        print(f"[Analyzer] README classification failed → {e} — using defaults")
        return default


# ────────────────────────────────────────────────────────────
# Static analysis pre-filter
# ────────────────────────────────────────────────────────────

SECURITY_CRITICAL_NAMES = {
    "auth", "authentication", "authorization", "login", "session",
    "token", "jwt", "oauth", "password", "crypto", "encrypt",
    "db", "database", "query", "store", "sql", "payment",
    "webhook", "middleware", "security", "permission", "rbac",
    "upload", "sanitize", "validate",
}


def is_security_critical_file(path: str) -> bool:
    name = path.lower().split("/")[-1].rsplit(".", 1)[0]
    return any(kw in name for kw in SECURITY_CRITICAL_NAMES)


def static_analysis_python(content: str) -> list[str]:
    issues = []
    try:
        ast.parse(content)
    except SyntaxError as e:
        # If the file was truncated, the syntax error is likely caused by
        # the truncation itself, not real broken code — skip to avoid hallucinations
        if "[truncated]" in content:
            return []
        issues.append(f"Syntax error: {e}")
        return issues

    dangerous_patterns = [
        (r'eval\s*\(',                          "eval() usage"),
        (r'exec\s*\(',                          "exec() usage"),
        (r'pickle\.loads?\s*\(',                "pickle.load() — unsafe deserialization"),
        (r'subprocess.*shell\s*=\s*True',       "subprocess with shell=True"),
        (r'f["\'].*SELECT.*WHERE.*\{',          "potential SQL injection via f-string"),
        (r'hashlib\.md5\s*\(',                  "MD5 usage — weak hashing"),
        (r'hashlib\.sha1\s*\(',                 "SHA1 usage — weak hashing"),
        (r'random\.(random|randint|choice)\s*\(', "non-cryptographic random"),
    ]
    for pattern, description in dangerous_patterns:
        if re.search(pattern, content, re.IGNORECASE):
            issues.append(description)
    return issues


def static_analysis_js(content: str) -> list[str]:
    issues = []
    dangerous_patterns = [
        (r'eval\s*\(',                    "eval() usage"),
        (r'innerHTML\s*=',                "innerHTML assignment — potential XSS"),
        (r'dangerouslySetInnerHTML',      "dangerouslySetInnerHTML — potential XSS"),
        (r'document\.write\s*\(',        "document.write() — potential XSS"),
        (r'\.exec\s*\(.*req\.|\.exec\s*\(.*input', "potential SQL injection"),
        (r'child_process.*exec\s*\(',    "child_process.exec() — potential command injection"),
        (r'Math\.random\s*\(\)',         "Math.random() — non-cryptographic"),
        (r'new\s+Function\s*\(',         "new Function() — potential code injection"),
    ]
    for pattern, description in dangerous_patterns:
        if re.search(pattern, content, re.IGNORECASE):
            issues.append(description)
    return issues


def run_static_analysis(file: dict) -> list[str]:
    lang    = file["language"]
    content = file["content"]
    if lang == "Python":
        return static_analysis_python(content)
    elif lang in ("JavaScript", "TypeScript"):
        return static_analysis_js(content)
    else:
        return []


def should_analyze_with_llm(file: dict, static_issues: list[str]) -> bool:
    if static_issues:
        return True
    if is_security_critical_file(file["path"]):
        return True
    # C, C++, Rust — memory bugs, buffer overflows, integer overflows
    # don't show up in pattern matching so always send to LLM
    if file["language"] in ("C", "C++", "Rust"):
        return True
    return False


# ────────────────────────────────────────────────────────────
# Prompt building
# ────────────────────────────────────────────────────────────

def build_system_prompt(repo_context: dict) -> str:
    project_type = repo_context.get("project_type", "unknown")
    description  = repo_context.get("project_description", "Unknown project")
    rules        = PROJECT_TYPE_RULES.get(project_type, PROJECT_TYPE_RULES["unknown"])
    return SYSTEM_PROMPT_TEMPLATE.format(
        project_type=project_type,
        project_description=description,
        project_rules=rules,
    )


def build_user_prompt(file_path: str, language: str, content: str, static_issues: list[str]) -> str:
    static_hint = ""
    if static_issues:
        static_hint = (
            "\nStatic analysis flagged these patterns in this file:\n" +
            "\n".join(f"  - {i}" for i in static_issues) +
            "\nFocus your review on these areas, but only report if you confirm a real bug.\n"
        )
    return f"""Analyze this {language} file for bugs and security vulnerabilities only.
Quote the exact problematic line in your description using backticks.
{static_hint}
File: {file_path}

```{language.lower()}
{content}
```"""


def build_verification_prompt(file_path: str, language: str, content: str, findings: list[dict]) -> str:
    findings_text = "\n\n".join([
        f"Finding {i+1}: {f['title']}\n"
        f"Line reference: {f.get('line_reference', 'N/A')}\n"
        f"Description: {f.get('description', 'N/A')}"
        for i, f in enumerate(findings)
    ])
    return f"""Verify each finding — is it a real bug or a false positive?

File: {file_path}

```{language.lower()}
{content}
```

Findings to verify:
{findings_text}"""


# ────────────────────────────────────────────────────────────
# Core analysis
# ────────────────────────────────────────────────────────────

def parse_findings(raw_text: str) -> list[dict]:
    raw_text = sanitize_json_response(raw_text)
    return json.loads(raw_text).get("findings", [])


def second_pass_verify(file: dict, findings: list[dict]) -> list[dict]:
    if not findings:
        return []

    messages = [
        {"role": "system", "content": SECOND_PASS_PROMPT},
        {"role": "user",   "content": build_verification_prompt(
            file["path"], file["language"], file["content"], findings
        )},
    ]

    try:
        raw = call_llm(messages)
        raw = sanitize_json_response(raw)

        confirmed = set(t.lower().strip() for t in json.loads(raw).get("confirmed", []))
        if not confirmed:
            print(f"[Analyzer] Second pass: all findings rejected for {file['path']}")
            return []

        kept     = [f for f in findings if f.get("title", "").lower().strip() in confirmed]
        rejected = len(findings) - len(kept)
        if rejected:
            print(f"[Analyzer] Second pass: {rejected} finding(s) rejected as false positives.")
        return kept

    except Exception as e:
        print(f"[Analyzer] Second pass failed → {e} — keeping originals")
        return findings


def analyze_file(file: dict, repo_context: dict) -> list[dict]:
    """
    Full pipeline for a single file:
    1. Static analysis
    2. Decide whether to send to LLM
    3. LLM pass 1 — find bugs
    4. LLM pass 2 — verify findings
    5. Line reference verification
    """
    path     = file["path"]
    language = file["language"]

    # Step 1 — static analysis
    static_issues = run_static_analysis(file)

    # Step 2 — skip if clean and not security-critical
    if not should_analyze_with_llm(file, static_issues):
        print(f"[Analyzer] Skipping {path} — clean static analysis, not security-critical.")
        return []

    print(f"[Analyzer] Analyzing {path} [{language}]...")
    if static_issues:
        print(f"[Analyzer]   Static flags: {', '.join(static_issues)}")

    system_prompt = build_system_prompt(repo_context)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": build_user_prompt(path, language, file["content"], static_issues)},
    ]

    try:
        # Pass 1 — find bugs
        raw      = call_llm(messages)
        findings = parse_findings(raw)
        findings = [f for f in findings if f.get("confidence", 0) >= MIN_CONFIDENCE]

        if not findings:
            print(f"[Analyzer]   → 0 finding(s) above confidence threshold.")
            return []

        for f in findings:
            f["file_path"] = path
            f["language"]  = language

        # Pass 2 — LLM self-verification
        print(f"[Analyzer]   → {len(findings)} finding(s) — running second pass...")
        findings = second_pass_verify(file, findings)

        # Pass 3 — line reference verification
        findings = verify_findings(findings, file)

        print(f"[Analyzer]   → {len(findings)} finding(s) confirmed.")
        return findings

    except json.JSONDecodeError as e:
        print(f"[Analyzer] JSON parse error for {path} → {e}")
        return []
    except RuntimeError:
        print(f"[Analyzer] All providers failed for {path} — skipping.")
        return []
    except Exception as e:
        print(f"[Analyzer] ERROR analyzing {path} → {e}")
        return []


def analyze_repo(files: list[dict], repo_context: dict) -> list[dict]:
    """
    Analyzes all files in a repo using repo context for smarter prompts.
    Returns deduplicated, verified findings sorted by severity.
    """
    all_findings = []

    for file in files:
        findings = analyze_file(file, repo_context)
        all_findings.extend(findings)

    all_findings = deduplicate_findings(all_findings)

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_findings.sort(key=lambda f: severity_order.get(f.get("severity", "low"), 3))

    print(f"\n[Analyzer] Total verified findings: {len(all_findings)}")
    return all_findings
