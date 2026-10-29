# Nodestro Runtime

Die Workflow-Engine von [Nodestro](https://github.com/peterkarlhuber/nodestro-studio) als eigenständige
Python-Bibliothek, ohne Server und ohne Oberfläche. Sie führt Multi-Agent-Workflows aus (Agenten, Entscheidungen,
Freigaben, Unterworkflows, Ergebnisse), baut die Agenten mit [orkexa](https://github.com/peterkarlhuber/orkexa) und
speichert alles in einer SQLite-Datenbank im Datenordner.

Teil der Nodestro-Plattform; den Überblick über alle Repositories gibt
[PLATTFORM.md](https://github.com/peterkarlhuber/nodestro-studio/blob/master/docs/PLATTFORM.md).

## Inhalt

| Bereich | Module |
| --- | --- |
| Workflow-Modell | `schemas`, `graph`, `conditions`, `forms`, `catalog`, `versions`, `release` |
| Ausführung | `engine` (DAG, parallele Zweige, Live-Events, Budget, Abbruch), `launcher`, `preflight`, `resume`, `run_views`, `usage_log`, `traces` |
| Agenten | `agent_builder` (`AgentConfig` → `orkexa.Agent`), `agent_test`, `providers` (Schlüssel, Endpunkte, Fähigkeiten) |
| Tools | `tool_library`, `builtin_tools`, `tool_drafts`, `python_deps`, `python_envs`, `python_tools` |
| Wissen, Memory, Dateien | `knowledge`, `memory_spaces`, `documents`, `input_files`, `file_formats`, `file_delivery`, `office`, `reports`, `adoption`, `sanitize`, `preview` |
| Zusammenbau | `workspace` (`Workspace`: alles für einen Datenordner), `storage` |
| Erweiterungen | `extensions` (Vertrag für Integrationen, Einstiegspunkt `nodestro.integrations`) |
| Tests | `testing` (simulierte Anbieter-Clients, nie ein echter Aufruf) |

## Installation

```bash
pip install "nodestro-runtime @ git+https://github.com/peterkarlhuber/nodestro-runtime.git@<commit>"
```

Orkexa wird mitinstalliert, gepinnt auf `cbf6df3d6f727b96924f1ae66e60b469eeef58e6`. Beide Repositories sind privat;
pip braucht Lesezugriff (z. B. über `git config url."https://<token>@github.com/peterkarlhuber/".insteadOf`).

## Benutzung

```python
import asyncio
from pathlib import Path

from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.workspace import Workspace

workspace = Workspace(Path("~/.nodestro").expanduser())
workflow = workspace.save_workflow(WorkflowIn.model_validate({
    "name": "Angebot",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "writer", "type": "agent", "label": "Autor",
         "agent": {"name": "Autor", "system_prompt": "Schreibe ein kurzes Angebot."}},
        {"id": "out", "type": "output"},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "writer"},
              {"id": "e2", "source": "writer", "target": "out"}],
}))

async def main() -> None:
    run = await workspace.run(workflow, RunIn(input="10 Stück bis Freitag"))
    done = await workspace.wait(run.id, timeout=300)
    print(done.status, done.output)

asyncio.run(main())
```

Der Schlüssel des Anbieters kommt aus der Umgebung (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`) oder aus
den Einstellungen des Datenordners. Derselbe Datenordner lässt sich mit dem Studio und der API öffnen.

## Entwicklung

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

Die Tests rufen nie einen kostenpflichtigen Anbieter auf. Ein ausdrücklich markierter Test installiert ein echtes
Paket von PyPI mit uv (`NODESTRO_NETWORK_TESTS=1`). `tests/test_architecture.py` prüft, dass die Runtime weder API
noch Studio importiert und nichts von claude-agent nutzt.

Die CI braucht das Repository-Secret `NODESTRO_READ` (Lesezugriff auf orkexa und die nodestro-Repositories).

## Herkunft und Lizenz

Aus `nodestro` 0.1 (15e2076) herausgelöst, siehe [PROVENANCE.md](PROVENANCE.md) und [CHANGELOG.md](CHANGELOG.md).
MIT, siehe [LICENSE](LICENSE).
