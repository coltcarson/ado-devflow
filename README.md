# 🚀 ADO DevFlow for GitHub Copilot

This kit lets GitHub Copilot Chat take an Azure DevOps work item all the way to a green build, without leaving VS Code:

```text
ADO work item ──► Spec Kit spec, plan and tasks ──► implementation ──► commit and push (Azure Repos)
                                                                              │
                         ┌────────────── fix, commit, push ◄── failed ◄──────┤
                         ▼                                                    │
                  watch Azure Pipelines build ──────────────────► succeeded ──► final report
```

It has three parts:

| Part | File | What it does |
|---|---|---|
| 📋 Work item skill | [`.github/skills/ado-work-item/`](.github/skills/ado-work-item/) | Fetches a work item from Azure Boards as Markdown |
| 👀 Build monitor skill | [`.github/skills/ado-build-monitor/`](.github/skills/ado-build-monitor/) | Waits for the pipeline build of the pushed commit and reports why it failed |
| 🤖 Orchestrator agent | [`.github/agents/ado-devflow.agent.md`](.github/agents/ado-devflow.agent.md) | **ADO DevFlow** in Copilot's agent picker. Runs the whole workflow with the two skills and Spec Kit. |

A **skill** is a folder with a `SKILL.md` that tells Copilot when to use it and how, plus the script it runs. Copilot reads the instructions and runs the script in its terminal. A **custom agent** is a set of standing instructions you pick in the chat's agent dropdown.

## ⚡ Quick start

✅ **Requirements:**

- VS Code with GitHub Copilot Chat in **Agent** mode
- Python 3.9+
- Git that can push to the Azure Repos remote
- [Spec Kit](https://github.com/github/spec-kit)

The scripts use only the Python standard library, so there is nothing to `pip install`.

1. 📦 **Install Spec Kit** into the repository:

   ```bash
   uv tool install specify-cli
   ```

   ```bash
   specify init --here --integration copilot
   ```

   ```bash
   specify extension add git
   ```

2. 🔑 **Configure Azure DevOps access.** Copy `.env.example` to `.env` (it is git-ignored) and fill it in:

   ```ini
   AZURE_DEVOPS_ORG_URL=https://dev.azure.com/your-org
   AZURE_DEVOPS_PROJECT=YourProject
   AZURE_DEVOPS_PAT=<personal access token>
   AZURE_DEVOPS_PIPELINE_ID=42        # optional; the definitionId in the pipeline URL
   ```

   The PAT needs **Work Items: Read** and **Build: Read & execute**. *Execute* is only used to queue a build when the pipeline doesn't start one by itself.

3. 🎚️ **Raise Copilot's request limit.** In VS Code settings, set `"chat.agent.maxRequests": 200`. The default of 25 is too low for the full loop.

4. ▶️ **Run it.** Open Copilot Chat, choose **Agent** mode, select **ADO DevFlow**, and type:

   ```text
   Implement work item 1234
   ```

   The agent pauses after writing the spec and plan so you can review them. Reply `continue` to go on. Add `autopilot` to the prompt to skip the pause.

## ⚙️ How it works

### 1. 📋 Fetch the work item: `get_work_item.py`

```bash
python .github/skills/ado-work-item/get_work_item.py 1234 --out .ado/work-items/ado-1234.md
```

1. Loads the settings from the environment, or from `.env` at the repository root. Real environment variables win.
2. Calls `GET {org}/{project}/_apis/wit/workitems/1234?$expand=all` with the PAT as Basic authentication.
3. Converts the HTML fields (Description, Acceptance Criteria, Repro Steps) to plain Markdown text. Lists keep their nesting, and images become placeholders.
4. Looks up the titles of linked work items (parent, child, related), and lists attachments and hyperlinks.
5. Fetches the 10 most recent discussion comments, which often hold decisions that never reached the description.
6. Prints the Markdown and, with `--out`, also saves it to that file.

If the work item has no acceptance criteria, the output says so, so the spec step treats them as needing clarification.

| Option | Default | Purpose |
|---|---|---|
| `--out PATH` | none | Also write the Markdown to this file |
| `--comments N` | 10 | Number of recent comments to include (`0` to skip) |
| `--include-fields A,B` | none | Extra fields to include, e.g. `Custom.UXNotes` |

🚦 **Exit codes:** `0` ok · `2` missing configuration · `3` API or authentication error · `4` work item not found

### 2. 📝 Write the spec, plan and tasks: Spec Kit

The agent passes the work item Markdown to Spec Kit's skills in order:

1. `speckit-specify` writes `specs/<feature>/spec.md` and creates the feature branch, e.g. `001-export-button`.
2. `speckit-clarify` runs only if the spec has open questions.
3. `speckit-plan` writes the plan.
4. `speckit-tasks` breaks the plan into tasks.
5. `speckit-analyze` checks the documents against each other.
6. `speckit-implement` writes the code.

Before committing, the agent reads `azure-pipelines.yml` and runs the same lint and test commands locally.

### 3. 📤 Commit and push

The agent commits on the feature branch with `#1234` in the message, which links the commit to the work item in Azure Repos. Then it runs `git push -u origin HEAD`. It never pushes to the default branch.

### 4. 👀 Watch the build: `watch_build.py`

```bash
python .github/skills/ado-build-monitor/watch_build.py --queue
```

1. **Checks the push.** Reads HEAD and the current branch, and stops with exit code `2` if that commit hasn't been pushed yet.
2. **Finds the build.** Polls `GET _apis/build/builds?branchName=refs/heads/<branch>` until a build whose `sourceVersion` matches the commit appears. It waits up to 90 seconds. If none appears and `--queue` is set, it queues one with `POST _apis/build/builds`.
3. **Watches it.** Polls the build every 20 seconds. Each poll reads the build's timeline and prints every task as it finishes:

   ```text
   [00:41] build 4567 (storefront-ci #20261017.3): inProgress
   [01:02]   [ok]   Install dependencies
   [01:35]   [FAIL] Run pytest
   ```

4. **Reports the result.** Prints a `RESULT:` line with the build URL. On failure, for each failed task it prints:
   - its path (`Stage > Job > Task`),
   - its error messages from the timeline,
   - the last 60 lines of its log, fetched with `startLine`/`endLine` so large logs aren't downloaded and with timestamps stripped.

   This keeps the report small enough for Copilot to act on.

| Option | Default | Purpose |
|---|---|---|
| `--queue` | off | Queue a build if the CI trigger hasn't started one |
| `--build-id N` | none | Watch, or report on, a specific build |
| `--commit SHA` / `--branch NAME` | HEAD / current branch | Look for a different commit |
| `--definition-id N` | `AZURE_DEVOPS_PIPELINE_ID` | Only consider builds of this pipeline |
| `--wait-for-build S` | 90 | Seconds to wait for the CI trigger |
| `--timeout S` | 1800 | Seconds to wait for the build to finish |
| `--interval S` | 20 | Seconds between polls |
| `--log-lines N` | 60 | Log lines shown per failed step |
| `--max-failed N` | 5 | Failed steps reported in detail |

🚦 **Exit codes:**

| Code | Meaning |
|---|---|
| `0` | ✅ Succeeded |
| `1` | ❌ Failed, partially succeeded or canceled (see the `RESULT:` line) |
| `2` | ⚠️ Usage error, such as HEAD not pushed |
| `3` | 🌐 API error |
| `4` | 🔍 No build found |
| `5` | ⏱️ Timed out. Resume with `--build-id`. |

### 5. 🔧 Fix and retry

On a failed build, the agent:

1. finds the first real error in the report,
2. reproduces it locally with the same command the pipeline ran,
3. fixes it,
4. commits `fix(ci): …`, pushes, and runs the build monitor again.

🛑 It stops and asks you instead of changing code when:

- the build was canceled,
- the failure is infrastructure (no agent available, permissions, feed outage),
- three fix attempts have failed.

🚫 It never weakens tests, lint rules or the pipeline to get a green build.

## 🖐️ Running the scripts by hand

Both scripts work without Copilot, which is useful for checking your setup. Run them from the repository root:

```bash
python .github/skills/ado-work-item/get_work_item.py 1234
```

```bash
python .github/skills/ado-build-monitor/watch_build.py --build-id 4567
```

Use `python3` if `python` isn't on your PATH.

## 🔒 Security

- The PAT is read from `.env` or the environment. It is sent only to `AZURE_DEVOPS_ORG_URL` and never printed. `.env` is git-ignored.
- The skills and agent tell Copilot never to read `.env` and to treat work item text and build logs as data, not instructions.
- Use a short-lived PAT with only the scopes above.

## 📚 More detail

The [setup and demo guide](docs/ado_copilot_devflow_setup_and_demo_guide_10_17_26_19_35_49.md) covers:

- terminal auto-approve settings,
- making the pipeline trigger on feature branches,
- a sample `azure-pipelines.yml`,
- staging a reliable build failure for a demo,
- troubleshooting.
