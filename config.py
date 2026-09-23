import os
from dotenv import load_dotenv

load_dotenv()

# ── API Keys ────────────────────────────────────────────────
GITHUB_TOKEN     = os.getenv("GITHUB_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GROQ_API_KEY     = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")

# Shared headers for every direct (non-PyGithub) call to the GitHub REST
# API. Used to live copy-pasted identically in six different files
# (code_puller, fetcher, issue_resolver, issue_search, pr_creator,
# reporter) — one definition here instead, so a future API-version bump
# is a one-line change instead of a six-file grep-and-replace.
GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

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
# baidu/cobuddy:free — OpenRouter's free model explicitly built/branded for
# coding tasks (131K context, tool calling + reasoning support).
# Caveat worth knowing: OpenRouter's free tier is capped by REQUEST COUNT,
# not tokens — 50 requests/day on an account with no credit history, 1,000/day
# once you've ever bought $10 of credits (that higher cap sticks permanently
# after the one-time purchase, even while you keep using $0 :free models).
# This pipeline can fire 100+ LLM calls in a single run across 5 repos, so
# expect OpenRouter's daily budget to run out mid-run on an unfunded account —
# that's fine, it just falls through to Groq → Gemini like any other provider
# failure (see call_llm in analyzer.py). If you want OpenRouter to carry most
# of the load reliably, the $10 one-time top-up is the practical fix.
OPENROUTER_MODEL = "baidu/cobuddy:free"
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

# ── Issue Search Settings ───────────────────────────────────
# Beyond checking the issue tracker of whichever repos the trending
# fetcher happened to pick, search GitHub's issue index directly for
# unclaimed, bug-shaped issues wherever they are. A repo being
# "trending" says nothing about whether it has anything fixable in its
# OWN tracker this run — in practice a lot of them turn up nothing.
# Searching for the issues directly, instead of hoping the repo we
# already picked has some, is the fix for that.
#
# IMPORTANT GOTCHA: `stars:` is NOT a working qualifier on GitHub's
# issue-search endpoint (confirmed, not assumed) — it silently
# degrades into a literal text token instead of an actual filter, so a
# query using it looks fine but the filter never happens. Star
# filtering for search-found repos is done as a separate, ordinary
# (non-search) API call per candidate in issue_search.py — don't try
# to "simplify" that back into the query string.
ENABLE_ISSUE_SEARCH     = True
ISSUE_SEARCH_RESULTS    = 30    # raw search results fetched per run, before filtering
MAX_SEARCH_REPOS_PER_RUN = 3    # on top of the REPOS_PER_RUN already scanned via the trending path

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
