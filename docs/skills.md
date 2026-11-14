# Skills (NAL-B3)

A skill is a package in the open [Agent Skills](https://agentskills.io/specification) format: a `SKILL.md` with
`name` and `description` in its front matter and Markdown instructions, plus optional `references/`, `assets/` and
`scripts/`. Reading and checking packages is Orkexa's (`orkexa.skills`, Orkexa ADR 0018); storing, assigning and
loading them is this repository's.

## Library (`skills.py`)

| Collection | Holds |
|---|---|
| `skills` | `Skill`: the latest state (name, description, status `active`/`disabled`/`archived`, version, language, origin) |
| `skill_versions` | `SkillVersion`: one immutable version per changed save (front matter, file list with SHA-256, digest, note, origin, author) |
| `skill_blobs` | file contents by SHA-256, shared by all versions that contain the same bytes |

- Names are unique in a workspace. A save with unchanged files makes no version.
- `check()` adds a `skill.secret_suspected` warning to Orkexa's findings for every text file that seems to contain a
  credential. Errors refuse a save (`skill.invalid`, with the first finding's code as `reason`).
- Deleting removes the versions and the blobs no other version uses; the caller first makes sure no agent uses the
  skill, or archives it instead.

## Assignment and run time

`AgentConfig.skills` lists at most 20 `SkillBinding(skill_id, version, mode)`. A binding names a fixed version; a newer
version is shown in the inspector (`skill.newer_version`) and never used automatically.

| Mode | In the system prompt | Tools |
|---|---|---|
| `on_demand` (default) | name and description in `<available_skills>` | `load_skill(name)` gives the instructions (`<skill_content>`) and the file list |
| `always` | also the instructions | none needed to load it |

`read_skill_file(name, path)` returns one text file of an assigned version (at most 200,000 bytes; binary files are
refused). Both tools are the run's own: they only read the fixed versions, so the per-call approval gate does not ask
about them. Each use is recorded on the step (`NodeRun.skills`) and as a `skill_loaded` event. A second
`load_skill` of the same skill in one step returns a short note instead of the instructions again.

The prompt states that skills are the workflow author's instructions and never grant tools, connections or
permissions. Permissions keep coming only from `tools`, `tool_policies` and the approval rules; `allowed-tools` is
shown and ignored. Scripts are stored and shown, never run.

Inspector findings (`AgentBuilder.validate`, field `skills`): `skill.not_found`, `skill.inactive`,
`skill.duplicate_name`, `skill.newer_version` (info), `skill.always_large` (over about 20,000 tokens of `always`
instructions), `skill.imported_with_write_tools` (an imported skill next to a tool that may change data).

## Releases

A release freezes every skill version its agents use (`ReleaseBundle.skills`, `SkillSnapshot` with all files and the
digest). A release run resolves skills only from its bundle (`ReleaseBundle.skill_source()`), so a later change of a
skill never changes a published version. Bundles without skills, and agents without `skills`, `tool_settings`,
`task` or `origin`, keep the digest they were published with.
