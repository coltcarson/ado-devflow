#!/usr/bin/env python3
"""Fetch an Azure DevOps work item and print it as Markdown for spec generation.

Usage:
    python .github/skills/ado-work-item/get_work_item.py 1234
    python .github/skills/ado-work-item/get_work_item.py 1234 --out .ado/work-items/ado-1234.md
    python .github/skills/ado-work-item/get_work_item.py 1234 --include-fields Custom.UXNotes

Configuration comes from environment variables, or from a .env file at the
repository root (never committed):
    AZURE_DEVOPS_ORG_URL   e.g. https://dev.azure.com/contoso
    AZURE_DEVOPS_PROJECT   e.g. Storefront
    AZURE_DEVOPS_PAT       Personal access token with the Work Items (Read) scope

Exit codes: 0 ok, 2 configuration or usage error, 3 API error, 4 work item not found.

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
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

API_VERSION = "7.1"
COMMENTS_API_VERSION = "7.1-preview.4"

RELATION_NAMES = {
    "System.LinkTypes.Hierarchy-Reverse": "Parent",
    "System.LinkTypes.Hierarchy-Forward": "Child",
    "System.LinkTypes.Related": "Related",
    "System.LinkTypes.Dependency-Forward": "Successor",
    "System.LinkTypes.Dependency-Reverse": "Predecessor",
    "Microsoft.VSTS.Common.TestedBy-Forward": "Tested by",
}


# --- Azure DevOps client (kept in sync with ado-build-monitor/watch_build.py) ---

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


# --- HTML to text ---------------------------------------------------------------

class _HtmlToText(HTMLParser):
    BLOCK_TAGS = {"p", "div", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.list_depth = 0

    def _at_line_start(self) -> bool:
        return not self.parts or self.parts[-1].endswith(("\n", "- "))

    def handle_starttag(self, tag, attrs):
        if tag in ("ul", "ol"):
            self.list_depth += 1
        elif tag == "li":
            self.parts.append("\n" + "  " * max(self.list_depth - 1, 0) + "- ")
        elif tag == "br" or tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag == "img":
            alt = dict(attrs).get("alt") or "image"
            self.parts.append(f"[{alt} (image attached in ADO)]")

    def handle_endtag(self, tag):
        if tag in ("ul", "ol"):
            self.list_depth = max(self.list_depth - 1, 0)
            if self.list_depth == 0:
                self.parts.append("\n")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        text = re.sub(r"\s+", " ", data)
        if self._at_line_start():
            text = text.lstrip()
        if text:
            self.parts.append(text)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _HtmlToText()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --- Work item rendering ----------------------------------------------------------

def person(value) -> str:
    if isinstance(value, dict):
        return value.get("displayName") or value.get("uniqueName") or ""
    return value or ""


def fetch_related_titles(client: AdoClient, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    try:
        result = client.request(
            "wit/workitems",
            {"ids": ",".join(str(i) for i in ids[:200]),
             "fields": "System.Title,System.WorkItemType,System.State",
             "errorPolicy": "omit"},
        )
    except AdoError:
        return {}
    return {item["id"]: item.get("fields", {}) for item in result.get("value", []) if item}


def fetch_comments(client: AdoClient, work_item_id: int, limit: int) -> list[dict] | None:
    try:
        result = client.request(
            f"wit/workItems/{work_item_id}/comments",
            {"$top": limit, "order": "desc"},
            api_version=COMMENTS_API_VERSION,
        )
    except AdoError:
        return None
    return result.get("comments", [])


def render_markdown(client: AdoClient, item: dict, include_fields: list[str], comment_limit: int) -> str:
    fields = item.get("fields", {})
    work_item_id = item["id"]
    title = fields.get("System.Title", "(untitled)")
    item_type = fields.get("System.WorkItemType", "Work Item")
    link = (item.get("_links", {}).get("html", {}).get("href")
            or f"{client.org_url}/{urllib.parse.quote(client.project)}/_workitems/edit/{work_item_id}")

    out = [f"# {item_type} {work_item_id}: {title}", ""]
    rows = [
        ("State", fields.get("System.State")),
        ("Reason", fields.get("System.Reason")),
        ("Assigned To", person(fields.get("System.AssignedTo"))),
        ("Area Path", fields.get("System.AreaPath")),
        ("Iteration", fields.get("System.IterationPath")),
        ("Priority", fields.get("Microsoft.VSTS.Common.Priority")),
        ("Story Points", fields.get("Microsoft.VSTS.Scheduling.StoryPoints")),
        ("Severity", fields.get("Microsoft.VSTS.Common.Severity")),
        ("Tags", fields.get("System.Tags")),
        ("Link", link),
    ]
    out += ["| Field | Value |", "|---|---|"]
    out += [f"| {name} | {value} |" for name, value in rows if value not in (None, "")]

    sections = [
        ("Description", fields.get("System.Description")),
        ("Acceptance Criteria", fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")),
        ("Repro Steps", fields.get("Microsoft.VSTS.TCM.ReproSteps")),
        ("System Info", fields.get("Microsoft.VSTS.TCM.SystemInfo")),
    ]
    sections += [(name, fields.get(name)) for name in include_fields]
    for heading, value in sections:
        text = html_to_text(value) if isinstance(value, str) else (str(value) if value is not None else "")
        if text:
            out += ["", f"## {heading}", "", text]

    if not fields.get("Microsoft.VSTS.Common.AcceptanceCriteria"):
        out += ["", "> Note: this work item has no Acceptance Criteria field. "
                    "Treat acceptance criteria as needing clarification."]

    relations = item.get("relations") or []
    linked = []
    attachments = []
    hyperlinks = []
    for rel in relations:
        rel_type = rel.get("rel", "")
        if rel_type == "AttachedFile":
            attachments.append(rel.get("attributes", {}).get("name", "attachment"))
        elif rel_type == "Hyperlink":
            hyperlinks.append(rel.get("url", ""))
        elif rel_type in RELATION_NAMES:
            match = re.search(r"/workItems/(\d+)$", rel.get("url", ""), re.IGNORECASE)
            if match:
                linked.append((RELATION_NAMES[rel_type], int(match.group(1))))

    if linked:
        titles = fetch_related_titles(client, [i for _, i in linked])
        out += ["", "## Related Work Items", ""]
        for relation, linked_id in linked:
            info = titles.get(linked_id, {})
            label = f"{info.get('System.WorkItemType', '')} {linked_id}".strip()
            suffix = f": {info['System.Title']} ({info.get('System.State', '')})" if "System.Title" in info else ""
            out.append(f"- {relation}: {label}{suffix}")
    if attachments:
        out += ["", "## Attachments", ""] + [f"- {name}" for name in attachments]
    if hyperlinks:
        out += ["", "## Links", ""] + [f"- {url}" for url in hyperlinks]

    if comment_limit > 0:
        comments = fetch_comments(client, work_item_id, comment_limit)
        if comments:
            out += ["", f"## Recent Comments (newest first, up to {comment_limit})", ""]
            for comment in comments:
                author = person(comment.get("createdBy"))
                date = (comment.get("createdDate") or "")[:10]
                body = html_to_text(comment.get("text")).replace("\n", "\n  ")
                out.append(f"- **{author}** ({date}): {body}")

    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Fetch an Azure DevOps work item as Markdown.")
    parser.add_argument("id", type=int, help="Work item ID, e.g. 1234")
    parser.add_argument("--out", help="Also write the Markdown to this file (directories are created)")
    parser.add_argument("--include-fields", default="",
                        help="Comma-separated extra field reference names, e.g. Custom.UXNotes")
    parser.add_argument("--comments", type=int, default=10,
                        help="Number of recent comments to include (0 to skip, default 10)")
    args = parser.parse_args()

    try:
        client = AdoClient(*load_config())
        try:
            item = client.request(f"wit/workitems/{args.id}", {"$expand": "all"})
        except AdoError as err:
            if err.exit_code == 4:
                raise AdoError(f"Work item {args.id} was not found in project {client.project}.", 4) from None
            raise
        extra = [name.strip() for name in args.include_fields.split(",") if name.strip()]
        markdown = render_markdown(client, item, extra, args.comments)
    except AdoError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return err.exit_code

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        print(f"Saved to {path}", file=sys.stderr)
    print(markdown, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
