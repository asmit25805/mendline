"""
github_api.py — shared helper
Small, dependency-free helpers used by more than one module. Deliberately
has no imports from reporter.py or pr_creator.py (or anything that
imports them) so that both of those — which already import from EACH
OTHER in one direction (reporter -> pr_creator) — can import from here
without creating a cycle.
"""

import base64
import requests

from config import GITHUB_HEADERS as HEADERS


def fetch_file(owner: str, repo: str, path: str) -> str | None:
    """Fetches a single file's decoded text content, or None if it doesn't exist / isn't reachable."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return base64.b64decode(resp.json()["content"]).decode("utf-8", errors="replace")
    except Exception:
        pass
    return None
