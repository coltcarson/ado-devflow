#!/usr/bin/env python3
"""Track advisory Copilot security reviews. No vulnerability detection or push gate.

Python 3.9+, Git, standard library only. Run from the repository being reviewed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import uuid


class ScanError(Exception):
    pass


def git(root, *args):
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        # Git errors can contain source text or URLs; do not echo raw stderr.
        raise ScanError("Git operation failed: " + args[0] + ". Check repository and refs.")
    return result.stdout


def names(data):
    return [os.fsdecode(value) for value in data.split(b"\0") if value]


def read_json(path):
    if path.is_symlink():
        raise ScanError("Scan artifacts must not be symlinks.")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    if path.is_symlink():
        raise ScanError("Scan artifacts must not be symlinks.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def safe_directory(root, relative):
    path = root / relative
    if path.resolve() != path:
        raise ScanError("Output directory must not contain symlinks.")
    return path


def store(root):
    return safe_directory(root, ".ado/security")


def run_path(root, run):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", run):
        raise ScanError("Run name must be 1-80 letters, digits, underscores or hyphens.")
    return safe_directory(root, ".ado/security/" + run)


def outputs(root):
    registry = store(root) / "outputs.json"
    return read_json(registry) if registry.exists() else []


def is_output(path, generated):
    return path.startswith(".ado/security/") or path in generated


def protected(path):
    name = PurePosixPath(path).name.lower()
    return (name == ".env" or
            (name.startswith(".env.") and name not in (".env.example", ".env.sample")) or
            name.endswith((".pem", ".key", ".p12", ".pfx")) or
            name in ("id_rsa", "id_ed25519", "credentials"))


def index_entries(root):
    entries = {}
    for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if record:
            meta, path = record.split(b"\t", 1)
            entries.setdefault(os.fsdecode(path), []).append(meta.decode("ascii"))
    return entries


def file_state(root, path, index):
    """Hash bytes without exposing them; never read secrets or follow symlinks."""
    target = root / path
    if any(entry.startswith("160000 ") for entry in index):
        return {"kind": "submodule", "reason": "Submodule contents not reviewed; scan separately."}
    if target.parent.resolve() != target.parent:
        return {"kind": "excluded", "reason": "Symlinked parent directory not followed."}
    if not target.exists() and not target.is_symlink():
        if protected(path):
            return {"kind": "protected", "reason": "Deleted credential file; baseline contents not read."}
        return {"kind": "deleted"}
    info = target.lstat()
    if protected(path):
        return {"kind": "protected", "reason": "Credential file contents not read.",
                "metadata": [info.st_size, info.st_mtime_ns, info.st_mode]}
    if target.is_symlink():
        return {"kind": "symlink", "target": os.readlink(target),
                "reason": "Link target recorded; target contents not followed."}
    if not target.is_file():
        return {"kind": "excluded", "reason": "Not a regular source file."}
    digest = hashlib.sha256()
    binary = False
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
            binary = binary or b"\0" in chunk
    value = {"kind": "binary" if binary else "file", "sha256": digest.hexdigest(),
             "executable": bool(info.st_mode & 0o111)}
    if binary:
        value["reason"] = "Binary content requires a separate review method."
    return value


def changed(root, base):
    # No rename guessing: both old and new names remain visible to the reviewer.
    args = ("--no-ext-diff", "--no-textconv", "--name-only", "--no-renames", "-z", base, "--")
    return set(names(git(root, "diff", *args))) | set(names(git(root, "diff", "--cached", *args)))


def capture(root, state):
    generated = outputs(root)
    index = index_entries(root)
    untracked = set(names(git(root, "ls-files", "--others", "--exclude-standard", "-z")))
    current = set(index) | untracked
    scoped = current if state["mode"] == "repository" else changed(root, state["base"]) | untracked
    scoped = {p for p in scoped if not is_output(p, generated)}
    # Supporting code also influences conclusions, so fingerprint the entire current tree.
    material = {}
    for path in sorted(current | scoped):
        if not is_output(path, generated):
            material[path] = {"worktree": file_state(root, path, index.get(path, [])),
                              "index": index.get(path, [])}
    fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return {
        "head": git(root, "rev-parse", "HEAD").decode().strip(),
        "fingerprint": fingerprint,
        "files": [{"path": p, "preexisting_edit": p in state["preexisting_edits"],
                   **material[p]} for p in sorted(scoped)],
    }


def initialize(root, run, mode, base):
    directory = run_path(root, run)
    if directory.exists():
        raise ScanError("Run already exists; reuse prepare or choose a new run name.")
    if mode == "changes" and not base:
        raise ScanError("Changes mode requires an explicit verified --base; no silent HEAD fallback.")
    resolved = None
    if base:
        resolved = git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()
    dirty = changed(root, "HEAD") | set(names(git(root, "ls-files", "--others", "--exclude-standard", "-z")))
    state = {"version": 1, "mode": mode, "base": resolved,
             "starting_head": git(root, "rev-parse", "HEAD").decode().strip(),
             "preexisting_edits": sorted(p for p in dirty if not is_output(p, outputs(root)))}
    initial = capture(root, state)
    write_json(directory / "state.json", state)
    write_json(directory / "initial.json", initial)
    return {"run": run, "base": resolved, "preexisting_edits": state["preexisting_edits"]}


def prepare(root, run):
    directory = run_path(root, run)
    state = read_json(directory / "state.json")
    snapshot = capture(root, state)
    identifier = uuid.uuid4().hex
    snapshot.update({"id": identifier, "base": state["base"], "mode": state["mode"]})
    write_json(directory / (identifier + ".snapshot.json"), snapshot)
    review = {"snapshot_id": identifier, "threat_model": "",
              "reviewed": [], "excluded": [], "findings": [], "limitations": []}
    review_path = directory / (identifier + ".review.json")
    write_json(review_path, review)
    return {"snapshot": str(directory / (identifier + ".snapshot.json")), "review": str(review_path)}


def validate_review(review, snapshot, historical_paths=None):
    historical_paths = historical_paths or {}
    if not isinstance(review.get("threat_model"), str):
        raise ScanError("threat_model must be text.")
    for key in ("reviewed", "excluded", "findings", "limitations"):
        if not isinstance(review.get(key), list):
            raise ScanError(key + " must be a list.")
    scope = {row["path"] for row in snapshot["files"]}
    if any(not isinstance(p, str) or p not in scope for p in review["reviewed"]):
        raise ScanError("reviewed must contain only inventory paths.")
    if any(not isinstance(s, str) for s in review["limitations"]):
        raise ScanError("limitations must contain text.")
    for exclusion in review["excluded"]:
        if (not isinstance(exclusion, dict) or exclusion.get("path") not in scope or
                not isinstance(exclusion.get("reason"), str) or not exclusion["reason"].strip()):
            raise ScanError("Each exclusion needs an inventory path and a reason.")
    seen = set()
    for finding in review["findings"]:
        fields = ("id", "title", "path", "severity", "status", "attribution", "evidence",
                  "attacker", "attack_path", "impact", "prerequisites", "counterevidence", "remediation")
        if not isinstance(finding, dict) or any(not isinstance(finding.get(k), str) or not finding[k].strip() for k in fields):
            raise ScanError("Finding fields must be nonempty strings; use 'unknown' where unresolved.")
        if finding["id"] in seen:
            raise ScanError("Duplicate finding ID.")
        seen.add(finding["id"])
        if finding["severity"] not in ("critical", "high", "medium", "low", "unknown"):
            raise ScanError("Invalid severity.")
        if finding["status"] not in ("open", "fixed", "rejected", "unvalidated"):
            raise ScanError("Invalid finding status.")
        if finding["attribution"] not in ("introduced", "worsened", "preexisting", "unknown"):
            raise ScanError("Invalid attribution.")
        if finding["path"] not in scope:
            historical = historical_paths.get(finding.get("origin_snapshot_id"), set())
            if finding["status"] not in ("fixed", "rejected") or finding["path"] not in historical:
                raise ScanError("Finding location must be in scope, or a resolved finding must cite its origin snapshot.")
        if type(finding.get("line")) is not int or finding["line"] < 1:
            raise ScanError("Finding line must be a positive integer; specify revision in evidence.")
        validation = finding.get("validation", {})
        if (not isinstance(validation, dict) or validation.get("method") not in ("source", "test", "none") or
                not isinstance(validation.get("detail"), str) or not validation["detail"].strip()):
            raise ScanError("Validation needs method source/test/none and a nonempty detail.")
        if finding["status"] in ("open", "fixed") and (validation["method"] == "none" or finding["severity"] == "unknown"):
            raise ScanError("Confirmed findings need validation and a calibrated severity.")
        if not isinstance(finding.get("history"), list) or any(not isinstance(s, str) for s in finding["history"]):
            raise ScanError("Finding history must be a list of text entries.")


def redact(text):
    # Defense in depth only. Authors must never put secrets into review JSON.
    text = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]+\b", "[REDACTED]", text)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=-]+", r"\1 [REDACTED]", text)
    return re.sub(r"(?i)\b(AZURE_DEVOPS_PAT|password|api[_-]?key|access[_-]?token)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1=[REDACTED]", text)


def display(value):
    return html.escape(redact(str(value))).replace("\n", "\n  ")


def report(root, run, review_file):
    directory = run_path(root, run)
    review_file = review_file.absolute()
    if review_file.parent != directory or not review_file.name.endswith(".review.json"):
        raise ScanError("Use the review JSON path returned by prepare for this run.")
    state = read_json(directory / "state.json")
    review = read_json(review_file)
    identifier = review.get("snapshot_id", "")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ScanError("Invalid snapshot_id.")
    snapshot = read_json(directory / (identifier + ".snapshot.json"))
    historical_paths = {}
    for finding in review.get("findings", []):
        origin = finding.get("origin_snapshot_id") if isinstance(finding, dict) else None
        if origin is not None:
            if not isinstance(origin, str) or not re.fullmatch(r"[0-9a-f]{32}", origin):
                raise ScanError("Invalid origin_snapshot_id.")
            prior = read_json(directory / (origin + ".snapshot.json"))
            historical_paths[origin] = {row["path"] for row in prior["files"]}
    validate_review(review, snapshot, historical_paths)
    fresh = capture(root, state)["fingerprint"] == snapshot["fingerprint"]
    reviewed = set(review["reviewed"])
    exclusions = {e["path"]: e["reason"] for e in review["excluded"]}
    for row in snapshot["files"]:
        if row["worktree"].get("reason"):
            exclusions[row["path"]] = row["worktree"]["reason"]
        if any(entry.split()[2] != "0" for entry in row["index"]):
            exclusions[row["path"]] = "Unmerged index; review incomplete."
    reviewed -= set(exclusions)
    missing = {f["path"] for f in snapshot["files"]} - reviewed - set(exclusions)
    complete = (not missing and not exclusions and bool(review["threat_model"].strip())
                and not review["limitations"]
                and not any(f["status"] == "unvalidated" for f in review["findings"]))
    status = "stale" if not fresh else "complete" if complete else "incomplete"
    counts = Counter((f["severity"], f["status"]) for f in review["findings"])
    lines = ["# Advisory security review", "", "Security findings do not block commits or pushes.", "",
             "- Review status: " + status, "- Mode: " + state["mode"],
             "- Baseline: " + (state["base"] or "current repository"),
             "- Reviewed HEAD: " + snapshot["head"], "- Snapshot: " + identifier,
             "- Content fingerprint: " + snapshot["fingerprint"],
             "- Coverage: %d of %d inventory paths reviewed" % (len(reviewed), len(snapshot["files"])),
             "", "## Threat model", "", display(review["threat_model"] or "Not supplied; review incomplete."),
             "", "## Finding counts", ""]
    lines += ["- %s / %s: %d" % (severity, disposition, count) for (severity, disposition), count in sorted(counts.items())]
    if not counts:
        lines.append("No findings recorded. This is not proof that the repository is secure.")
    lines += ["", "## Findings", ""]
    for finding in review["findings"]:
        lines += ["### " + display(finding["id"] + ": " + finding["title"]), ""]
        for key, value in finding.items():
            if key not in ("id", "title"):
                lines.append("- " + key + ": " + display(json.dumps(value, ensure_ascii=True) if isinstance(value, (dict, list)) else value))
        lines.append("")
    lines += ["## Coverage and limitations", "",
              "Freshness checks cover content and index changes, not external services, runtime state, or unread credential contents."]
    if not fresh:
        lines.append("STALE: current files differ from the reviewed snapshot. Re-review or disclose incomplete review before pushing.")
    for path in sorted(reviewed):
        lines.append("- Reviewed: " + display(path))
    for path, reason in sorted(exclusions.items()):
        lines.append("- Excluded: " + display(path) + " — " + display(reason))
    for path in sorted(missing):
        lines.append("- Unreviewed: " + display(path))
    for limitation in review["limitations"]:
        lines.append("- " + display(limitation))
    lines += ["", "## Pre-existing workspace edits", "",
              "These paths were dirty at initialization; this does not establish ownership of later edits."]
    lines += ["- " + display(p) for p in state["preexisting_edits"]] or ["None recorded."]
    docs = safe_directory(root, "docs")
    docs.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%m-%d-%y-%H-%M-%S")
    # Exclusive creation, including same-second repeats of the same snapshot.
    sequence = 1
    while True:
        suffix = "" if sequence == 1 else "-r" + str(sequence)
        path = docs / ("ado-security-review-" + run + "-" + identifier[:8] + suffix + "-" + stamp + ".md")
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
            break
        except FileExistsError:
            sequence += 1
    generated = outputs(root)
    generated.append(path.relative_to(root).as_posix())
    write_json(store(root) / "outputs.json", generated)
    result = {"report": str(path), "status": status, "snapshot_id": identifier,
              "counts": {s + "/" + d: n for (s, d), n in sorted(counts.items())}}
    write_json(directory / "latest-report.json", result)
    return result


def freshness(root, run):
    directory = run_path(root, run)
    latest = read_json(directory / "latest-report.json")
    if not isinstance(latest.get("snapshot_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", latest["snapshot_id"]):
        raise ScanError("Invalid saved snapshot_id.")
    snapshot = read_json(directory / (latest["snapshot_id"] + ".snapshot.json"))
    fresh = capture(root, read_json(directory / "state.json"))["fingerprint"] == snapshot["fingerprint"]
    return {**latest, "status": latest["status"] if fresh else "stale", "fresh": fresh,
            "advisory": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "prepare", "report", "status"):
        command = commands.add_parser(name)
        command.add_argument("--run", required=True)
        if name == "init":
            command.add_argument("--mode", choices=("changes", "repository"), required=True)
            command.add_argument("--base")
        if name == "report":
            command.add_argument("--review", type=Path, required=True)
    args = parser.parse_args()
    try:
        root = Path(os.fsdecode(git(Path.cwd(), "rev-parse", "--show-toplevel")).strip()).resolve()
        if args.command == "init":
            result = initialize(root, args.run, args.mode, args.base)
        elif args.command == "prepare":
            result = prepare(root, args.run)
        elif args.command == "report":
            result = report(root, args.run, args.review)
        else:
            result = freshness(root, args.run)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0  # Includes high/critical findings, stale reports and incomplete reviews.
    except (ScanError, OSError, ValueError, KeyError, TypeError) as error:
        # Do not leak arbitrary review content or OS error strings into logs.
        print("ERROR: " + (str(error) if isinstance(error, ScanError) else
                          "Cannot read/write scan artifacts; check paths and JSON format.") +
              " Security review is incomplete, not a push gate.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
