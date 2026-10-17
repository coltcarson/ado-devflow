# ADO DevFlow: setup and demo guide for GitHub Copilot

This kit makes GitHub Copilot Chat (Agent mode in VS Code) take one Azure DevOps work item all the way to a green build:

**ADO work item → Spec Kit spec, plan and tasks → implementation → commit and push to Azure Repos → watch Azure Pipelines → fix and push until green.**

## What's in the kit

```text
.github/
  agents/ado-devflow.agent.md          Orchestrator custom agent ("ADO DevFlow" in the agent picker)
  skills/ado-work-item/
    SKILL.md                           Tells Copilot when and how to fetch a work item
    get_work_item.py                   Calls the ADO Work Items API; prints Markdown
  skills/ado-build-monitor/
    SKILL.md                           Tells Copilot how to watch a build and read failures
    watch_build.py                     Finds the build for the pushed commit, polls it, reports failures
.env.example                           Configuration template (copy to .env, which is git-ignored)
.gitignore                             Ignores .env and .ado/ (fetched work items)
```

Both scripts use only the Python standard library (3.9+). There is nothing to `pip install`, and they run the same on Windows, macOS and Linux.

## How the pieces fit

| Step | Who does it | How |
|---|---|---|
| Pull work item | `ado-work-item` skill | `GET _apis/wit/workitems/{id}?$expand=all` plus comments. HTML is converted to Markdown. |
| Spec, plan, tasks | Spec Kit skills | `speckit-specify` → `speckit-clarify` (if needed) → `speckit-plan` → `speckit-tasks` → `speckit-analyze` |
| Human review | You | The agent pauses after planning unless you say "autopilot" |
| Implement | `speckit-implement` | The agent then runs the same commands as `azure-pipelines.yml` locally |
| Commit and push | Agent (git) | Commit message includes `#<ID>`, which links the commit to the work item in Azure Repos |
| Monitor | `ado-build-monitor` skill | Matches the build to the pushed commit's SHA, prints tasks live, and on failure reports error lines plus log tails |
| Fix loop | Agent | Reproduce locally → fix → push → monitor again. At most 3 attempts. |

## Prerequisites

- **VS Code** with **GitHub Copilot Chat**, using **Agent** mode. Agent Skills and custom agents need a recent VS Code release.
- **Python 3.9+** on the PATH.
- **Git** that can push to the Azure Repos remote. Git Credential Manager is the easiest option; do a manual `git push` once first so credentials are cached.
- **uv**, to install the Spec Kit CLI.
- An **Azure DevOps PAT** (see the next section).

## Setup

### 1. Copy the kit into the client repository

Copy `.github/agents/`, `.github/skills/ado-*`, `.env.example`, and the `.gitignore` entries into the root of the Azure Repos repository you will demo in.

### 2. Install Spec Kit with Copilot skills and the git extension

```bash
uv tool install specify-cli
```

```bash
specify init --here --integration copilot
```

```bash
specify extension add git
```

`init` installs the Spec Kit skills under `.github/skills/speckit-*`. The **git extension** makes `speckit-specify` create a numbered feature branch such as `001-export-button`. Without it, the agent creates the branch itself. Commit this baseline to the default branch before the demo.

Optionally, create a constitution once (`/speckit-constitution`) that states your stack, test framework and coding standards. Every generated plan follows it.

### 3. Create the PAT and `.env`

Create a PAT at `https://dev.azure.com/<org>/_usersSettings/tokens` with:

| Scope | Why |
|---|---|
| Work Items: Read | Fetch the work item |
| Build: Read & execute | Read builds and logs. *Execute* is only needed for `--queue`. |

Then copy `.env.example` to `.env` and fill it in:

```ini
AZURE_DEVOPS_ORG_URL=https://dev.azure.com/your-org
AZURE_DEVOPS_PROJECT=YourProject
AZURE_DEVOPS_PAT=<token>
AZURE_DEVOPS_PIPELINE_ID=42
```

`AZURE_DEVOPS_PIPELINE_ID` is the `definitionId` in the pipeline's URL. Setting it stops the monitor from picking up a build from a different pipeline, and it is required for `--queue`.

Real environment variables override `.env`. Use them instead of `.env` if the client's policy forbids token files.

### 4. Make sure the pipeline runs on feature branches

The monitor first waits up to 90 seconds for the **CI trigger** to start a build for the pushed commit. If `azure-pipelines.yml` only triggers on `main`, either widen the trigger:

```yaml
trigger:
  branches:
    include: ['*']
```

or rely on `--queue`, which queues the pipeline for the branch and commit itself. The agent passes `--queue` by default.

### 5. VS Code settings (User settings)

```jsonc
{
  // The full loop takes far more than the default 25 agent requests.
  "chat.agent.maxRequests": 200,

  // Auto-approve only the commands this workflow needs. Everything else still asks.
  "chat.tools.terminal.autoApprove": {
    "/^python3? \\.github[\\\\/]skills[\\\\/]ado-(work-item|build-monitor)[\\\\/]/": true,
    "/^git (status|diff|log|add|commit|branch|switch|rev-parse)\\b/": true,
    "/^git push( -u origin HEAD)?$/": true
  }
}
```

For a cautious first run, leave auto-approve out and click **Allow** on each command. If the `ado-*` skills don't appear in Copilot, check that Agent Skills are turned on in your VS Code version's Copilot settings.

### 6. Smoke-test the scripts

Run these from the repository root:

```bash
python .github/skills/ado-work-item/get_work_item.py 1234
```

```bash
python .github/skills/ado-build-monitor/watch_build.py --build-id <a recent build id>
```

## Running the demo

1. Open Copilot Chat, switch to **Agent** mode, and pick **ADO DevFlow** in the agent picker.
2. Prompt: `Implement work item 1234`. For an unattended run, add `autopilot`, which skips the review pause.
3. The agent works through the phases and says which one it is starting:
   1. Fetches and summarizes the work item.
   2. Writes the spec, plan and tasks under `specs/<feature>/`, then **pauses for review**. Reply `continue`.
   3. Implements the tasks and runs the pipeline's checks locally.
   4. Commits and pushes the feature branch.
   5. Watches the build, printing tasks live (`[ok]`, `[FAIL]`).
   6. On failure: names the root cause, reproduces it, fixes it, pushes, and watches again.
   7. Ends with a report: branch, spec, commits, each build attempt and the final build URL.

The agent stops and asks you, instead of changing code, when:

- the build is canceled,
- it hits an authentication or infrastructure error,
- three fix attempts have failed, or
- the work item or logs contain instructions aimed at it.

### Making the self-healing moment reliable

The agent runs the pipeline's checks locally before pushing, so most bugs never reach the pipeline. That is the right behavior, but it means the fix loop may not appear on its own. To show it reliably, add a check that **only runs in CI**. Azure Pipelines sets `TF_BUILD=True` on its agents:

```python
# tests/test_ci_only_checks.py
import os
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("TF_BUILD") != "True", reason="runs in Azure Pipelines only")

def test_every_button_has_accessible_label():
    ...  # e.g. scan templates for <button> elements without aria-label
```

Choose a rule the work item doesn't mention, such as accessibility labels or a naming convention. The first build then fails on something real, and the agent has to read the pipeline log to find and fix it. **Tell the audience the check was planted.** The point is to show the loop, and saying so keeps the demo credible.

### Sample pipeline for a Python repository

```yaml
trigger:
  branches:
    include: ['*']

pool:
  vmImage: ubuntu-latest

steps:
  - task: UsePythonVersion@0
    inputs:
      versionSpec: '3.12'
  - script: pip install -r requirements.txt ruff pytest
    displayName: Install dependencies
  - script: ruff check .
    displayName: Lint
  - script: pytest -q
    displayName: Run tests
```

Keep the pipeline to 2–4 minutes. Every fix cycle costs one full pipeline run in front of the audience.

## Script reference

### `get_work_item.py <ID>`

| Option | Default | Purpose |
|---|---|---|
| `--out PATH` | none | Also save the Markdown, e.g. `.ado/work-items/ado-1234.md` |
| `--comments N` | 10 | Include the N most recent comments (`0` to skip) |
| `--include-fields A,B` | none | Extra field reference names, e.g. `Custom.UXNotes` |

Exit codes: `0` ok, `2` configuration error, `3` API or authentication error, `4` not found.

### `watch_build.py`

| Option | Default | Purpose |
|---|---|---|
| `--queue` | off | Queue a build if CI hasn't started one within `--wait-for-build` |
| `--build-id N` | none | Watch or report on a specific build (skips the commit lookup) |
| `--commit SHA` / `--branch NAME` | HEAD / current branch | Look up a different commit |
| `--definition-id N` | `AZURE_DEVOPS_PIPELINE_ID` | Limit the lookup to one pipeline |
| `--wait-for-build S` | 90 | Seconds to wait for the CI trigger |
| `--timeout S` | 1800 | Seconds to wait for the build to finish |
| `--interval S` | 20 | Seconds between polls |
| `--log-lines N` | 60 | Log lines shown per failed step |
| `--max-failed N` | 5 | Failed steps reported in detail |

Exit codes: `0` succeeded; `1` failed, partially succeeded or canceled (see the `RESULT:` line); `2` usage error, such as HEAD not pushed; `3` API error; `4` no build found; `5` timed out (resume with `--build-id`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `rejected the credentials (returned a sign-in page)` | The PAT is wrong or expired, or `AZURE_DEVOPS_ORG_URL` points at the wrong org |
| `returned 401/403` | The PAT is missing a scope: Work Items Read, or Build Read (& execute for `--queue`) |
| `No build found ... after 90s` | The CI trigger doesn't include the branch. Use `--queue` or widen `trigger:`. |
| `HEAD ... is not what the remote has` | The agent forgot to push. It should push and rerun. |
| The agent stops mid-loop with "continue?" | Raise `chat.agent.maxRequests` |
| The agent's terminal returns before the build finishes | Rerun with `--build-id <id>` (printed by the script). This doesn't count as a fix attempt. |
| The Spec Kit steps don't create a branch | Install the git extension (`specify extension add git`). The agent also creates the branch as a fallback. |

## Security notes for the client

- The PAT lives only in `.env` (git-ignored) or the environment. The scripts send it only to `AZURE_DEVOPS_ORG_URL` and never print it. The agent is told never to read `.env`.
- Use a short-lived PAT with the minimum scopes above. Revoke it after the demo.
- Work item text and build logs are untrusted input. Both skills and the agent tell Copilot to treat them as data and to flag embedded instructions.
- Terminal auto-approve is limited to the two scripts and the git commands the workflow needs. Arbitrary commands still require a click.
- The agent never pushes to the default branch and never weakens tests or the pipeline to get a green build. It finishes by suggesting a pull request, so a human still reviews and merges.
