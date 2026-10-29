# Herkunft

Quelle: `peterkarlhuber/nodestro` (heute `nodestro-legacy`) am Commit
`15e20768dea496820d4f8a286720176b47817da7` (Merge von PR #14, Version 0.1.0). Die Spalte „Stand“ vergleicht jede
Datei byte-genau mit dem Original; „angepasst“ nennt die geänderten Zeilen (meist umgeschriebene Importe).

13 Dateien unverändert, 25 angepasst, 13 neu.

| Datei | Original | Stand |
| --- | --- | --- |
| `.github/workflows/ci.yml` | — | neu |
| `.gitignore` | — | neu |
| `CHANGELOG.md` | — | neu |
| `LICENSE` | — | neu |
| `README.md` | — | neu |
| `pyproject.toml` | — | neu |
| `src/nodestro_runtime/__init__.py` | `backend/src/nodestro/__init__.py` | angepasst (+9 −3 Zeilen) |
| `src/nodestro_runtime/adoption.py` | `backend/src/nodestro/adoption.py` | angepasst (+11 −11 Zeilen) |
| `src/nodestro_runtime/agent_builder.py` | `backend/src/nodestro/agent_builder.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_runtime/agent_test.py` | `backend/src/nodestro/agent_test.py` | angepasst (+1 −1 Zeilen) |
| `src/nodestro_runtime/builtin_tools.py` | `backend/src/nodestro/builtin_tools.py` | unverändert |
| `src/nodestro_runtime/catalog.py` | `backend/src/nodestro/catalog.py` | unverändert |
| `src/nodestro_runtime/conditions.py` | `backend/src/nodestro/conditions.py` | angepasst (+1 −1 Zeilen) |
| `src/nodestro_runtime/documents.py` | `backend/src/nodestro/documents.py` | angepasst (+3 −87 Zeilen) |
| `src/nodestro_runtime/engine.py` | `backend/src/nodestro/engine.py` | angepasst (+8 −8 Zeilen) |
| `src/nodestro_runtime/extensions.py` | — | neu |
| `src/nodestro_runtime/file_delivery.py` | `backend/src/nodestro/file_delivery.py` | angepasst (+1 −1 Zeilen) |
| `src/nodestro_runtime/file_formats.py` | `backend/src/nodestro/file_formats.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_runtime/forms.py` | `backend/src/nodestro/forms.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_runtime/graph.py` | `backend/src/nodestro/graph.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_runtime/input_files.py` | `backend/src/nodestro/input_files.py` | angepasst (+4 −4 Zeilen) |
| `src/nodestro_runtime/knowledge.py` | `backend/src/nodestro/knowledge.py` | angepasst (+23 −7 Zeilen) |
| `src/nodestro_runtime/launcher.py` | `backend/src/nodestro/launcher.py` | angepasst (+7 −7 Zeilen) |
| `src/nodestro_runtime/memory_spaces.py` | `backend/src/nodestro/memory_spaces.py` | unverändert |
| `src/nodestro_runtime/office.py` | `backend/src/nodestro/office.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_runtime/preflight.py` | `backend/src/nodestro/preflight.py` | angepasst (+6 −6 Zeilen) |
| `src/nodestro_runtime/preview.py` | `backend/src/nodestro/preview.py` | angepasst (+5 −5 Zeilen) |
| `src/nodestro_runtime/problems.py` | `backend/src/nodestro/problems.py` | unverändert |
| `src/nodestro_runtime/providers.py` | `backend/src/nodestro/providers.py` | unverändert |
| `src/nodestro_runtime/py.typed` | — | neu |
| `src/nodestro_runtime/python_deps.py` | `backend/src/nodestro/python_deps.py` | unverändert |
| `src/nodestro_runtime/python_envs.py` | `backend/src/nodestro/python_envs.py` | unverändert |
| `src/nodestro_runtime/python_tools.py` | `backend/src/nodestro/python_tools.py` | unverändert |
| `src/nodestro_runtime/release.py` | `backend/src/nodestro/release.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_runtime/reports.py` | `backend/src/nodestro/reports.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_runtime/resume.py` | `backend/src/nodestro/resume.py` | angepasst (+22 −22 Zeilen) |
| `src/nodestro_runtime/run_views.py` | `backend/src/nodestro/run_views.py` | angepasst (+4 −4 Zeilen) |
| `src/nodestro_runtime/sanitize.py` | `backend/src/nodestro/sanitize.py` | unverändert |
| `src/nodestro_runtime/schemas.py` | `backend/src/nodestro/schemas.py` | angepasst (+10 −10 Zeilen) |
| `src/nodestro_runtime/storage.py` | `backend/src/nodestro/storage.py` | unverändert |
| `src/nodestro_runtime/testing.py` | — | neu |
| `src/nodestro_runtime/tool_drafts.py` | `backend/src/nodestro/tool_drafts.py` | unverändert |
| `src/nodestro_runtime/tool_library.py` | `backend/src/nodestro/tool_library.py` | angepasst (+6 −3 Zeilen) |
| `src/nodestro_runtime/traces.py` | `backend/src/nodestro/traces.py` | angepasst (+1 −1 Zeilen) |
| `src/nodestro_runtime/usage_log.py` | `backend/src/nodestro/usage_log.py` | unverändert |
| `src/nodestro_runtime/versions.py` | `backend/src/nodestro/versions.py` | unverändert |
| `src/nodestro_runtime/workspace.py` | — | neu |
| `tests/test_architecture.py` | — | neu |
| `tests/test_engine.py` | `backend/tests/test_engine.py` | angepasst (+2 −2 Zeilen) |
| `tests/test_sanitize.py` | — | neu |
| `tests/test_workspace.py` | — | neu |
