"""
code_puller.py — Module 2
Fetches source code files and README from a repo.
For large repos, scores files by importance and picks the best ones.
"""

import base64
import requests
from config import (
    GITHUB_TOKEN, SUPPORTED_EXTENSIONS,
    MAX_FILE_SIZE_KB, MAX_FILES_PER_REPO,
    MAX_CHARS_PER_FILE, SKIP_PATHS
)


HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

HIGH_VALUE_DIRS = {
    "src", "lib", "core", "api", "server", "backend",
    "app", "pkg", "internal", "cmd", "handler", "handlers",
    "service", "services", "controller", "controllers",
    "middleware", "router", "routes", "auth", "db", "database",
}

HIGH_VALUE_NAMES = {
    "auth", "authentication", "authorization", "login", "session",
    "token", "jwt", "oauth", "password", "crypto", "encrypt",
    "db", "database", "query", "store", "storage", "cache",
    "router", "routes", "handler", "server", "api",
    "payment", "billing", "webhook", "middleware", "security",
    "upload", "file", "permission", "rbac", "config",
}

LOW_VALUE_NAMES = {
    "vite.config", "tsdown.config", "webpack.config", "rollup.config",
    "babel.config", "jest.config", "eslint.config", "prettier.config",
    "tailwind.config", "postcss.config", "next.config", "nuxt.config",
    "svelte.config", "astro.config", "vitest.config", "tsconfig",
    "__init__", "index",
}


def score_file(path: str, size_bytes: int) -> int:
    score  = 0
    parts  = path.lower().replace("\\", "/").split("/")
    name_no_ext = parts[-1].rsplit(".", 1)[0] if "." in parts[-1] else parts[-1]

    for part in parts[:-1]:
        if part in HIGH_VALUE_DIRS:
            score += 30
            break

    depth = len(parts) - 1
    if depth == 0:
        score -= 20
    elif depth == 1:
        score += 5

    if name_no_ext in HIGH_VALUE_NAMES:
        score += 40
    if name_no_ext in LOW_VALUE_NAMES:
        score -= 50

    for hvn in HIGH_VALUE_NAMES:
        if hvn in name_no_ext and name_no_ext not in HIGH_VALUE_NAMES:
            score += 15
            break

    size_kb = size_bytes / 1024
    if size_kb > 5:   score += 10
    if size_kb > 15:  score += 10
    if size_kb > 30:  score += 5
    if size_kb > MAX_FILE_SIZE_KB * 0.8:
        score -= 10

    return score


def should_skip_path(path: str) -> bool:
    path_lower = path.lower()
    return any(skip in path_lower for skip in SKIP_PATHS)


def get_file_extension(filename: str) -> str:
    if "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    return ""


def fetch_repo_tree(owner: str, repo: str, branch: str) -> list[dict]:
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}"
    try:
        resp = requests.get(url, headers=HEADERS, params={"recursive": "1"}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("tree", [])
    except requests.RequestException as e:
        print(f"[Puller] ERROR fetching tree → {e}")
        return []


def fetch_readme(owner: str, repo: str) -> str:
    """Fetches the repo README. Returns empty string if not found."""
    for filename in ["README.md", "README.rst", "README.txt", "README"]:
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{filename}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                data    = resp.json()
                encoded = data.get("content", "")
                decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
                # Trim to 4000 chars — enough for classification, not wasteful
                return decoded[:4000]
        except Exception:
            continue
    return ""


def fetch_file_content(owner: str, repo: str, file_path: str) -> str | None:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data    = resp.json()
        size_kb = data.get("size", 0) / 1024

        if size_kb > MAX_FILE_SIZE_KB:
            print(f"[Puller] Skipping {file_path} — too large ({size_kb:.1f} KB)")
            return None

        encoded = data.get("content", "")
        decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")

        if len(decoded) > MAX_CHARS_PER_FILE:
            decoded = decoded[:MAX_CHARS_PER_FILE] + "\n\n# ... [truncated]"

        return decoded

    except Exception as e:
        print(f"[Puller] ERROR fetching {file_path} → {e}")
        return None


def pull_code_files(repo: dict) -> tuple[list[dict], str, list[str]]:
    """
    Main function. Returns (files, readme, file_tree_paths).
    files: list of { path, language, content }
    readme: raw README text for classification
    file_tree_paths: all file paths (for classification context)
    """
    owner  = repo["owner"]
    name   = repo["name"]
    branch = repo["default_branch"]

    print(f"\n[Puller] Fetching file tree for {owner}/{name}...")
    tree = fetch_repo_tree(owner, name, branch)

    if not tree:
        return [], "", []

    # Fetch README for context
    print(f"[Puller] Fetching README...")
    readme = fetch_readme(owner, name)
    if readme:
        print(f"[Puller] README found ({len(readme)} chars)")
    else:
        print(f"[Puller] No README found")

    # All paths for classification context
    all_paths = [item["path"] for item in tree if item.get("type") == "blob"]

    # Filter to supported source files
    candidate_files = []
    for item in tree:
        if item.get("type") != "blob":
            continue

        path = item["path"]
        ext  = get_file_extension(path)

        if ext not in SUPPORTED_EXTENSIONS:
            continue
        if should_skip_path(path):
            continue

        size_bytes = item.get("size", 0)
        candidate_files.append({
            "path":     path,
            "language": SUPPORTED_EXTENSIONS[ext],
            "score":    score_file(path, size_bytes),
            "size":     size_bytes,
        })

    total = len(candidate_files)
    candidate_files.sort(key=lambda f: f["score"], reverse=True)

    if total > MAX_FILES_PER_REPO:
        print(f"[Puller] {total} eligible files — scoring and picking top {MAX_FILES_PER_REPO}.")
    else:
        print(f"[Puller] Found {total} eligible source files.")

    fetched = []
    for file_info in candidate_files[:MAX_FILES_PER_REPO]:
        score_str = f" (score: {file_info['score']})" if total > MAX_FILES_PER_REPO else ""
        print(f"[Puller]   → {file_info['path']}{score_str}")
        content = fetch_file_content(owner, name, file_info["path"])

        if content and len(content.strip()) > 50:
            fetched.append({
                "path":     file_info["path"],
                "language": file_info["language"],
                "content":  content,
            })

    print(f"[Puller] Successfully fetched {len(fetched)} files.")
    return fetched, readme, all_paths
