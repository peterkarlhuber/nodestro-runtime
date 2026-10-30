# Nodestro Runtime

The workflow engine of [Nodestro](https://github.com/peterkarlhuber/nodestro-studio) as a standalone Python
library, without a server and without a user interface. It runs multi-agent workflows (agents, decisions,
human approvals, sub-workflows, result files), builds every agent with
[Orkexa](https://github.com/peterkarlhuber/orkexa) and stores everything in one SQLite database inside a data
directory.

The runtime is part of the Nodestro platform; [PLATFORM.md](https://github.com/peterkarlhuber/nodestro-studio/blob/master/docs/PLATFORM.md)
gives an overview of all repositories. The studio and the HTTP API (`nodestro-api`) are built on top of this
package; the runtime itself depends on neither.

## Contents

| Area | Modules |
| --- | --- |
| Workflow model | `schemas`, `graph`, `conditions`, `forms`, `catalog`, `versions`, `release` |
| Execution | `engine` (DAG, parallel branches, live events, budget, cancellation), `launcher`, `preflight`, `resume`, `run_views`, `usage_log`, `traces` |
| Agents | `agent_builder` (`AgentConfig` → `orkexa.Agent`), `agent_test`, `providers` (keys, endpoints, capabilities) |
| Tools | `tool_library`, `builtin_tools`, `tool_drafts`, `python_deps`, `python_envs`, `python_tools` |
| Knowledge, memory, files | `knowledge`, `memory_spaces`, `documents`, `input_files`, `file_formats`, `file_delivery`, `office`, `reports`, `adoption`, `sanitize`, `preview` |
| Assembly | `workspace` (`Workspace`: every service of one data directory), `storage` |
| Extensions | `extensions` (contract for integrations, entry point group `nodestro.integrations`) |
| Testing | `testing` (simulated provider clients and uv; never a real request) |

**Public API.** `workspace.Workspace`, the models in `schemas`, `problems` (`NodestroError`, `Problem`),
`extensions` and `testing` are the stable interface for Python code. The other modules are used by the
Nodestro API and studio and may change between versions.

## Installation

Requires Python 3.10 or newer (CI tests 3.10 and 3.13).

```bash
pip install "nodestro-runtime @ git+https://github.com/peterkarlhuber/nodestro-runtime.git@<commit>"
```

Orkexa is installed with it, pinned to commit `cbf6df3d6f727b96924f1ae66e60b469eeef58e6` (Orkexa has no
tags). Both repositories are private, so pip needs read access, for example through
`git config url."https://<token>@github.com/peterkarlhuber/".insteadOf "https://github.com/peterkarlhuber/"`.
The tested combination of all Nodestro components is recorded in `nodestro-studio/platform/versions.json`.

## Quickstart (no provider account needed)

This example runs completely offline: `ScriptedClient` from `nodestro_runtime.testing` answers in place of
the provider, so it costs nothing and always gives the same result.

```python
import asyncio
import tempfile

from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text
from nodestro_runtime.workspace import Workspace

# Each request is answered from the script whose key occurs in the agent's system prompt.
client = ScriptedClient({"OFFER": [text("Offer: 10 units, delivered Friday, 1,200 EUR.")]})

workflow = WorkflowIn.model_validate(
    {
        "name": "Offer",
        "nodes": [
            {"id": "start", "type": "start"},
            {
                "id": "writer",
                "type": "agent",
                "label": "Writer",
                "agent": {"name": "Writer", "system_prompt": "OFFER: write a short offer."},
            },
            {"id": "out", "type": "output", "label": "Result"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "writer"},
            {"id": "e2", "source": "writer", "target": "out"},
        ],
    }
)


async def main() -> None:
    data_dir = tempfile.mkdtemp()
    async with Workspace(data_dir, client_factory=lambda _provider: client, integrations=[]) as workspace:
        saved = workspace.save_workflow(workflow)
        run = await workspace.run(saved, RunIn(input="10 units by Friday"))
        done = await workspace.wait(run.id, timeout=60)
        print(done.status, done.output)  # succeeded Offer: 10 units, delivered Friday, 1,200 EUR.


asyncio.run(main())
```

To use a real model, leave out `client_factory` and provide the provider's key. A key from the environment
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`) wins over one stored in the settings
of the data directory (`workspace.update_settings(...)`). OpenAI-compatible endpoints, Ollama and vLLM are
configured with a base URL instead. Models are named `provider:model`, for example `openai:gpt-5.5`; a name
without a prefix is an Anthropic model. The same data directory (default `~/.nodestro`) can be opened by the
studio and the API.

## Using the runtime

**Lifecycle.** A `Workspace` owns one data directory; open one per process and directory. The workspace holds
a lock on `<data_dir>/nodestro.lock` until it is closed: a second workspace on the same directory, in this or
another process, is refused with `workspace.in_use` (it would mark the first one's runs as interrupted and run
its schedules a second time). Runs execute as
tasks of the asyncio event loop that started them, so start runs from async code. `async with Workspace(...)`
(or `await workspace.aclose()`) cancels the runs of this process, waits until each one has stored its final
state and closes the database. `close()` does the same without waiting. Paused runs are not affected: they
wait in the database for their approval and continue after the next start. Runs that were executing when the
process stopped are marked as interrupted (`run.interrupted`) when the data directory is opened again and can
be continued.

**Running.** `await workspace.run(workflow, RunIn(...))` checks the workflow first (the same pre-check the
editor shows, `nodestro_runtime.preflight`), refuses a run that would fail for a known reason and returns the
queued run. `await workspace.wait(run_id)` returns it once it finished or paused. `workspace.cancel(run_id)`
stops a running or paused run.

**Approvals.** An approval node pauses the run (`status == "paused"`, the node is `awaiting_approval`) until
a person decides with `workspace.decide(run_id, node_id, "approved" | "rejected", comment)`. Nothing is ever
approved automatically, a decision is recorded once, and a pending approval survives restarts. `decide`,
`cancel` and starting a run may also be called from another thread while the event loop runs; they are
carried out on that loop.

**Continuing.** `await workspace.resume(run_id)` continues a failed or cancelled run as a new linked run. A
step's earlier result is reused only when its fingerprint (configuration, connections, tool code, inputs and
files, sub-workflow version) is unchanged; reused steps cost nothing again. Steps that run again after they
had called own tools need `confirm=True`.

**Errors.** Expected failures raise `nodestro_runtime.problems.NodestroError` (a `ValueError`) with a stable
`code`, `params` and an English message, for example `api_key_missing`, `workflow.blocked` or
`approval.not_pending`. Failures during a run are recorded on the run and its nodes (`error`, `error_code`,
`error_params`) instead of being raised. Unknown ids raise `KeyError`.

**Events.** `workspace.runs.subscribe(run_id)` returns the events so far and a queue of the next ones (`None`
ends the stream). Event numbers (`seq`) increase by one without gaps for the whole life of a run in a process.
Text and thinking deltas beyond 5,000 events are published but not kept for late subscribers, and a
subscriber that falls 10,000 events behind is ended (it reads the run again).

**Tools.** Built-in tools run in the process. Custom and AI-generated tools are checked statically and run in
a sandbox (a separate Python process with an empty environment, time and memory limits, or Docker). The
process sandbox limits what a tool can do by accident; it is not a proven security boundary against hostile
code, and the Docker sandbox isolates more strongly. Third-party Python packages are installed only after a person approved
the exact versions of every package (`python_envs`), into a separate environment per workflow or tool.

## Security and data protection

- Everything is stored locally in `<data_dir>/nodestro.db` (SQLite, WAL mode) and below the data directory.
  Prompts, inputs and files that agents receive are sent to the configured model provider.
- API keys are never returned by display methods and are given only to agents of their own provider. Error
  texts stored with runs and the logs of package installations are cleaned of anything that looks like a
  credential.
- `uv` (package resolution and installation) runs with an allowlist of environment variables: system paths,
  locale, temporary directories, proxy and certificate settings, `UV_*` and compiler settings. Provider keys,
  tokens and other secrets are not passed on. Resolving a package that has no wheel builds it from source,
  which runs that package's build script before the approval overview is shown. Nodestro therefore resolves
  the libraries of generated tools with `--no-build` (such a package is reported as `deps.build_required`);
  only a plan a person asks for (*Review and approve*) may build from source. uv is not a sandbox: a build
  script runs with the rights of the Nodestro process.
- Workflows can contain approval nodes for human oversight; every decision is stored with the run, as are
  the prompts, tool calls, results and costs of every step, and each run records the workflow version it ran.
- Sanitized exports (`sanitize`) replace credentials and personal data by placeholders.

These are technical measures. They do not by themselves make an application compliant with the GDPR, the EU
AI Act or ISO/IEC 42001; that depends on how an operator uses and documents the system.

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

The tests never call a paid provider, need no API key and no network. `tests/test_architecture.py` checks that the runtime
imports neither the API nor the studio and uses nothing of claude-agent; `tests/test_readme.py` runs the
quickstart above.

CI needs the repository secret `NODESTRO_READ` (read access to Orkexa and the Nodestro repositories).

## Provenance and license

Extracted from `nodestro` 0.1 (15e2076); see [PROVENANCE.md](PROVENANCE.md) and [CHANGELOG.md](CHANGELOG.md).
MIT, see [LICENSE](LICENSE).
