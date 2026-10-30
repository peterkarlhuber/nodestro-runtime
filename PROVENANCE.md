# Provenance

Source: `peterkarlhuber/nodestro` (now `nodestro-legacy`) at commit
`15e20768dea496820d4f8a286720176b47817da7` (merge of PR #14, version 0.1.0). The column "State" compares each
file byte by byte with the original at the time of the extraction (version 0.2.0); "adapted" names the changed
lines (mostly rewritten imports). Changes after 0.2.0 are recorded in the Git history and in CHANGELOG.md.

13 files unchanged, 25 adapted, 13 new.

| File | Original | State |
| --- | --- | --- |
| `.github/workflows/ci.yml` | — | new |
| `.gitignore` | — | new |
| `CHANGELOG.md` | — | new |
| `LICENSE` | — | new |
| `README.md` | — | new |
| `pyproject.toml` | — | new |
| `src/nodestro_runtime/__init__.py` | `backend/src/nodestro/__init__.py` | adapted (+9 −3 lines) |
| `src/nodestro_runtime/adoption.py` | `backend/src/nodestro/adoption.py` | adapted (+11 −11 lines) |
| `src/nodestro_runtime/agent_builder.py` | `backend/src/nodestro/agent_builder.py` | adapted (+2 −2 lines) |
| `src/nodestro_runtime/agent_test.py` | `backend/src/nodestro/agent_test.py` | adapted (+1 −1 lines) |
| `src/nodestro_runtime/builtin_tools.py` | `backend/src/nodestro/builtin_tools.py` | unchanged |
| `src/nodestro_runtime/catalog.py` | `backend/src/nodestro/catalog.py` | unchanged |
| `src/nodestro_runtime/conditions.py` | `backend/src/nodestro/conditions.py` | adapted (+1 −1 lines) |
| `src/nodestro_runtime/documents.py` | `backend/src/nodestro/documents.py` | adapted (+3 −87 lines) |
| `src/nodestro_runtime/engine.py` | `backend/src/nodestro/engine.py` | adapted (+8 −8 lines) |
| `src/nodestro_runtime/extensions.py` | — | new |
| `src/nodestro_runtime/file_delivery.py` | `backend/src/nodestro/file_delivery.py` | adapted (+1 −1 lines) |
| `src/nodestro_runtime/file_formats.py` | `backend/src/nodestro/file_formats.py` | adapted (+2 −2 lines) |
| `src/nodestro_runtime/forms.py` | `backend/src/nodestro/forms.py` | adapted (+2 −2 lines) |
| `src/nodestro_runtime/graph.py` | `backend/src/nodestro/graph.py` | adapted (+2 −2 lines) |
| `src/nodestro_runtime/input_files.py` | `backend/src/nodestro/input_files.py` | adapted (+4 −4 lines) |
| `src/nodestro_runtime/knowledge.py` | `backend/src/nodestro/knowledge.py` | adapted (+23 −7 lines) |
| `src/nodestro_runtime/launcher.py` | `backend/src/nodestro/launcher.py` | adapted (+7 −7 lines) |
| `src/nodestro_runtime/memory_spaces.py` | `backend/src/nodestro/memory_spaces.py` | unchanged |
| `src/nodestro_runtime/office.py` | `backend/src/nodestro/office.py` | adapted (+2 −2 lines) |
| `src/nodestro_runtime/preflight.py` | `backend/src/nodestro/preflight.py` | adapted (+6 −6 lines) |
| `src/nodestro_runtime/preview.py` | `backend/src/nodestro/preview.py` | adapted (+5 −5 lines) |
| `src/nodestro_runtime/problems.py` | `backend/src/nodestro/problems.py` | unchanged |
| `src/nodestro_runtime/providers.py` | `backend/src/nodestro/providers.py` | unchanged |
| `src/nodestro_runtime/py.typed` | — | new |
| `src/nodestro_runtime/python_deps.py` | `backend/src/nodestro/python_deps.py` | unchanged |
| `src/nodestro_runtime/python_envs.py` | `backend/src/nodestro/python_envs.py` | unchanged |
| `src/nodestro_runtime/python_tools.py` | `backend/src/nodestro/python_tools.py` | unchanged |
| `src/nodestro_runtime/release.py` | `backend/src/nodestro/release.py` | adapted (+2 −2 lines) |
| `src/nodestro_runtime/reports.py` | `backend/src/nodestro/reports.py` | adapted (+2 −2 lines) |
| `src/nodestro_runtime/resume.py` | `backend/src/nodestro/resume.py` | adapted (+22 −22 lines) |
| `src/nodestro_runtime/run_views.py` | `backend/src/nodestro/run_views.py` | adapted (+4 −4 lines) |
| `src/nodestro_runtime/sanitize.py` | `backend/src/nodestro/sanitize.py` | unchanged |
| `src/nodestro_runtime/schemas.py` | `backend/src/nodestro/schemas.py` | adapted (+10 −10 lines) |
| `src/nodestro_runtime/storage.py` | `backend/src/nodestro/storage.py` | unchanged |
| `src/nodestro_runtime/testing.py` | — | new |
| `src/nodestro_runtime/tool_drafts.py` | `backend/src/nodestro/tool_drafts.py` | unchanged |
| `src/nodestro_runtime/tool_library.py` | `backend/src/nodestro/tool_library.py` | adapted (+6 −3 lines) |
| `src/nodestro_runtime/traces.py` | `backend/src/nodestro/traces.py` | adapted (+1 −1 lines) |
| `src/nodestro_runtime/usage_log.py` | `backend/src/nodestro/usage_log.py` | unchanged |
| `src/nodestro_runtime/versions.py` | `backend/src/nodestro/versions.py` | unchanged |
| `src/nodestro_runtime/workspace.py` | — | new |
| `tests/test_architecture.py` | — | new |
| `tests/test_engine.py` | `backend/tests/test_engine.py` | adapted (+2 −2 lines) |
| `tests/test_sanitize.py` | — | new |
| `tests/test_workspace.py` | — | new |
