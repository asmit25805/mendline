import os
from dotenv import load_dotenv

load_dotenv()

# ── API Keys ────────────────────────────────────────────────
GITHUB_TOKEN     = os.getenv("GITHUB_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GROQ_API_KEY     = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")

# ── Trending Repo Settings ──────────────────────────────────
TRENDING_DAYS        = 365
TRENDING_MIN_STARS   = 1200
REPOS_PER_RUN        = 5

# ── Code Fetching Settings ──────────────────────────────────
MAX_FILE_SIZE_KB     = 150
MAX_FILES_PER_REPO   = 25  
MAX_CHARS_PER_FILE   = 15000

# ── Supported file extensions → language name ───────────────
SUPPORTED_EXTENSIONS = {
    ".py":   "Python",
    ".js":   "JavaScript",
    ".ts":   "TypeScript",
    ".java": "Java",
    ".go":   "Go",
    ".rs":   "Rust",
    ".c":    "C",
    ".cpp":  "C++",
    ".rb":   "Ruby",
    ".php":  "PHP",
}

# ── AI Settings ─────────────────────────────────────────────
# openrouter/free — OpenRouter's own router that auto-selects a live free
# model on every call (filtering for tool-calling/structured-output support).
# Previously this pinned one specific free model slug (baidu/cobuddy:free),
# which OpenRouter has since removed — every call 404'd. Free model slugs on
# OpenRouter rotate as providers add/drop them; pointing at the router instead
# of a specific slug means a single removed model can't take this whole step
# down again the way it just did.
# Caveat worth knowing: OpenRouter's free tier is capped by REQUEST COUNT,
# not tokens — 50 requests/day on an account with no credit history, 1,000/day
# once you've ever bought $10 of credits (that higher cap sticks permanently
# after the one-time purchase, even while you keep using $0 :free models).
# When that budget runs out mid-run, it falls through to Groq → Gemini like
# any other provider failure (see call_llm in analyzer.py).
OPENROUTER_MODEL = "openrouter/free"
MIN_CONFIDENCE   = 0.92

# ── Auto-fix Settings ────────────────────────────────────────
# When on: for each issue opened, also generate a minimal fix,
# mechanically verify it against the real file + a syntax/safety gate,
# include the patch in the issue, and open a PR (fork -> branch -> PR)
# with the same change. A rejected/failed fix just falls back to an
# issue with no patch — it never blocks the issue itself.
ENABLE_AUTO_FIX  = True

# ── Existing-Issue Resolution Settings ─────────────────────────
# Beyond self-discovered bugs, also look at the repo's OWN open issues
# and try to fix the ones nobody's already working on. Uses the issue
# text itself (not the bug-hunting file-scoring heuristic) to locate
# the relevant file(s), then runs through the same fixer/build_check/
# pr_creator pipeline as any self-found finding.
ENABLE_ISSUE_RESOLUTION = True
MAX_ISSUES_TO_RESOLVE   = 3     # per repo, per run — bounds LLM + API cost
MAX_OPEN_ISSUES_SCANNED = 50    # how many open issues to look through before giving up
SKIP_ISSUE_LABELS = {
    "question", "discussion", "wontfix", "duplicate", "invalid",
    "enhancement", "feature", "feature request", "help wanted",
}

# ── Files/Folders to always skip ────────────────────────────
SKIP_PATHS = [
    # Dependencies / build output
    "node_modules", "vendor", "dist", "build", ".git", "__pycache__",
    # Tests
    "test", "tests", "spec", "specs", "__tests__",
    # Migrations / fixtures
    "migrations", "fixtures", "mock", "mocks",
    # Examples / docs
    "example", "examples", "demo", "demos", "docs", "doc",
    # Benchmarks / scripts
    "benchmark", "benchmarks", "scripts",
    # Config/build file name patterns
    "tsdown.config", "vite.config", "webpack.config", "rollup.config",
    "babel.config", "jest.config", "eslint.config", "prettier.config",
    "tailwind.config", "postcss.config", "next.config", "nuxt.config",
    "svelte.config", "astro.config", "vitest.config", ".vitepress",
]
