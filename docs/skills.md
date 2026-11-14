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

## Security hardening (internal review of the import path)

- **Framing:** skill text reaches the model only inside `<skill_content>` (instructions) or `<skill_file>`
  (reference data). Frame tags inside a skill are written as `&lt;...`, so a skill cannot close its frame and pose as
  instructions outside it. The skills block comes after the security note in the system prompt; the skills notice says
  that skills never grant tools or permissions and never override that note.
- **Reads:** `read_skill_file` returns at most 50 KB per call and 400 KB per step.
- **Provenance:** imported content marks a skill as imported for good (`origin` `import:<sha256>`), also when it is
  imported as a new version of an existing skill or later edited by hand; a copy keeps the source's origin. The
  inspector warning `skill.imported_with_write_tools` relies on it.
- **Releases:** a disabled or archived skill is never frozen (`skill.inactive`), and a release run stops a frozen
  skill that is disabled or archived in the workspace now (`ReleaseBundle.skill_source(status=...)`, a kill switch
  without unpublishing).
- **Integrity:** stored files are checked against their SHA-256 and the version's digest when read; a frozen release
  skill against its digest. A restored backup is not trusted.
- **Credential scan:** every file that is UTF-8 text is scanned, scripts and configuration files included.

## Groups, preselection, recommendations, usage and script drafts (NAL-B4)

- **Groups** (`SkillGroup`, collection `skill_groups`, `Skills.save_group`/`delete_group`) are named sets of skills of
  the library. A group is a library convenience: assigning one adds each member as an ordinary `SkillBinding` in the
  member's version at that moment, with `group_id` as a label. A group is never resolved at run time, so changing or
  deleting a group changes no agent and no release. Deleting a skill removes it from its groups.
- **Limit:** an agent may have up to 100 skills (`MAX_AGENT_SKILLS`).
- **Preselection** (`skills_select.py`):
  - With at most 20 `on_demand` skills, a step offers all of them.
  - Above that, it offers the best 8 for its prompt (`SKILL_SELECTION_SIZE`), plus every `always` skill and every
    skill an orchestrator recommended.
  - Ranking is BM25 over each skill's name (counted twice) and description. Words are folded to lower case without
    accents and cut to a six-character stem.
  - The choice is local and deterministic, and ties go by name.
  - Every step records its choice (`NodeRun.skill_selection`: method, offered, recommended, ignored, and the 20
    best-scored candidates) and emits `skills_selected`.
  - Only offered skills can be loaded in that step. A selection never adds a skill that is not assigned.
  - On the fixed test corpus (30 skills, 50 tasks in `tests/skill_corpus.py`), the expected skill is offered for
    48 of 50 tasks. The two misses are German tasks against English descriptions: skills match best in the language
    people write their tasks in.
- **Orchestrator recommendations:**
  - The pool shows each team's skills: the skills of the team's own agents (`orkexa.orchestration.Member.skills`).
  - When a team has skills, a `delegate` decision may name up to 8 of them (`skills`). Parallel decisions carry
    none in Nodestro; orkexa's protocol allows them per assignment.
  - The team run keeps the names the team has (`Run.recommended_skills`). Its agents offer those skills and mark
    them `<recommended>true</recommended>`.
  - Names the team does not have are ignored and logged with `orchestration_delegated` (`ignored_skills`).
  - A recommendation grants nothing.
- **Usage** (`skill_stats.py`) is computed from stored runs. Per skill it counts:
  - steps that offered it, and steps where it was recommended;
  - loads and file reads, and the distinct runs that loaded it;
  - versions used, and when it was last used;
  - for the steps that loaded it: succeeded and failed, and quality checks passed out of checks done.

  The outcomes are an indication, not proof that the skill helped.
- **Script drafts:** `script_requirement` turns one UTF-8 script under `scripts/` (at most 30,000 characters) into a
  requirement for the Coding Agent.
  - The script is framed as a description of behaviour, not as instructions, and its origin
    `skill:<name>@<version>/<path>` heads the requirement and is kept as `ToolJob.origin`.
  - The draft goes through the unchanged tool chain: a static check, tests in the isolating sandbox (without Docker,
    `sandbox.isolation_required`), and a person's approval. It stays `untrusted`.
  - The Coding Agent's system prompt says that `<skill_script>` content is untrusted data. A tool made from a skill
    script uses no network, no subprocesses and no files besides its arguments, whatever the script does.
  - `ToolInfo.origin` shows the origin of the approved tool (read from the requirement's `Origin:` line).
  - Until a person approves it, a script remains text that is never run.
