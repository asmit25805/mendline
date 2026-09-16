# Mendline

*(formerly Code Auditor)*

An automated security and bug-finding pipeline that scans public GitHub repositories, verifies findings through a multi-pass LLM review, and opens issues — with a verified fix and PR alongside them where one can be generated and confirmed safe.

Built solo. Runs on free-tier inference. No funding, no team.

---

## What it does

Mendline scans a codebase, generates candidate findings with an LLM, then runs those findings back through a second and third pass before anything gets posted as a GitHub issue. The goal isn't volume — it's making sure a maintainer can trust the report enough to act on it without doing the verification themselves.

As of this update, it goes a step further: for each finding it also generates a minimal fix, mechanically verifies that fix against the real file (rejecting anything hallucinated or unverifiable), runs it through a syntax/compile check and a safety-pattern scan, and — if all of that passes — opens a PR with the change alongside the issue.

**Pipeline:**

1. **Scan** — pulls a target repo, walks the source tree
2. **First-pass analysis** — LLM flags candidate bugs/vulnerabilities, must quote the exact line of code
3. **Second-pass verification** — an independent LLM pass asks "is this actually real, or a false positive?"
4. **Static verification layer** — code checks that every function/variable/file path referenced in a finding actually exists — hallucinated references get rejected automatically
5. **Confidence gate** — only findings above a 0.92 confidence threshold survive
6. **Context check** — the pipeline reads the project type before flagging anything context-dependent (e.g. "unbounded stdin" is a real issue in a web server, a non-issue in a CLI tool)
7. **Template-aware reporting** — before posting, the bot reads the repo's `CONTRIBUTING.md` (if one exists) and conforms the report to the repo's own bug-report format and conventions, instead of posting a generic template
8. **Post** — opens a GitHub issue with the finding, the exact line reference, and severity
9. **Fix generation** — proposes a minimal search/replace fix for the finding; rejected outright if the snippet it's anchored to doesn't exist verbatim in the file
10. **Fix verification** — safety-pattern scan (won't ship a fix that sneaks in `eval`, `os.system`, shell-outs, etc.), then a real syntax/compile check on the patched file
11. **PR** — fork → branch → commit → PR back to the upstream repo, linked from the issue. A verified fix's diff is also included directly in the issue body

**Two sources of "what to fix," both feeding the same fix/verify/PR pipeline above (steps 9-11):**

- **Self-discovered** (steps 1-8): the LLM finds its own candidate bugs by reading the code.
- **Existing issues** (`issue_resolver.py`): separately, the bot reads the repo's own *open issues*, and — for the ones that look like unclaimed, actionable bug reports (no assignee, no label like `enhancement`/`question`, no PR already referencing them, body long enough to say something real) — uses the issue's own text plus the repo's real file tree to figure out which file it's actually about. That's a deliberately different lookup than the file-scoring heuristic in `code_puller.py`, which is tuned for spotting new security-shaped problems in files it picks itself, not for locating whatever a specific human-filed issue happens to be describing. Once a file is picked, the same anchor-to-real-code check (reject anything not verbatim in the file) confirms the issue is actually there before a fix is even attempted. On success, it comments on the original issue with the PR link instead of opening a new issue — the PR body includes `Closes #N` so merging it auto-closes the issue. On failure at any step, it stays silent on that issue rather than posting a "couldn't fix this" comment.


More verification layers sit between "LLM had a thought" and anything getting posted or committed than before — the fix itself now goes through the same don't-trust-it-check-it treatment as the finding.

---

## Why the template-aware step exists

Early versions of this bot ignored repo conventions entirely and posted a fixed format regardless of what the maintainer expected. At least one maintainer rejected a report specifically because it didn't match their contribution guidelines. The fix: read `CONTRIBUTING.md` before generating the report and adapt to it, rather than assuming a one-size-fits-all format works everywhere.

---

## Two report formats, two very different track records

This project went through a real format shift, and it's worth being honest about both sides of it rather than only showing the good numbers.

**Early / batch format** — one issue containing many findings at once (10-30+ per report). This is where the false-positive problem lived. Maintainers across several repos flagged these as alert fatigue, asked for findings to be split up, or closed them with a one-word "tldr." This format is retired.

**Current / scoped format** — one finding per issue, verified end-to-end through the full pipeline above. Across the most recent batch of scoped reports, this format has produced real, maintainer-confirmed fixes with a single confirmed false positive in the set — roughly a 5% false-positive rate, a large improvement over the batch era.

The lesson: verification layers matter less than report granularity. A confident LLM producing 25 findings in one issue will always read as spam, no matter how good the underlying analysis is. One well-scoped, well-verified finding per issue is what actually gets read and fixed.

---

## Results

Confirmed real bugs fixed by maintainers include (non-exhaustive):

- **deeplethe/forkd** — blocking `accept()` deadlock, merged same day
- **nubjs/nub** — path traversal via unvalidated `bin_subpath`, fixed in v0.2.4; cache key bug, fixed in same release
- **Helvesec/rmux** — deserializer bug, resolved in v0.7.1
- **vercel-labs/zerolang** — NULL pointer dereference, confirmed and fixed
- **vercel/eve** — undefined auth secret + null AbortSignal bug, both fixed
- **BigPizzaV3/CodexPlusPlus** — arbitrary file write via unchecked backup path, fixed and pushed to main
- **nexu-io/html-anything** — zero-duration handling bug and unsanitized HTML injection via `document.write`, both confirmed real

20+ verified bugs fixed across public repos overall, spanning both format eras.

---

## Stack

- **Language:** Python
- **Primary inference:** OpenRouter — `baidu/cobuddy:free` (coding-focused, 131K context; free tier is 50–1,000 requests/day depending on credit history)
- **Fallback inference:** Groq → Gemini
- **Scheduling:** GitHub Actions cron
- **State tracking:** SQLite (tracks what's already been scanned to avoid duplicate reports)
- **Cost:** $0 to run — entirely on free tiers

---

## Honest limitations

- Confidence scoring reduces but does not eliminate false positives
- Context-dependent findings (intentional design choices that look like bugs) are the main remaining failure mode
- The pipeline audits public repos it doesn't own, so it can't push to them directly — it forks, branches, and opens a PR, and a human maintainer still has to review and merge it, same as any outside contributor
- The fix-verification build check confirms the patched file parses/compiles — it does not run the target repo's actual test suite. Doing that for real would mean installing each repo's full dependency tree (arbitrary npm/pip/cargo packages, including postinstall scripts) inside the same job holding this bot's tokens, which is its own risk on top of being impractical across the languages this pipeline supports
- Not every finding gets a fix — if the model can't produce a minimal, verifiable patch, or the patch fails the safety/syntax gate, that finding still gets an issue, just without a PR
- OpenRouter's free tier is request-capped (not token-capped) at 50/day on an unfunded account, 1,000/day after a one-time $10 credit purchase. A busy run across 5 repos can burn through that on its own — when it does, the pipeline just falls through to Groq, then Gemini, same as any other provider failure. Nothing breaks, but expect OpenRouter to carry less of the load than you might assume unless that $10 top-up has been made
- The "already claimed" check for existing issues relies on GitHub's own cross-reference detection (a PR that mentions the issue number). A PR that fixes the issue without ever mentioning it by number wouldn't be caught, so in rare cases this could still open a redundant PR
- Existing-issue resolution only looks at issues on repos this bot hasn't already scanned and marked done — a repo doesn't get revisited later just because someone filed a new issue on it since
- This is a solo research/tooling project, not a commercial product

---

## License

CC BY-NC on the tool itself. See repo for full license text.

---

Built and maintained by [@asmit25805](https://github.com/asmit25805).
