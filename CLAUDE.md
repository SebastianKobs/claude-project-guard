# project-guard

A Claude Code `PreToolUse` hook that keeps tool calls inside the project: a boundary check, agent scopes and output
redirects. [README.md](README.md) describes the behaviour; the module docstring of `guard.py` is the reference.

## Working rules
- **TDD, always:** write the failing test first, run it and see it fail for the right reason, then write the code.
  - A test that passes before the change proves nothing; fix the test.
  - If code came first anyway, break it on purpose and check that the new tests fail, before trusting them.
- **Dependencies:** Python ≥ 3.12 and the standard library. PyYAML is the only exception:
  - the hook imports it only when a config file exists;
  - the tests import it directly.
- **Tests:** `python3 -m unittest discover -s tests`, from this folder. All of them pass before a change is done.
- **Never touch real data in tests:**
  - Each test runs the hook as a subprocess in a throwaway project under `tests/.tmp/` (see `tests/helpers.py`).
  - Pass environment variables (`CLAUDE_PROJECT_DIR`, `WSL_DISTRO_NAME`, `PATH`) explicitly.
  - Never read or write the real `~/.claude` or the host project.
- **The hook must never crash:** every policy catches its own errors.
  - Agent scopes fail closed (deny).
  - The boundary and the redirects fail to a prompt.
  - A crash would block or break the user's tool call.
- **Keep the docs in step:** a change of behaviour updates, in the same change:
  - the module docstring;
  - the comment in `guard.yml`;
  - `DEFAULTS` in `guard.py`, which must equal `guard.yml` (a test checks this);
  - `README.md`, if users notice the change.

## Style
- **Layout:** PEP 8, max 120 columns, one import per line (stdlib, then third party, then local).
- **Naming:** descriptive names, no one-letter names except loop indices.
- **Statements and expressions:**
  - no one-line compound statements, no `;`, no assigned lambdas, no backslash continuations
  - f-strings only; regex flags spelled out (`re.IGNORECASE`)
- **Types and records:** type hints everywhere in `guard.py`, `@dataclass(frozen=True)` for records.
- **Paths are strings (`os.path`), not `pathlib`:**
  - Most paths come raw from shell commands and tool arguments (`~`, `$HOME`, globs, Windows paths).
  - The checks are prefix checks on the resolved strings.
  - Files are opened with `encoding="utf-8"`.
- **Docs:** a docstring on every function and class. Comments explain why, not what.
- **Tests:**
  - stdlib `unittest`, one behaviour per test method, no type hints
  - Test classes, `setUp` and `__init__` need no docstring; shared base cases and helpers do.
- **Editing files by script:** when a script or heredoc mentions a redirected program with an output option (e.g.
  `chrome.exe --screenshot=...`), write it to a file and run the file. Passed inline, the guard would rewrite its
  own source text.

## Layout
```
guard.py                    the hook: config loading, the three policies, combine and reply
guard.yml                   the defaults, documented (loaded first; the project's .claude/guard.yml and
                            .claude/guard.local.yml override it)
tests/helpers.py            Project (a throwaway project) and ProjectTestCase
tests/fixture-*.yml         the fixture config copied into each throwaway project
tests/test_guard_agents.py    agent scopes, the guard's own files, fail closed
tests/test_guard_outside.py   project boundary, Windows paths, config files and order
tests/test_guard_redirect.py  output redirects for tools and Bash programs
tmp/                        redirected output (gitignored; the one place below .claude/hooks subagents may write)
```

## Design facts the code relies on
- **Paths:**
  - The project is `$CLAUDE_PROJECT_DIR`, else three folders above `guard.py`.
  - `{guard}` in `redirect_outputs.target` is the folder of `guard.py`.
- **Order of the answer:** an agent-scope deny, then a boundary prompt, then an agent-scope or redirect allow, then
  silence.
  - Policies 1 and 2 see the call after the redirect.
- **Claude Code's hook contract:**
  - The hook's answer carries the rewritten input (`updatedInput`, the full tool input), a warning for the user
    (`systemMessage`) and one for the model (`additionalContext`).
  - Claude Code reads the hook list at session start, so a moved or renamed `guard.py` needs a restart.
- **Redirect target:** checked only when something is redirected. So a guard installed outside the project fails
  only those calls.
- **Windows paths:** they count as outside, except this distro's `\\wsl.localhost\<distro>\...` share, which maps to
  the Linux path. A program on `/mnt/<letter>/` gets its redirected output as such a share path.
- **Bash programs:**
  - Only the program a command runs counts: the first word, also after an operator or a wrapper like `timeout 60`.
  - A listed program is matched by its location and file name. Symlinks are not followed, and a bare name is looked
    up in `PATH`.
