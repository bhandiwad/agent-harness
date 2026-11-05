---
name: maintenance-planning
description: Plan pump inspections using crew capacity, setup time, exclusions and a maintenance window; revise saved plans when constraints change.
---
Read references/planning-rules.md with harness_read_skill_resource before planning.
Read the user's asset file from the private workspace. Ask for missing crew size with harness_ask_user before calculating or creating a plan. A clarification answer resumes this task.
Use harness_calculate for arithmetic. Never silently change the crew, scope, duration or window. State clearly when all selected pumps cannot fit, rather than silently omitting any.
Before completing each planning request, save BOTH artifacts in this order: (1) plan.md, then (2) schedule.json. These are two separate harness_write_file calls. Then call harness_list_files to confirm BOTH files exist. Do not finish after writing only one artifact. For updates, read each existing file to obtain its current digest and pass that exact digest to harness_write_file. Use null only for a new file.
The JSON artifact must contain exactly these fields:
- included_pumps: list of selected pump IDs
- excluded_pumps: list of excluded pump IDs
- technicians: integer
- inspection_minutes_per_pump: integer
- setup_minutes: integer
- window_minutes: integer
- total_inspection_person_minutes: integer
- required_elapsed_minutes: integer
- fits_window: boolean
- source: string naming the asset file and skill reference
The Markdown artifact explains the constraints, calculations, feasibility and proposed sequence in plain English. Briefly summarize the saved result and mention any unresolved constraint. Do not include credentials or claim publication.
