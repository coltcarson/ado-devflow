# ADO DevFlow security review: research and validation

Research checked October 29, 2026. This implementation provides advisory vulnerability review through GitHub Copilot, without external scanners or a security push gate.

## Research findings

Codex Security's published workflow builds repository context and a threat model, investigates realistic vulnerabilities, validates evidence, and proposes repairs. It can use isolated validation environments, which are infrastructure capabilities rather than properties of a Markdown skill. See [OpenAI's overview](https://openai.com/index/codex-security-now-in-research-preview/) and [Codex Security documentation](https://help.openai.com/en/articles/20001107-codex-security).

OpenAI explains why this workflow begins with system behavior and security controls instead of a SAST report: the presence of a check does not establish that it enforces the intended security property. This supports examining transformations, callers, deployment prerequisites, and counterevidence. See [Why Codex Security doesn't start with a SAST report](https://openai.com/index/why-codex-security-doesnt-include-sast/).

The locally installed Codex Security plugin, version `0.1.32`, was inspected as design research. Its repository, diff, and core review instructions emphasize source-backed findings, fixed scope, deleted-file inspection, validation, candidate dispositions, and honest coverage. Its Codex-specific host tools, schemas, and orchestration were not copied into this skill. Inspecting these resources did not launch a scan.

GitHub Copilot supports repository skills under `.github/skills/<name>/SKILL.md`, with name/description frontmatter and optional executable resources. VS Code supports explicit slash-command invocation and contextual loading. See [GitHub's authoring documentation](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/add-skills) and [VS Code agent skills](https://code.visualstudio.com/docs/agent-customization/agent-skills).

This implements a portable review workflow, without claiming proprietary model performance, hosted isolation, or Codex Security's scan-management capabilities. A skill cannot guarantee that Copilot discovers vulnerabilities or follows every instruction.

## Implemented behavior

The [security skill](../.github/skills/ado-security-scan/SKILL.md) provides changes and repository modes. Copilot maps trust boundaries, investigates risks, validates candidates, and writes structured review data. The Python helper supplies deterministic inventories, fingerprints, and reports. It never calls a model, invokes a vulnerability scanner, or decides whether code is vulnerable.

The [ADO DevFlow agent](../.github/agents/ado-devflow.agent.md) captures its baseline before Spec Kit edits. Starting on the target branch uses its starting HEAD; an existing feature branch uses the merge base with a verified target. The baseline remains fixed across CI retries. Dirty paths at initialization are recorded, without claiming ownership of every later change in those paths.

Before each push, the agent reviews cumulative changes and may attempt two security repair-and-recheck cycles for confirmed introduced/worsened findings within the work item. This budget is separate from the existing three CI retries. Existing unrelated vulnerabilities are documented. Full-repository scans report by default and require an explicit fix request to modify source.

All security outcomes are advisory. High/critical findings, incomplete coverage, unavailable review tools, and stale snapshots require disclosure but do not block the normal workflow. Existing restrictions on credentials, branch safety, and weakening tests still apply.

## Helper interface and artifacts

Run `python .github/skills/ado-security-scan/security_scan.py --help` from the target repository. Python 3.9+ and Git are the only dependencies; use `python3` if needed.

| Command | Purpose |
|---|---|
| `init --run NAME --mode changes --base REF` | Pin a baseline and record pre-existing edits; no implicit fallback for an invalid baseline. |
| `init --run NAME --mode repository` | Initialize current-tree review. |
| `prepare --run NAME` | Create a snapshot and editable review JSON template; return their paths. |
| `report --run NAME --review PATH` | Validate review structure and format an advisory report; does not verify reasoning. |
| `status --run NAME` | Compare the current content/index fingerprint with the last report's snapshot. |

State is under `.ado/security/`, already ignored by this kit. Reports use `docs/ado-security-review-<run>-<snapshot>[-rN]-MM-DD-YY-HH-MM-SS.md`, using the runtime machine's local timezone. Same-second repeats receive distinct filenames and exclusive creation. Reports are not staged automatically. This research document uses America/New_York time.

Snapshots capture cumulative changes, index entries, worktree hashes, pre-existing-edit markers, and the baseline. Renames retain old and new paths. Both staged and working-tree differences remain visible, including changes that cancel each other. Supporting current files outside the diff also affect the fingerprint. Staging can invalidate it; committing already-reviewed staged bytes alone does not.

Only helper-owned state and registered generated report paths are automatically excluded. Other documentation remains visible. Credential contents are not opened, symlink targets are not followed, and binary/submodule contents are explicit gaps. External services and unread credential contents are outside the fingerprint's guarantees.

Review JSON records the threat model, reviewed/excluded paths, limitations, and findings. Findings carry severity, open/fixed/rejected/unvalidated disposition, attribution, locations, evidence, attacker, attack path, impact, prerequisites, counterevidence, validation, remediation, and history. Fixed/rejected findings whose paths leave the diff can cite an earlier `origin_snapshot_id`. Open/unvalidated findings need a current in-scope location.

Exit `0` means successful bookkeeping, even with critical findings or stale/incomplete reviews. Exit `2` is an operational error requiring an incomplete-review disclosure, not a push gate. Limited output redaction is a backstop: authors must never put credentials into review JSON.

## Validation performed

```bash
python -m unittest discover -s .github/skills/ado-security-scan/tests -v
```

The suite passed 25 tests on this host. It uses temporary repositories rather than changing this repository's index or history. Tests cover cumulative commits, staged/unstaged/untracked files, deleted/renamed paths, unusual filenames, fixed baselines, pre-existing edits, supporting-code/index freshness, commit-only transitions, exclusion boundaries, protected files, symlinks, binary/submodule gaps, historical fixed findings, report naming/redaction, artifact path safety, and advisory exit behavior. Three fixture tests demonstrate the vulnerable and protected examples' local behavior and check the uncertain example's missing control.

The skill-creator validator accepted the frontmatter, and `git diff --check` passed for tracked edits. These checks establish packaging and helper behavior, not Copilot detection quality or a vulnerability-free repository.

## Copilot behavioral evaluation

The [behavioral cases](../.github/skills/ado-security-scan/tests/behavior_cases.json) cover a reachable authorization bypass, effective counter-control, unavailable external authorization, scoped repair, and advisory handling with a malicious source comment.

For each case, use a fresh disposable Git repository with the skill/helper, a `.gitignore` excluding `.ado/`, and the case files. Do not provide credentials or a push-capable remote. When `baseline_files` exists, commit those files and initialize review before applying `files`; otherwise materialize `files` and use repository mode. Give Copilot only the case's prompt, context, and source files. Withhold `expected` criteria until grading.

Inspect reports and source diffs for evidence-backed confirmation, rejection of the protected counterexample, explicit uncertainty, scoped repairs, honest test results, retained finding history, and advisory behavior. Change a reviewed file after reporting to verify re-review or explicit stale disclosure. Never substitute a self-reported pass for inspecting the artifacts.

The Copilot CLI is not installed on this host, so an end-to-end Copilot evaluation was not run. VS Code discovery, actual model compliance, and behavior across Copilot models remain manual acceptance checks. No live scan, publication, commit, or push was performed during implementation.
