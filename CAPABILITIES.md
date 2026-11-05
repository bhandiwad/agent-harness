# Author and load modular capabilities

The operator authors `AGENTS.md`, `SKILL.md` and supporting files using their editor or development tools. The harness loads reviewed versions. It does not let a running model change its own installed instructions, permissions or tool catalog. Session workspace file editing remains a separate capability.

## Project instructions

```python
from pandita_agent.agent.sdk import pin_instructions
pin = pin_instructions('/deployment/project', directory='operations/assets')
# Put pin in Agent(instruction_tree=pin, ...).
```

The loader reads `AGENTS.md` at the project root and every ancestor down to the selected directory. Runtime rules and configured agent instructions take precedence; within project guidance, the more specific directory takes precedence. The selected directory is explicit configuration, not inferred from a user upload. The digest includes absent ancestor instruction files, so adding a file also invalidates the pin. Files are UTF-8, bounded to 32 KiB each and 64 KiB combined, with a maximum 16-directory scope. Symlinks are rejected.

## Skills

A package contains `SKILL.md` plus optional `references/`, `assets/` and `scripts/`. Minimal `SKILL.md`:

```markdown
---
name: asset-analysis
description: Compare asset records with maintenance instructions.
---
Use datafusion_search for source facts. Distinguish observations from assumptions.
Read references/guide.md when interpreting maintenance intervals.
```

```python
from pandita_agent.agent.sdk import pin_skill
skill = pin_skill('/deployment/skills/asset-analysis', version='v1',
                  required_tools=('datafusion_search',), permission='capability:assets')
# Put skill in Agent(skills=(skill,), tools=('datafusion_search',), ...).
```

The default `skill_mode="on_demand"` initially sends only names, descriptions, versions and required tool names. The model calls `harness_select_skills` with the complete desired list and a reason; this replaces its selection. An empty list unloads skills. The default maximum is four active skills, configurable up to sixteen. The loader then supplies their full instructions. Selection is saved per agent execution frame, survives follow-up turns and restarts, and is recorded in the event trace. Specialist frames have their own selection. `skill_mode="eager"` explicitly loads all permitted configured skills for legacy behavior.

`harness_read_skill_resource` reads one UTF-8 resource from an already-selected skill, at most 16 KiB. It rejects traversal, symlinks and `scripts/` resources. The loader never executes scripts. Register reviewed execution as a tool if needed. All package files contribute to the digest; the package limit is 1 MiB and 128 files.

`required_tools` must be explicitly enabled on the agent. The user must have each tool's permission, plus the skill's optional permission. Skills unavailable to that user are omitted from the catalog. Revoking access to a selected skill stops further use. Markdown and `allowed-tools` metadata never grant authority. These are bounded supported formats, not a claim of complete compatibility with every external skill specification.

## Publish a new configuration

Use `write_config(settings, '/deployment/new-agent.json')` to validate and save a new file. It refuses to overwrite an existing file and does not install it or grant permissions. Review the changes, mount packages read-only, then point API and worker at the same reviewed configuration. Re-pin deliberately after editing package content. Changed files are rejected before model use and before tool dispatch, including after an approval wait.

`GET /v1/agents/{id}/capabilities` exposes permitted descriptors and tool schemas without file paths, full skill bodies or credentials. `sdk.capabilities(id)` wraps it.

Version 0.3 introduces new fields in the configuration digest. Finish or reconcile pending work using its original 0.2 deployment before upgrading; start new conversations under 0.3. The runtime never silently migrates a pending approval to changed configuration. No database migration is required for this increment.
