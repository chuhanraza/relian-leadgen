# Contributing

## The one hard rule

**Never report a fix as complete without fetching the changed file(s) from GitHub's API
against `origin/main` after pushing.** Local git state (`git log`, `git status`, `git push`
exiting 0) is not sufficient proof that a fix actually reached GitHub.

### Why this rule exists

Two real incidents in this repo's history show the gap between "looks pushed locally" and
"actually landed on GitHub":

- `39380df` (fix: drop broken banner img) and `86db404` (fix: migrate to gpt-oss models)
  were both worked on in sessions where the fix was verified against the local working
  tree and reported done — but local verification cannot distinguish a commit that's
  sitting on an unpushed branch, an unmerged PR, or a push that silently failed, from one
  that's genuinely live on `origin/main`. The only way to know is to ask GitHub.

A fix that never reached `origin/main` is a fix that never happened, from the point of
view of the next scheduled pipeline run (GitHub Actions checks out `origin/main`, not your
laptop). This is the same underlying failure pattern as the model-deprecation and
silent-breakage issues this repo has hit repeatedly: something looks fine locally while
production quietly does not have it.

### How to satisfy the rule

Run `scripts/verify_pushed.py` with the commit SHA after pushing:

```bash
python scripts/verify_pushed.py <sha>
```

It re-fetches the commit from GitHub's live REST API (not local git) and confirms it is
genuinely reachable from `origin/main`, printing an unambiguous `CONFIRMED` or `NOT ON
main` line and exiting non-zero on anything but confirmation. End every fix session with
this call and paste its output — not a description of what you believe you did.
