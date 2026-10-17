#!/usr/bin/env python3
"""Watch the Azure Pipelines build for the pushed commit and report why it failed.

Usage (from the repository root, after `git push`):
    python .github/skills/ado-build-monitor/watch_build.py
    python .github/skills/ado-build-monitor/watch_build.py --queue
    python .github/skills/ado-build-monitor/watch_build.py --build-id 4567   # resume watching

By default it finds the build for the current HEAD commit on the current branch,
polls until the build finishes, and prints a short report. On failure the report
lists each failed task with its error messages and the end of its log, so the
agent can fix the code without reading the whole build log.

Configuration comes from environment variables, or from a .env file at the
repository root (never committed):
    AZURE_DEVOPS_ORG_URL       e.g. https://dev.azure.com/contoso
    AZURE_DEVOPS_PROJECT       e.g. Storefront
    AZURE_DEVOPS_PAT           PAT with Build (Read), or Build (Read & execute) for --queue
    AZURE_DEVOPS_PIPELINE_ID   optional pipeline (definition) ID; required for --queue

Exit codes:
    0 build succeeded
    1 build failed, partially succeeded or was canceled (see the RESULT line)
    2 configuration or usage error (for example the commit is not pushed)
    3 Azure DevOps API error
    4 no build found for the commit
    5 timed out while the build was still running (rerun with --build-id)

Uses only the Python standard library (3.9+), so there is nothing to install.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_VERSION = "7.1"
TIMESTAMP_PREFIX = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z ")
STATUS_MARKERS = {
    "succeeded": "[ok]  ",
    "succeededWithIssues": "[warn]",
    "failed": "[FAIL]",
    "canceled": "[cncl]",
    "skipped": "[skip]",
    "abandoned": "[abnd]",
}


# --- Azure DevOps client (kept in sync with ado-work-item/get_work_item.py) ------

class AdoError(Exception):
    def __init__(self, message: str, exit_code: int = 3):
        super().__init__(message)
        self.exit_code = exit_code


def load_dotenv() -> None:
    """Load KEY=VALUE pairs from .env at the repo root without overriding the environment."""
    candidates = []
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
        candidates.append(Path(root) / ".env")
    except (OSError, subprocess.CalledProcessError):
        pass
    candidates.append(Path.cwd() / ".env")
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))
        return


def load_config() -> tuple[str, str, str]:
    load_dotenv()
    required = ("AZURE_DEVOPS_ORG_URL", "AZURE_DEVOPS_PROJECT", "AZURE_DEVOPS_PAT")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise AdoError(
            "Missing configuration: " + ", ".join(missing)
            + ". Set them in the environment or in .env at the repo root (see .env.example).",
            2,
        )
    return (
        os.environ["AZURE_DEVOPS_ORG_URL"].rstrip("/"),
        os.environ["AZURE_DEVOPS_PROJECT"],
        os.environ["AZURE_DEVOPS_PAT"],
    )


class AdoClient:
    def __init__(self, org_url: str, project: str, pat: str):
        self.org_url = org_url
        self.project = project
        self.base = f"{org_url}/{urllib.parse.quote(project)}/_apis"
        token = base64.b64encode(f":{pat}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def request(self, path, params=None, method="GET", body=None, accept_text=False, api_version=API_VERSION):
        query = dict(params or {})
        query.setdefault("api-version", api_version)
        url = f"{self.base}/{path}?{urllib.parse.urlencode(query)}"
        headers = dict(self._headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if accept_text:
            headers["Accept"] = "text/plain"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
                # A bad PAT gets a 203 with an HTML sign-in page instead of a 401.
                if resp.status == 203 or "text/html" in resp.headers.get("Content-Type", ""):
                    raise AdoError(
                        "Azure DevOps rejected the credentials (returned a sign-in page). "
                        "Check AZURE_DEVOPS_PAT and AZURE_DEVOPS_ORG_URL."
                    )
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise AdoError(
                    f"Azure DevOps returned {err.code}: the PAT is invalid, expired, "
                    "or missing a required scope."
                ) from None
            if err.code == 404:
                raise AdoError(f"Not found: {path}", 4) from None
            detail = err.read().decode("utf-8", errors="replace")[:500]
            raise AdoError(f"Azure DevOps returned {err.code} for {path}: {detail}") from None
        except urllib.error.URLError as err:
            raise AdoError(f"Could not reach Azure DevOps at {self.org_url}: {err.reason}") from None
        return payload if accept_text else json.loads(payload)


# --- Helpers ----------------------------------------------------------------------

def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as err:
        stderr = getattr(err, "stderr", "") or str(err)
        raise AdoError(f"git {' '.join(args)} failed: {stderr.strip()}", 2) from None


def log(start: float, message: str) -> None:
    elapsed = int(time.monotonic() - start)
    print(f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {message}", flush=True)


def build_label(build: dict) -> str:
    name = build.get("definition", {}).get("name", "pipeline")
    return f"build {build['id']} ({name} #{build.get('buildNumber', '?')})"


def web_url(build: dict) -> str:
    return build.get("_links", {}).get("web", {}).get("href", "")


def resolve_commit_and_branch(args) -> tuple[str, str]:
    commit = git("rev-parse", args.commit or "HEAD")
    branch = args.branch or git("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise AdoError("HEAD is detached. Check out the feature branch or pass --branch.", 2)
    if not args.commit:
        try:
            upstream = subprocess.run(
                ["git", "rev-parse", "@{u}"], capture_output=True, text=True, check=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            raise AdoError(
                f"Branch {branch} has no upstream, so it has not been pushed. "
                "Run: git push -u origin HEAD", 2
            ) from None
        if upstream != commit:
            raise AdoError(
                f"HEAD {commit[:8]} is not what the remote has ({upstream[:8]}). "
                "Push first: git push", 2
            )
    return commit, branch


def find_build(client, start, commit, branch, definition_id, wait_seconds, interval):
    params = {
        "branchName": f"refs/heads/{branch}",
        "queryOrder": "queueTimeDescending",
        "$top": 25,
    }
    if definition_id:
        params["definitions"] = definition_id
    deadline = time.monotonic() + wait_seconds
    announced = False
    while True:
        builds = client.request("build/builds", params).get("value", [])
        matches = [b for b in builds if (b.get("sourceVersion") or "").lower() == commit.lower()]
        if matches:
            if len(matches) > 1:
                others = ", ".join(build_label(b) for b in matches[1:])
                log(start, f"Several builds ran for this commit; watching the newest. Others: {others}. "
                           "Set AZURE_DEVOPS_PIPELINE_ID to pick one pipeline.")
            return matches[0]
        if time.monotonic() >= deadline:
            return None
        if not announced:
            log(start, f"Waiting for a build of {commit[:8]} on {branch} to be queued...")
            announced = True
        time.sleep(min(interval, 10))


def queue_build(client, commit, branch, definition_id):
    body = {
        "definition": {"id": int(definition_id)},
        "sourceBranch": f"refs/heads/{branch}",
        "sourceVersion": commit,
    }
    return client.request("build/builds", method="POST", body=body)


def record_path(record: dict, records_by_id: dict) -> str:
    names = []
    current = record
    while current:
        if current.get("type") in ("Stage", "Job", "Task"):
            names.append(current.get("name", "?"))
        current = records_by_id.get(current.get("parentId"))
    return " > ".join(reversed(names))


def get_timeline(client, build_id):
    try:
        return client.request(f"build/builds/{build_id}/timeline").get("records", []) or []
    except AdoError as err:
        if err.exit_code == 4:
            return []  # The timeline does not exist until the build starts.
        raise


def log_tail(client, build_id, log_id, line_counts, lines):
    total = line_counts.get(log_id)
    params = {}
    if total and total > lines:
        params = {"startLine": total - lines + 1, "endLine": total}
    text = client.request(f"build/builds/{build_id}/logs/{log_id}", params, accept_text=True)
    if text.lstrip().startswith("{"):
        try:
            text = "\n".join(json.loads(text).get("value", []))
        except ValueError:
            pass
    tail = [TIMESTAMP_PREFIX.sub("", line) for line in text.splitlines()]
    return "\n".join(tail[-lines:])


def failure_report(client, build, records, log_lines, max_failed) -> str:
    out = []
    for result in build.get("validationResults") or []:
        if result.get("result") == "error":
            out.append(f"- Pipeline validation error: {result.get('message')}")

    records_by_id = {r["id"]: r for r in records}
    failed = [r for r in records if r.get("result") in ("failed", "succeededWithIssues")]
    tasks = [r for r in failed if r.get("type") == "Task"]
    # A job can fail without a failed task (agent lost, timeout, YAML error).
    targets = tasks or [r for r in failed if r.get("type") in ("Job", "Stage", "Phase")]
    targets.sort(key=lambda r: (r.get("result") != "failed", r.get("order") or 0))

    line_counts = {}
    if any(r.get("log") for r in targets):
        logs = client.request(f"build/builds/{build['id']}/logs").get("value", [])
        line_counts = {entry["id"]: entry.get("lineCount") for entry in logs}

    for record in targets[:max_failed]:
        out += ["", f"### {record_path(record, records_by_id)} — {record.get('result')}"]
        issues = record.get("issues") or []
        errors = [i for i in issues if i.get("type") == "error"] or issues
        for issue in errors[:10]:
            data = issue.get("data") or {}
            location = ""
            if data.get("sourcepath"):
                location = f" ({data['sourcepath']}:{data.get('linenumber', '?')})"
            out.append(f"- {issue.get('type', 'issue')}: {issue.get('message', '').strip()}{location}")
        if record.get("log") and record["log"].get("id") is not None:
            try:
                tail = log_tail(client, build["id"], record["log"]["id"], line_counts, log_lines)
            except AdoError as err:
                tail = f"(could not fetch log: {err})"
            out += ["", f"Last {log_lines} log lines:", "```text", tail, "```"]
    if len(targets) > max_failed:
        out += ["", f"({len(targets) - max_failed} more failed steps not shown; see the build URL.)"]
    if not out:
        out.append("No failed steps were reported. Open the build URL for details.")
    return "\n".join(out)


def watch(client, start, build, timeout, interval):
    """Poll until the build completes. Prints tasks as they finish. Returns the final build or None."""
    deadline = time.monotonic() + timeout
    seen = set()
    last_status = None
    last_heartbeat = time.monotonic()
    while True:
        status = build.get("status")
        if status != last_status:
            log(start, f"{build_label(build)}: {status}")
            last_status = status
        for record in sorted(get_timeline(client, build["id"]), key=lambda r: (r.get("startTime") or "")):
            if record.get("type") == "Task" and record.get("state") == "completed" and record["id"] not in seen:
                seen.add(record["id"])
                marker = STATUS_MARKERS.get(record.get("result"), f"[{record.get('result')}]")
                log(start, f"  {marker} {record.get('name')}")
                last_heartbeat = time.monotonic()
        if status == "completed":
            return build
        if time.monotonic() >= deadline:
            return None
        if time.monotonic() - last_heartbeat >= 120:
            log(start, f"Still {status}...")
            last_heartbeat = time.monotonic()
        time.sleep(interval)
        build = client.request(f"build/builds/{build['id']}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    parser = argparse.ArgumentParser(description="Watch the Azure Pipelines build for the pushed commit.")
    parser.add_argument("--build-id", type=int, help="Watch this build instead of looking one up")
    parser.add_argument("--commit", help="Commit SHA to look for (default: HEAD, which must be pushed)")
    parser.add_argument("--branch", help="Branch the build ran on (default: current branch)")
    parser.add_argument("--definition-id", default=os.environ.get("AZURE_DEVOPS_PIPELINE_ID"),
                        help="Pipeline (definition) ID; defaults to AZURE_DEVOPS_PIPELINE_ID")
    parser.add_argument("--queue", action="store_true",
                        help="Queue a build if the CI trigger has not started one within --wait-for-build")
    parser.add_argument("--wait-for-build", type=int, default=90,
                        help="Seconds to wait for the CI trigger to queue a build (default 90)")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Seconds to wait for the build to finish (default 1800)")
    parser.add_argument("--interval", type=int, default=20, help="Seconds between status checks (default 20)")
    parser.add_argument("--log-lines", type=int, default=60, help="Log lines to show per failed step (default 60)")
    parser.add_argument("--max-failed", type=int, default=5, help="Failed steps to report in detail (default 5)")
    args = parser.parse_args()

    start = time.monotonic()
    try:
        client = AdoClient(*load_config())
        # load_config reads .env, so the env default for --definition-id may only exist now.
        definition_id = args.definition_id or os.environ.get("AZURE_DEVOPS_PIPELINE_ID")

        if args.build_id:
            build = client.request(f"build/builds/{args.build_id}")
        else:
            commit, branch = resolve_commit_and_branch(args)
            build = find_build(client, start, commit, branch, definition_id, args.wait_for_build, args.interval)
            if build is None:
                if not args.queue:
                    raise AdoError(
                        f"No build found for {commit[:8]} on {branch} after {args.wait_for_build}s. "
                        "The pipeline's CI trigger may not include this branch. "
                        "Rerun with --queue (needs AZURE_DEVOPS_PIPELINE_ID) to start one.", 4)
                if not definition_id:
                    raise AdoError("--queue needs a pipeline ID: set AZURE_DEVOPS_PIPELINE_ID or pass --definition-id.", 2)
                build = queue_build(client, commit, branch, definition_id)
                log(start, f"No CI build appeared, so queued {build_label(build)}.")
        log(start, f"Watching {build_label(build)}: {web_url(build)}")

        final = watch(client, start, build, args.timeout, args.interval)
        if final is None:
            print(f"\nRESULT: timeout. {build_label(build)} is still running after {args.timeout}s.")
            print(f"Resume with: python .github/skills/ado-build-monitor/watch_build.py --build-id {build['id']}")
            return 5

        result = final.get("result")
        print()
        print(f"RESULT: {result}")
        print(f"Build: {build_label(final)}")
        print(f"Commit: {(final.get('sourceVersion') or '')[:8]} on {final.get('sourceBranch', '')}")
        print(f"URL: {web_url(final)}")
        if result == "succeeded":
            return 0
        if result == "canceled":
            print("\nThe build was canceled, not failed. Do not change code for this; ask the user what to do.")
            return 1
        print("\n## Failed steps")
        print(failure_report(client, final, get_timeline(client, final["id"]), args.log_lines, args.max_failed))
        return 1
    except AdoError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return err.exit_code
    except KeyboardInterrupt:
        print("\nStopped watching. The build keeps running in Azure DevOps.", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
