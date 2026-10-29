# Changelog

## 0.2.0 (2026-10-29)

- Erste Version als eigenes Repository: aus `nodestro` 0.1.0 (Commit 15e2076, heute `nodestro-legacy`)
  herausgelöst, ohne neue Funktionen und ohne Verhaltensänderung. Herkunft jeder Datei: PROVENANCE.md.
- Abhängigkeiten auf exakte Commits gepinnt (orkexa `cbf6df3d6f727b96924f1ae66e60b469eeef58e6`); die getestete
  Kombination steht in `nodestro-studio/platform/versions.json`.
- CI mit dem Repository-Secret `NODESTRO_READ`.
- Neu: `Workspace` als Zusammenbau der Runtime für einen Datenordner, `extensions` (Vertrag 1 für Integrationen),
  `testing` (simulierte Anbieter-Clients). Die OCR ist nach `nodestro-integrations` umgezogen.
