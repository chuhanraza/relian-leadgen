"""Verifies a commit is genuinely on origin/main by re-fetching it from GitHub's live API —
not local git state. This exists because "fixed locally, never pushed" has actually bitten
this repo: see CONTRIBUTING.md and the 39380df/86db404 gap it documents. `git log` and
`git status` only prove a commit exists in your local clone; they say nothing about whether
it ever reached GitHub. This script is the only thing in the repo that checks the truth
GitHub itself has.

Usage:
  python scripts/verify_pushed.py <sha> [--branch main] [--repo owner/name]

Exit code 0 = the commit is confirmed on that branch on GitHub. Non-zero = it is not (or
the API call itself failed) — printed detail explains which.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import requests


def _default_repo() -> str:
    """Derive owner/repo from `git remote get-url origin` so this works without a flag in
    the common case, e.g. https://github.com/chuhanraza/relian-leadgen.git -> chuhanraza/relian-leadgen.
    """
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
    ).stdout.strip()
    path = url.split("github.com")[-1].lstrip(":/").removesuffix(".git")
    return path


def verify(sha: str, branch: str, repo: str) -> bool:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    commit_resp = requests.get(
        f"https://api.github.com/repos/{repo}/commits/{sha}", headers=headers, timeout=15
    )
    if commit_resp.status_code != 200:
        print(f"[verify_pushed] GitHub does not have commit {sha} in {repo} at all "
              f"(HTTP {commit_resp.status_code}): {commit_resp.text[:300]}")
        return False
    full_sha = commit_resp.json()["sha"]

    compare_resp = requests.get(
        f"https://api.github.com/repos/{repo}/compare/{branch}...{full_sha}",
        headers=headers,
        timeout=15,
    )
    if compare_resp.status_code != 200:
        print(f"[verify_pushed] compare API call failed (HTTP {compare_resp.status_code}): "
              f"{compare_resp.text[:300]}")
        return False

    status = compare_resp.json()["status"]
    # "identical" = sha IS branch's tip; "behind" = sha is an ancestor of branch (on its
    # history). "ahead"/"diverged" mean the commit exists on GitHub but NOT on this branch —
    # e.g. it's sitting on an unmerged feature branch, which is exactly the failure mode
    # this script exists to catch.
    on_branch = status in ("identical", "behind")
    if on_branch:
        print(f"[verify_pushed] CONFIRMED: {full_sha} is on origin/{branch} (status={status}).")
    else:
        print(f"[verify_pushed] NOT ON {branch}: {full_sha} exists on GitHub but "
              f"compare status is {status!r} — it has not reached origin/{branch}.")
    return on_branch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha", help="Commit SHA to verify")
    parser.add_argument("--branch", default="main", help="Branch to check against (default: main)")
    parser.add_argument("--repo", default=None, help="owner/repo (default: derived from git remote origin)")
    args = parser.parse_args()

    repo = args.repo or _default_repo()
    ok = verify(args.sha, args.branch, repo)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
