# project-guard

A `PreToolUse` hook for [Claude Code](https://code.claude.com) that keeps tool calls inside the project directory,
in the main session and in every subagent. A single Python file, configured with YAML.

## What you'll see

The guard gives one of three answers:

- **Ask:** Claude Code shows you a permission prompt.
- **Deny:** the call is refused without asking you. The agent gets the reason and can try another way.
- **Stay silent:** Claude Code's own permission rules decide, as if the guard weren't there.

| The tool call …                                                            | Main session | Subagent       |
| -------------------------------------------------------------------------- | ------------ | -------------- |
| stays inside the project                                                   | silent       | silent         |
| reads or lists outside the project (Read, Glob, Grep, MCP path arguments)  | **ask**      | **ask**        |
| names an outside path in a Bash command (`cat /etc/hosts`, `ls ../other`)  | **ask**      | **ask**        |
| writes a file outside the project (Write, Edit, MultiEdit, NotebookEdit)   | **ask**      | **deny**       |
| touches a `never_access` path, even inside the project                     | **ask**      | **ask**; a write through a symlink out of the project: **deny** |
| writes the guard's own files, or names them in Bash                        | silent       | **deny**       |
| saves output outside via a covered tool or program (see below)             | redirected   | redirected     |

"Outside" means outside the project directory and outside the allowed extras. Those extras are:

- Claude Code's own session folders (memory, scratchpad);
- exact paths like `/dev/null`;
- your `extra_allowed_paths`;
- for Bash, program locations like `/usr/bin`.

Windows paths such as `C:\Users\...` count as outside too.

### The permission prompt

- **What it names:** the outside paths the guard found, e.g. `Access outside the project directory …: /etc/hosts`.
  Read it before you approve: the tool call may do more than the part you were expecting.
- **What approving means:** approving lets this one call through as it stands. The guard remembers nothing, so the
  next outside call asks again.
- **Not every prompt is the guard's:** Claude Code prompts on its own for calls its permission rules don't allow.
  The guard's prompts start with its reason.
- **To stop being asked for a path:** add it to `extra_allowed_paths` (see [Configure](#configure)).

### Subagents are refused, not asked

Subagents often run while you aren't watching. So for them, the guard refuses writes instead of asking:

- **Writes:** the edit tools only write inside `subagent_write` (default: the project directory). Anything else is
  denied.
- **The guard's own files:** the Claude Code settings, `.claude/hooks/` and the guard config are denied, with the
  edit tools and in Bash commands. That way a subagent can't switch the guard off. Reading them with Read still
  works. The one exception is the guard's `tmp/`, where redirected output goes.
- **Stricter scopes:** a subagent listed in `agent_scopes` writes only in its own `write` paths. It runs only its
  own `commands`, exactly; with none listed, it gets no Bash at all.
- **Everything else stays a prompt:** a subagent's reads and Bash commands that name outside paths still ask you,
  as in the main session.

The main session is never denied by the guard, only asked.

### Output redirects

A covered tool or program may be about to save a file outside the project. Then the guard rewrites that path to
its own `tmp/` folder before the call runs, and allows the call:

- **Warnings:** you get a message naming both paths, and Claude is told the new path.
- **Why:** a prompt you approve by mistake still writes nothing outside.
- **Other outside paths:** if the same call names other outside paths, you are still asked. The prompt shows the
  rewritten call.

Covered out of the box:

- **Playwright MCP:** the `filename` argument (screenshots, PDFs, snapshots).
- **Headless Chrome, Edge and Chromium from Bash:** `--screenshot=` and `--print-to-pdf=`.
  - It works for the Windows browsers under WSL (as a `\\wsl.localhost\...` path), on macOS (`/Applications/`) and
    on Linux (`/usr/bin/`).
  - The browser itself then runs without a prompt. Its other outside arguments still ask.

This is a guard against mistakes, not a sandbox. Paths hidden in variables, globs or inline code get through.

## Requirements

- Python 3.12 or later.
- [PyYAML](https://pypi.org/project/PyYAML/) (`pip install pyyaml` or your distribution's `python3-yaml`), since
  the shipped `guard.yml` is a config file.
  - Without it, the main session falls back to the built-in defaults, and its prompts say PyYAML is missing.
  - Subagents' Bash and edit calls are denied instead, because agent scopes fail closed.
- Linux, macOS or WSL. The Windows parts only apply under WSL.

## Install

Put this folder at `.claude/hooks/project-guard/` in your project, for example as a git submodule:

```sh
git submodule add https://github.com/SebastianKobs/claude-project-guard.git .claude/hooks/project-guard
```

Register it in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Read|Write|Edit|MultiEdit|NotebookEdit|Glob|Grep|mcp__.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/project-guard/guard.py\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Then restart Claude Code: it reads hooks when a session starts.

## Configure

[`guard.yml`](guard.yml) holds the defaults, and a comment explains every key. Don't edit it for one project;
override it instead. Later files win:

| File                       | For                                                         |
| -------------------------- | ----------------------------------------------------------- |
| `guard.yml` (this folder)  | the defaults, shipped with the guard                        |
| `.claude/guard.yml`        | the project's settings, committed                           |
| `.claude/guard.local.yml`  | your personal settings; add it to the project's .gitignore  |

Mappings merge key by key, lists replace the default, and a key without a value keeps the default.

A few examples for the project's `.claude/guard.yml`:

```yaml
shared:
  never_access: [deploy/prod-link, secrets]   # always prompt, even inside the project

hooks:
  extra_allowed_paths: [~/shared-notes]       # outside the project, but fine

  agent_scopes:                               # a subagent that only writes and runs its tests
    test-writer:
      write: [tests]
      commands: ["python3 -m unittest {paths}"]

  redirect_outputs:
    programs:
      locations:
        "/opt/tools/": [renderer, exporter: [--out]]   # exporter redirects --out only
```

## When the guard itself goes wrong

The guard never blocks a call by crashing:

- A broken agent scope denies that subagent's Bash and edit calls.
- A broken boundary or redirect setting prompts, and names the problem.
- Input that isn't a JSON object is denied.

## Folder layout

```
.claude/      registers the guard for work on this repo itself
guard.py      the hook
guard.yml     its defaults, documented
tests/        unittest suites; each test builds a throwaway project under tests/.tmp/
tmp/          redirected output (gitignored)
```

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests run the hook as a subprocess in throwaway projects and never touch your real `~/.claude`. One test checks
the hook's registration in the host project's `.claude/settings.json`. It is skipped when the guard isn't installed
in a project.
