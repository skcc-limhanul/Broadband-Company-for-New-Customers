# Repository Guidelines

## Scope

These instructions apply to all work in this repository. More specific `AGENTS.md`
files in subdirectories, if present, may add or refine these rules for their scope.

## General rules

- Read the relevant code, documentation, and configuration before changing them.
- Keep changes focused on the user's request; do not reformat or modify unrelated files.
- Preserve existing user changes and do not overwrite, discard, or reset them without
  explicit permission.
- Do not use or refer to code or files under `./temporary_dont_use/`.
- Prefer existing project conventions, dependencies, scripts, and patterns over
  introducing new ones.
- Keep secrets, credentials, tokens, private data, and generated local artifacts out
  of the repository.
- Update documentation when behavior, setup, or usage changes.
- Add or update tests for behavior changes when the project provides a suitable test
  mechanism.
- Run the smallest relevant validation available after making changes, and report
  what was run and any limitations.
- Review the final diff for correctness, scope, accidental files, and sensitive data
  before handing off the work.
- write recap of your jobs steps to ./document/job/job.log

## Version control and delivery

- Whenever the user asks or commands work to be done, create a new task-specific
  branch before making changes.
- Do not commit, push, merge, or open a pull request unless the user explicitly asks.
- After finishing the requested work, ask the user to validate the changes and create
  a pull request to `main` after that validation, unless the user specifies another
  target or workflow.
- Avoid destructive commands such as hard resets, forced pushes, or broad deletion
  unless the user explicitly authorizes the exact action.

## Communication

- State assumptions when requirements are ambiguous and choose the least risky
  reasonable interpretation.
- Summarize files changed, validation performed, and any follow-up needed.
