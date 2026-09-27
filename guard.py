#!/usr/bin/env python3
"""PreToolUse guard for the main session and every subagent, registered once in .claude/settings.json. It applies
three policies to each tool call and combines their answers, most restrictive first:

1. Agent scopes, for subagents (events with an agent_type; the main session is left alone):
   - Built in, for every subagent: no writing the files that enforce this guard (PROTECTED: the Claude Code
     settings, .claude/hooks/, this file, the guard config), neither with the edit tools nor with a Bash command
     that names one of them (which also blocks `cat`; Read still works). Not configurable, so a subagent can't
     lift it by editing the config. For Bash this is a heuristic: paths hidden in globs, variables or inline code
     get through. The one exception is tmp/ next to this file, where redirected output goes (policy 3).
   - Agents listed in `hooks.agent_scopes`:
     - Bash only for one of their `commands`, exactly, with `{paths}` replaced by one or more paths below their
       `write` paths and no shell operators. Without `commands`, no Bash at all.
     - Write and the edit tools only on or below their `write` paths.
   - Every other subagent: Write and the edit tools only on or below `hooks.subagent_write` (default: the project
     directory). Their Bash is left to the project boundary.
   Everything else these tools try is denied with a reason. Other tools (Read, Grep, MCP tools) are left alone.
   `{name.key}` in a value refers to another config value: a section of the guard config, or else
   config/<name>.yml (+ <name>.local.yml) of the project, loaded only when a scope refers to it.
2. Project boundary, for everyone: a permission prompt ("ask") for any path outside the project directory or in
   `shared.never_access`, except the allowed paths in the `hooks` section (session directories, extras, exact
   paths). Bash commands themselves are allowed (sed, python3, /usr/bin/..., ...); only explicit references to
   outside data paths (~, $HOME, /etc, /var, /tmp, other repos, ..) in a command prompt. This is a heuristic, not a
   sandbox.
3. Output redirects, for everyone: an output file argument of a tool in `hooks.redirect_outputs.tools` (default:
   `filename` of the Playwright MCP tools) that points outside the project (a Windows path such as C:/Users/..., an
   absolute, home or parent path outside the boundary) is rewritten to <target>/<server>.<tool>/<file name> (target
   default: {guard}/tmp, the tmp/ folder next to this file). The call is then allowed with the rewritten input, and
   a warning tells the user (systemMessage) and the model (additionalContext) where the file went, so a prompt
   approved by mistake still writes nothing outside. Relative names are left alone: the tool puts them in its own
   output folder.
   The same for Bash commands that run a program listed in `programs.locations` (location pattern -> program file
   names; default: Chrome, Edge and Chromium in /mnt/?/, /Applications/ and /usr/bin/): an outside value of one of
   its output options becomes <target>/<program>/<file name>. The options are `programs.options` (--screenshot=...,
   --print-to-pdf=...), or the program's own for an entry `{name: [options]}`, which replace them. A program on a
   Windows drive (/mnt/<letter>/) gets it as a \\\\wsl.localhost\\<distro>\\... path, since it can't write into WSL
   otherwise (which is why it reaches for C:\\Users\\Public); outside WSL ($WSL_DISTRO_NAME unset) it isn't rewritten,
   and the boundary prompts for the Windows path. Only the program a command runs counts (the first word, also after
   `timeout 60`, `nohup`, ...), not a name in a script's text; a bare name is looked up in PATH. A listed program in
   its location doesn't prompt itself, like /usr/bin; every other path of the command still does.

Combined: the policies 1 and 2 see the call after the redirect. An agent-scope deny wins, then a boundary prompt (with
the rewritten input), then the agent-scope allow of a configured command (so a configured command that names an
outside path still prompts), then the allow of a redirect; otherwise the hook stays silent. A broken
`hooks.redirect_outputs` prompts.

Config: guard.yml next to this file (the defaults, documented), overridden by the project's .claude/guard.yml and
then the personal .claude/guard.local.yml; all are optional. Values there replace the DEFAULTS below: nested mappings
are merged key by key, lists are replaced, and an empty key keeps the default. The project directory is
$CLAUDE_PROJECT_DIR, or else the directory three levels above this file (.claude/hooks/project-guard/guard.py).

The two policies fail differently, and each part catches its own errors, so a broken agent scope never touches the
main session. Agent scopes fail closed: a config that can't be loaded (unreadable, invalid YAML, PyYAML missing) or
a broken entry or reference mean "deny" for every subagent's Bash and edit calls. The boundary fails to a prompt: an
error while checking asks with the reason. Input that isn't a JSON object is denied, since it can't tell whose call
it is. The hook never blocks a tool call by crashing.

Windows paths (C:\\..., C:/..., \\\\server\\...) count as outside, except the WSL share of this distro
(\\\\wsl.localhost\\<$WSL_DISTRO_NAME>\\... or \\\\wsl$\\...), which maps to the Linux path. Paths are resolved with
realpath, so symlinks out of the project or out of the write paths are caught. They are
handled as strings (os.path), not pathlib: most of them come raw from shell commands and tool arguments (~, $HOME,
globs), and the checks are prefix checks on the resolved strings.
"""
import fnmatch
import json
import os
import re
import shlex
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

GUARD_DIR = os.path.dirname(os.path.realpath(__file__))           # .claude/hooks/project-guard
PROJECT = os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.path.join(GUARD_DIR, "..", "..", ".."))
HOME = os.path.expanduser("~")
# Claude Code names its per-project directories after the project path with
# every non-alphanumeric character replaced by "-" (/home/x/repo -> -home-x-repo).
SLUG = re.sub(r"[^A-Za-z0-9]", "-", PROJECT)

# relative to the project: the guard's documented defaults, then the project's settings, then personal ones
CONFIG_FILES = (os.path.relpath(os.path.join(GUARD_DIR, "guard.yml"), PROJECT), ".claude/guard.yml",
                ".claude/guard.local.yml")
CONFIG_NAME = CONFIG_FILES[0]                                       # where the reasons point for the settings
GUARD_PLACEHOLDER = "{guard}"                                       # in redirect_outputs.target: GUARD_DIR
# Redirected output: subagents may write here although it is below .claude/hooks.
GUARD_TMP = os.path.join(GUARD_DIR, "tmp")
REFERENCED_CONFIG_DIR = "config"                                    # {name.key} -> config/<name>.yml
DEFAULTS: dict[str, Any] = {
    "shared": {
        "never_access": [],
    },
    "hooks": {
        "session_paths": ["~/.claude/projects/{slug}", "/tmp/claude-{uid}/{slug}"],
        "extra_allowed_paths": [],
        "allowed_exact": ["/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty"],
        "bash_system_prefixes": ["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/opt", "/snap",
                                 "/dev"],
        "subagent_write": ["."],
        "agent_scopes": {},
        "redirect_outputs": {
            "target": "{guard}/tmp",
            "tools": {"mcp__*playwright*__*": ["filename"]},
            "programs": {
                "options": ["--screenshot", "--print-to-pdf"],
                "locations": {
                    "/mnt/?/": ["chrome.exe", "msedge.exe"],
                    "/Applications/": ["Google Chrome", "Microsoft Edge", "Chromium"],
                    "/usr/bin/": ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                                  "microsoft-edge"],
                },
            },
        },
    },
}

# Project boundary
# Tool argument names that hold file system paths (Read, Write, Glob, Grep, MCP tools, …).
PATH_KEYS = frozenset({"file_path", "path", "paths", "file_paths", "notebook_path",
                       "repo_path", "root", "out_dir", "cwd", "directory", "dir"})
SHELL_OPERATOR = re.compile(r"[;&|()<>]+")
REDIRECTION_PREFIX = re.compile(r"^(\d*[<>]+|&>)")     # 2>/x, >>/x, <x, &>/x
GLOB_WILDCARD = re.compile(r"[*?\[{]")
WINDOWS_DRIVE_MOUNT = re.compile(r"^/mnt/[a-z]/", re.IGNORECASE)
# words that run the next word as the command (timeout 60 chrome.exe), and their arguments: options, durations and
# VAR=value
COMMAND_WRAPPERS = frozenset({"command", "env", "exec", "nice", "nohup", "time", "timeout"})
WRAPPER_ARGUMENT = re.compile(r"^(?:-.*|[\d.]+[smhd]?|\w+=.*)$")
MAX_LISTED_PATHS = 5                                   # how many offending paths the prompt names

# Agent scopes
# The files that enforce this guard, relative to the project; no subagent may write them (see protected_paths()).
PROTECTED = (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks", *CONFIG_FILES)
USER_SETTINGS = ("~/.claude/settings.json", "~/.claude/settings.local.json")
EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
EDIT_PATH_KEYS = ("file_path", "notebook_path", "path")
SHELL_SPECIAL = set(";&|<>`$\n\\(){}*?[]~!#")      # anything a shell would do more with than pass a word
ENTRY_KEYS = ("write", "commands")
# {name.key}: at least one dot, so the {paths} placeholder in a command is not taken for a reference
REFERENCE = re.compile(r"\{(\w[\w-]*(?:\.\w[\w-]*)+)\}")
PATHS_PLACEHOLDER = "{paths}"                      # in a command: one or more paths below the write paths
NO_MATCH = "not a configured command"

# Output redirects
WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")    # C:/x, C:\x, \\server\share
PATH_SEPARATORS = re.compile(r"[\\/]")
WSL_SHARE = re.compile(r"^\\\\wsl(?:\.localhost|\$)\\(?P<distro>[^\\]+)(?P<path>\\.*)?$", re.IGNORECASE)
# the value of --option=value in a command: double quoted, single quoted or a bare word
OPTION_VALUE = r"=(?P<value>\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s;&|<>'\"]+)"

Config = dict[str, Any]
Hit = tuple[str, str]                                  # (path as written, resolved path)


class Denied(Exception):
    """The tool call is not allowed; the message is the reason shown to the agent."""


@dataclass(frozen=True)
class Redirect:
    """A tool input with its outside output paths rewritten, and one note per rewritten path (from -> to)."""
    tool_input: dict[str, Any]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    """The hook's answer: a permission decision ("deny", "ask" or "allow"), its reason and any redirect."""
    decision: str
    reason: str
    redirect: Redirect | None = None


# --- shared ------------------------------------------------------------------------------------------------------

def merge(base: Config, override: Config) -> Config:
    """base updated with override: mappings merged recursively, lists and texts replaced, empty (None) keys skipped."""
    merged = dict(base)
    for key, value in override.items():
        if value is None:
            continue                                   # `key:` without a value keeps the default
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_files(names: tuple[str, ...]) -> tuple[Config, str | None]:
    """(config, error) for the project-relative YAML files `names`, merged in order; missing files are skipped."""
    config: Config = {}
    error = None
    for name in names:
        path = os.path.join(PROJECT, name)
        if not os.path.exists(path):
            continue
        try:
            import yaml
        except ImportError:
            return config, f"PyYAML not installed, can't read {name}"
        try:
            with open(path, encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as exc:
            error = f"{name}: {exc}"
            continue
        if data is None:
            continue
        if not isinstance(data, dict):
            error = f"{name}: not a mapping"
            continue
        config = merge(config, data)
    return config, error


def load_config() -> tuple[Config, str | None]:
    """Return (config, error): DEFAULTS overridden by the CONFIG_FILES, in order."""
    try:
        config, error = load_files(CONFIG_FILES)
    except Exception as exc:                           # deliberately broad: the hook must never crash
        return DEFAULTS, f"{type(exc).__name__}: {exc}"
    return merge(DEFAULTS, config), error


def section(config: Config, name: str) -> Config:
    """config[name] if it is a mapping, else {}."""
    value = config.get(name)
    if isinstance(value, dict):
        return value
    return {}


def is_under(path: str, base: str) -> bool:
    return path == base or path.startswith(base + os.sep)


def expand_home(raw: str) -> str:
    """raw with $HOME, ${HOME} and ~ filled in."""
    path = raw.replace("${HOME}", HOME).replace("$HOME", HOME)
    return os.path.expanduser(path)


def real_path(raw: str, cwd: str) -> str:
    """Absolute, symlink-free form of a path as written in a tool call, relative ones taken from cwd."""
    return os.path.realpath(os.path.join(cwd, expand_home(raw)))


def windows_to_wsl(raw: str) -> str | None:
    """The Linux path of a path on this distro's WSL share, else None (a drive or other UNC path)."""
    share = WSL_SHARE.match(raw)
    distro = os.environ.get("WSL_DISTRO_NAME")
    if share is None or not distro or share.group("distro").lower() != distro.lower():
        return None
    return (share.group("path") or "\\").replace("\\", "/")


def display_path(path: str) -> str:
    """A resolved path relative to the project if it is inside, else absolute."""
    if is_under(path, PROJECT):
        return os.path.relpath(path, PROJECT)
    return path


def tool_input_of(event: dict[str, Any]) -> dict[str, Any]:
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        return tool_input
    return {}


def cwd_of(event: dict[str, Any]) -> str:
    return event.get("cwd") or os.getcwd()


# --- project boundary --------------------------------------------------------------------------------------------

def configured_paths(config: Config, section_name: str, key: str, expand: bool = True) -> list[str]:
    """A list of path strings from the config; with expand, ~, {uid} and {slug} are filled in."""
    values = section(config, section_name).get(key) or []
    if not isinstance(values, list):
        raise ValueError(f"{section_name}.{key} in {CONFIG_NAME} must be a list")
    paths = []
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if expand:
            value = value.replace("{uid}", str(os.getuid())).replace("{slug}", SLUG)
            value = os.path.expanduser(value)
        paths.append(value)
    return paths


# (location pattern, {program file name: its own output options, or None for programs.options})
ProgramLocations = tuple[tuple[str, dict[str, tuple[str, ...] | None]], ...]


def program_entry(entry: Any) -> tuple[str, tuple[str, ...] | None] | None:
    """(name, own options) of a programs.locations entry: `name` or `{name: [options]}`; None if it is broken."""
    if isinstance(entry, str) and entry:
        return entry, None
    if not isinstance(entry, dict) or len(entry) != 1:
        return None
    name, options = next(iter(entry.items()))
    is_text_list = isinstance(options, list) and all(isinstance(option, str) and option for option in options)
    if not isinstance(name, str) or not name or not is_text_list:
        return None
    return name, tuple(options)


def program_locations(config: Config) -> ProgramLocations:
    """hooks.redirect_outputs.programs.locations; broken entries are skipped here, redirect_rules() reports them."""
    redirects = section(config, "hooks").get("redirect_outputs")
    programs = redirects.get("programs") if isinstance(redirects, dict) else None
    locations = programs.get("locations") if isinstance(programs, dict) else None
    if not isinstance(locations, dict):
        return ()
    parsed = []
    for location, entries in locations.items():
        if not isinstance(entries, list):
            continue
        valid_entries = [program_entry(entry) for entry in entries]
        parsed.append((str(location), dict(entry for entry in valid_entries if entry is not None)))
    return tuple(parsed)


def own_program_options(path: str, locations: ProgramLocations) -> tuple[str, ...] | None:
    """The own output options of the listed program at path, or None if it uses programs.options."""
    for location, names in locations:
        if fnmatch.fnmatchcase(path, location.rstrip("/") + "/*") and os.path.basename(path) in names:
            return names[os.path.basename(path)]
    return None


def is_listed_program(path: str, locations: ProgramLocations) -> bool:
    """True if path is one of the listed programs, directly or further below its location."""
    return any(fnmatch.fnmatchcase(path, location.rstrip("/") + "/*") and os.path.basename(path) in names
               for location, names in locations)


def program_path(token: str, cwd: str, locations: ProgramLocations) -> str | None:
    """The path of the program a command word runs: as written (not following symlinks, since /usr/bin/google-chrome
    points into /opt), or a listed bare name looked up in PATH; None for other bare names."""
    if "/" in token:
        return os.path.normpath(os.path.join(cwd, expand_home(token)))
    if not any(token in names for _, names in locations):
        return None
    found = shutil.which(token)
    if found is None:
        return None
    return os.path.normpath(os.path.abspath(found))


@dataclass(frozen=True)
class Boundary:
    """Where tools may go without a prompt: the project and the allowed paths, minus never_access."""
    never_access: tuple[str, ...]
    allowed_prefixes: tuple[str, ...]
    allowed_exact: frozenset[str]
    bash_system_prefixes: tuple[str, ...]
    program_locations: ProgramLocations
    config_error: str | None

    @classmethod
    def from_config(cls, config: Config, config_error: str | None) -> "Boundary":
        never_access = [os.path.join(PROJECT, path.strip("/"))
                        for path in configured_paths(config, "shared", "never_access", expand=False)]
        # Session directories plus configured extras; everything else outside PROJECT prompts.
        allowed_prefixes = [os.path.realpath(path)
                            for path in configured_paths(config, "hooks", "session_paths")
                            + configured_paths(config, "hooks", "extra_allowed_paths")]
        return cls(never_access=tuple(never_access),
                   allowed_prefixes=tuple(allowed_prefixes),
                   allowed_exact=frozenset(configured_paths(config, "hooks", "allowed_exact")),
                   bash_system_prefixes=tuple(configured_paths(config, "hooks", "bash_system_prefixes")),
                   program_locations=program_locations(config),
                   config_error=config_error)

    def is_never_access(self, path: str) -> bool:
        return any(is_under(path, blocked) for blocked in self.never_access)

    def is_system_path(self, path: str) -> bool:
        """Program and library locations that Bash commands may name without a prompt."""
        return any(is_under(path, prefix) for prefix in self.bash_system_prefixes)

    def is_allowed(self, path: str) -> bool:
        """True for resolved paths inside the project or an allowed location, and not never_access."""
        if path in self.allowed_exact:
            return True
        if self.is_never_access(path):
            return False
        return any(is_under(path, base) for base in [PROJECT, *self.allowed_prefixes])

    def resolve(self, raw: str, cwd: str) -> str:
        """real_path(), but never_access paths stay as written, so they prompt even where the symlink is missing."""
        if WINDOWS_PATH.match(raw):
            linux_path = windows_to_wsl(raw)
            if linux_path is None:
                return raw                             # outside: a Windows drive or another machine
            return os.path.realpath(linux_path)
        normalized = os.path.normpath(os.path.join(cwd, expand_home(raw)))
        if self.is_never_access(normalized):
            return normalized
        return real_path(raw, cwd)


def path_arguments(value: Any, key: str | None = None) -> Iterator[str]:
    """Every non-URL string stored under a PATH_KEYS key, at any depth of the tool input."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from path_arguments(child, child_key)
    elif isinstance(value, list):
        for child in value:
            yield from path_arguments(child, key)
    elif isinstance(value, str) and value and key in PATH_KEYS and "://" not in value:
        yield value


def from_input(tool: str, tool_input: dict[str, Any], cwd: str, boundary: Boundary) -> Iterator[Hit]:
    """Paths from structured tool arguments."""
    for raw in path_arguments(tool_input):
        yield raw, boundary.resolve(raw, cwd)
    pattern = tool_input.get("pattern")
    if tool == "Glob" and isinstance(pattern, str):
        if pattern.startswith(("/", "~")) or ".." in pattern:
            # check the fixed part of the pattern before the first wildcard
            fixed_part = GLOB_WILDCARD.split(pattern, maxsplit=1)[0] or "/"
            yield pattern, boundary.resolve(fixed_part, cwd)


def tokenize(command: str) -> list[str]:
    """Shell words and operators; falls back to whitespace splitting for unbalanced quotes."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


def is_cd_target(token: str, previous: str | None) -> bool:
    """The directory argument of `cd DIR` / `pushd DIR` (not an option, operator or unknown variable)."""
    if previous not in ("cd", "pushd"):
        return False
    if token.startswith("-") or SHELL_OPERATOR.fullmatch(token):
        return False
    return "$" not in token.replace("$HOME", "")


def strip_redirection(token: str) -> str:
    """The path part of redirections and --option=value tokens: 2>/x, >>/x, <x, --file=/x."""
    token = REDIRECTION_PREFIX.sub("", token)
    if token.startswith("-") and "=" in token:
        token = token.split("=", 1)[1]
    return token


def token_parts(token: str) -> list[str]:
    """Split colon-separated lists (PATH=/a:/b) into their parts, but leave URLs whole."""
    if ":" in token and "://" not in token:
        return token.split(":")
    return [token]


def looks_like_path(part: str) -> bool:
    """Absolute, home-relative or parent-relative paths; plain relative words don't count."""
    return (part.startswith(("/", "~", "$HOME", "${HOME}"))
            or part == ".."
            or part.startswith("../")
            or "/../" in part)


def top_level_exists(part: str) -> bool:
    """Skips regex-like tokens such as /foo/p whose top-level directory doesn't exist."""
    top_level = "/" + part.lstrip("/").split("/", 1)[0]
    return os.path.exists(top_level)


def command_words(command: str) -> Iterator[tuple[str, bool]]:
    """(token, is the program a command runs) for every token: the first word, or the first after an operator or a
    wrapper such as `timeout 60`."""
    expect_command = True
    for token in tokenize(command):
        if SHELL_OPERATOR.fullmatch(token):
            expect_command = True
            yield token, False
        elif expect_command and (token in COMMAND_WRAPPERS or WRAPPER_ARGUMENT.match(token)):
            yield token, False
        else:
            yield token, expect_command
            expect_command = False


def from_bash(command: str, cwd: str, boundary: Boundary) -> Iterator[Hit]:
    """Paths that a shell command refers to explicitly; a listed program in its location that a command runs doesn't
    count."""
    previous = None
    for token, is_command in command_words(command):
        cd_target = is_cd_target(token, previous)
        previous = token
        if is_command:
            program = program_path(token, cwd, boundary.program_locations)
            if program is not None and is_listed_program(program, boundary.program_locations):
                continue
        if cd_target:
            # follow `cd DIR` so later relative paths resolve from there
            target = boundary.resolve(token, cwd)
            if not boundary.is_allowed(target) and not boundary.is_system_path(target):
                yield token, target
            cwd = target
            continue
        stripped = strip_redirection(token)
        if WINDOWS_PATH.match(stripped):
            yield stripped, boundary.resolve(stripped, cwd)   # before token_parts, which would split at C:
            continue
        for part in token_parts(stripped):
            if not part or "://" in part:
                continue
            if not looks_like_path(part):
                # relative paths only matter when they point into a never_access path
                if "$" not in part:
                    candidate = os.path.normpath(os.path.join(cwd, part))
                    if boundary.is_never_access(candidate):
                        yield part, candidate
                continue
            if part.startswith("/") and not top_level_exists(part):
                continue
            resolved = boundary.resolve(part, cwd)
            if not boundary.is_system_path(resolved):
                yield part, resolved


def outside_paths(event: dict[str, Any], boundary: Boundary) -> list[str]:
    """Sorted descriptions ("raw -> resolved") of every path in the tool call that isn't allowed."""
    tool = event.get("tool_name", "")
    tool_input = tool_input_of(event)
    cwd = cwd_of(event)

    hits: list[Hit] = []
    command = tool_input.get("command")
    if tool == "Bash" and isinstance(command, str):
        hits += from_bash(command, cwd, boundary)
    hits += from_input(tool, tool_input, cwd, boundary)
    # a subagent/session running with cwd outside the project is itself outside
    real_cwd = os.path.realpath(cwd)
    if tool in ("Glob", "Grep") and not boundary.is_allowed(real_cwd):
        hits.append((cwd, real_cwd))

    descriptions = set()
    for raw, resolved in hits:
        if not boundary.is_allowed(resolved):
            descriptions.add(resolved if raw == resolved else f"{raw} -> {resolved}")
    return sorted(descriptions)


def permission_reason(paths: list[str], boundary: Boundary) -> str:
    listed = "; ".join(paths[:MAX_LISTED_PATHS])
    if len(paths) > MAX_LISTED_PATHS:
        listed += " …"
    reason = (f"Access outside the project directory {PROJECT} or to a shared.never_access path "
              f"(see {CONFIG_NAME}): {listed}")
    if boundary.config_error:
        reason += f" [guard config not loaded: {boundary.config_error}]"
    return reason


def boundary_verdict(event: dict[str, Any], config: Config, config_error: str | None) -> Verdict | None:
    """"ask" for a call that touches a path outside the boundary, or when the check itself fails."""
    try:
        boundary = Boundary.from_config(config, config_error)
        paths = outside_paths(event, boundary)
    except Exception as exc:                           # deliberately broad: prompt instead of crashing
        return Verdict("ask", f"guard error while checking the project boundary, see {CONFIG_NAME} "
                              f"({type(exc).__name__}: {exc})")
    if not paths:
        return None
    return Verdict("ask", permission_reason(paths, boundary))


# --- agent scopes ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Scope:
    """What one agent may do: its write paths as configured and resolved, its commands, and where they are set."""
    agent: str
    write_names: tuple[str, ...]
    write_paths: tuple[str, ...]
    commands: tuple[str, ...]
    source: str                                        # the config key, named in the reasons


class ConfigValues:
    """The values {name.key} references point to: a section of the guard config, else config/<name>.yml."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.other_files: dict[str, Config] = {}

    def other_file(self, name: str) -> Config:
        """config/<name>.yml (+ <name>.local.yml), loaded once; raises Denied if it is missing or broken."""
        if name not in self.other_files:
            base = os.path.join(REFERENCED_CONFIG_DIR, name)
            config, error = load_files((f"{base}.yml", f"{base}.local.yml"))
            if error or not config:
                raise Denied(f"agent guard: {base}.yml not usable ({error or 'missing or empty'})")
            self.other_files[name] = config
        return self.other_files[name]

    def lookup(self, dotted: str) -> str:
        namespace, _, rest = dotted.partition(".")
        value: Any
        if namespace in self.config:
            value = self.config
            keys = dotted.split(".")
        else:
            value = self.other_file(namespace)
            keys = rest.split(".")
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                raise Denied(f"agent guard: unknown config reference {{{dotted}}}")
            value = value[key]
        if not isinstance(value, str) or not value:
            raise Denied(f"agent guard: config reference {{{dotted}}} is not a text")
        return value

    def expand(self, text: str) -> str:
        return REFERENCE.sub(lambda match: self.lookup(match.group(1)), text)


def repo_path(relative: str) -> str:
    """A repo-relative config path as a resolved absolute path."""
    return os.path.realpath(os.path.join(PROJECT, relative))


def is_in_scope(path: str, scope: Scope) -> bool:
    return any(is_under(path, base) for base in scope.write_paths)


def configured_list(entry: Config, key: str, where: str) -> list[str]:
    """entry[key] as a list of non-empty texts ([] if absent or empty); raises Denied for anything else."""
    values = entry.get(key)
    if values is None:
        return []
    is_text_list = isinstance(values, list) and all(isinstance(value, str) and value for value in values)
    if not is_text_list:
        raise Denied(f"agent guard: {where}.{key} in {CONFIG_NAME} must be a list of texts")
    return values


def make_scope(agent: str, write: list[str], commands: list[str], values: ConfigValues, source: str) -> Scope:
    """A Scope with its references expanded and its write paths resolved."""
    write_names = tuple(values.expand(path) for path in write)
    expanded_commands = tuple(values.expand(command) for command in commands)
    write_paths = tuple(repo_path(name) for name in write_names)
    return Scope(agent, write_names, write_paths, expanded_commands, source)


def listed_scope(agent: str, entry: Any, values: ConfigValues) -> Scope:
    """The agent's scope from its hooks.agent_scopes entry; raises Denied if the entry is broken."""
    where = f"hooks.agent_scopes.{agent}"
    if not isinstance(entry, dict):
        raise Denied(f"agent guard: {where} must be a mapping with {' and '.join(ENTRY_KEYS)}")
    unknown = sorted(str(key) for key in entry if key not in ENTRY_KEYS)
    if unknown:
        raise Denied(f"agent guard: {where} has unknown keys {', '.join(unknown)} (allowed: {', '.join(ENTRY_KEYS)})")
    return make_scope(agent, configured_list(entry, "write", where), configured_list(entry, "commands", where),
                      values, where)


def default_scope(agent: str, hooks: Config, values: ConfigValues) -> Scope:
    """The write scope of subagents not listed in hooks.agent_scopes: hooks.subagent_write, no commands."""
    return make_scope(agent, configured_list(hooks, "subagent_write", "hooks"), [], values, "hooks.subagent_write")


def protected_paths() -> list[str]:
    """PROTECTED and the user settings, resolved, plus this file wherever it is installed."""
    paths = [repo_path(relative) for relative in PROTECTED]
    paths += [os.path.realpath(os.path.expanduser(path)) for path in USER_SETTINGS]
    paths.append(os.path.realpath(__file__))
    return paths


def command_paths(command: str, cwd: str) -> list[str]:
    """Every word of a shell command resolved as a path, following `cd DIR`; words with variables are skipped."""
    paths = []
    previous = None
    for token in tokenize(command):
        cd_target = is_cd_target(token, previous)
        previous = token
        if cd_target:
            cwd = real_path(token, cwd)
            continue
        for part in token_parts(strip_redirection(token)):
            if part and "://" not in part and "$" not in part.replace("${HOME}", "").replace("$HOME", ""):
                paths.append(real_path(part, cwd))
    return paths


def check_protected(tool: str, tool_input: dict[str, Any], cwd: str, agent: str) -> None:
    """Raise Denied if a subagent's edit or Bash call names one of the guard's own files."""
    if tool == "Bash":
        targets = command_paths(str(tool_input.get("command", "")), cwd)
    else:
        targets = edit_targets(tool_input, cwd)
    protected = protected_paths()
    hits = sorted({display_path(target) for target in targets
                   if any(is_under(target, path) for path in protected) and not is_under(target, GUARD_TMP)})
    if not hits:
        return
    listed = ", ".join(hits[:MAX_LISTED_PATHS])
    if tool == "Bash":
        raise Denied(f"{agent}: subagents may not name the guard's own files in Bash commands ({listed}); "
                     f"read them with Read")
    raise Denied(f"{agent}: subagents may not write the guard's own files ({listed})")


def write_scope_text(scope: Scope) -> str:
    names = ["the project directory" if os.path.normpath(name) == "." else name for name in scope.write_names]
    return ", ".join(names) or "nothing"


def command_problem(words: list[str], template: str, scope: Scope, cwd: str) -> str | None:
    """Why the command's words don't match the configured command template, or None if they do."""
    template_words = shlex.split(template)
    if PATHS_PLACEHOLDER not in template_words:
        if words == template_words:
            return None
        return NO_MATCH
    placeholder = template_words.index(PATHS_PLACEHOLDER)
    prefix = template_words[:placeholder]
    suffix = template_words[placeholder + 1:]
    has_frame = words[:len(prefix)] == prefix and (not suffix or words[-len(suffix):] == suffix)
    paths = words[len(prefix):len(words) - len(suffix)]
    if not has_frame or not paths:
        return NO_MATCH
    for raw in paths:
        if raw.startswith("-") or not is_in_scope(real_path(raw, cwd), scope):
            return f"`{raw}` is not a path in {write_scope_text(scope)}"
    return None


def check_command(command: str, scope: Scope, cwd: str) -> None:
    """Raise Denied unless command is one of the agent's configured commands."""
    if not scope.commands:
        raise Denied(f"{scope.agent} runs no commands ({scope.source} in {CONFIG_NAME}); "
                     f"read with Read, Grep and the MCP tools")
    hint = f"{scope.agent} may only run " + " or ".join(f"`{template}`" for template in scope.commands)
    if any(PATHS_PLACEHOLDER in template for template in scope.commands):
        hint += f", with {PATHS_PLACEHOLDER} = one or more paths in {write_scope_text(scope)}"
    if set(command) & SHELL_SPECIAL:
        raise Denied(f"shell operators or special characters are not allowed; {hint}")
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise Denied(f"unparsable command ({exc}); {hint}") from exc
    problems = [command_problem(words, template, scope, cwd) for template in scope.commands]
    if None in problems:
        return
    specific = [problem for problem in problems if problem != NO_MATCH]
    if specific:
        raise Denied(f"{specific[0]}; {hint}")
    raise Denied(hint)


def edit_targets(tool_input: dict[str, Any], cwd: str) -> list[str]:
    """The resolved paths an edit tool would write."""
    return [real_path(tool_input[key], cwd) for key in EDIT_PATH_KEYS if isinstance(tool_input.get(key), str)]


def check_edit(tool_input: dict[str, Any], scope: Scope, cwd: str) -> None:
    """Raise Denied unless every path the edit tool writes is in the agent's write paths."""
    if scope.write_names:
        rule = f"{scope.agent} may write only in {write_scope_text(scope)} ({scope.source} in {CONFIG_NAME})"
    else:
        rule = f"{scope.agent} may not write files ({scope.source} in {CONFIG_NAME})"
    targets = edit_targets(tool_input, cwd)
    if not targets:
        raise Denied(f"{rule}; no file path given")
    for target in targets:
        if not is_in_scope(target, scope):
            raise Denied(f"{rule}, not {display_path(target)}")


def scope_decision(event: dict[str, Any], config: Config, config_error: str | None) -> str | None:
    """"allow" for a configured command, None to leave the call to the boundary check; raises Denied otherwise."""
    agent = event.get("agent_type")
    if not agent:
        return None                                    # the main session
    tool = event.get("tool_name")
    if tool != "Bash" and tool not in EDIT_TOOLS:
        return None
    check_protected(tool, tool_input_of(event), cwd_of(event), agent)
    if config_error:
        raise Denied(f"agent guard: can't load {CONFIG_NAME} ({config_error})")
    hooks = section(config, "hooks")
    scopes = hooks.get("agent_scopes") or {}
    if not isinstance(scopes, dict):
        raise Denied(f"agent guard: hooks.agent_scopes in {CONFIG_NAME} must map agent names to their scopes")
    values = ConfigValues(config)
    cwd = cwd_of(event)
    tool_input = tool_input_of(event)
    if agent not in scopes:
        if tool == "Bash":
            return None                                # unlisted agents: Bash is left to the boundary
        check_edit(tool_input, default_scope(agent, hooks, values), cwd)
        return None
    scope = listed_scope(agent, scopes[agent], values)
    if tool == "Bash":
        check_command(str(tool_input.get("command", "")), scope, cwd)
        return "allow"
    check_edit(tool_input, scope, cwd)
    return None


# --- output redirects --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RedirectRules:
    """hooks.redirect_outputs: the resolved target, {tool pattern: argument keys}, and the Bash programs' locations
    and output options."""
    target: str
    tools: dict[str, list[str]]
    program_locations: ProgramLocations
    program_options: tuple[str, ...]


def text_list(settings: Config, key: str, where: str) -> tuple[str, ...]:
    """settings[key] as a tuple of non-empty texts ([] if absent); raises ValueError for anything else."""
    values = settings.get(key) or []
    if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"{where}: {key} must be a list of texts")
    return tuple(values)


def redirect_rules(config: Config) -> RedirectRules:
    """The rules in hooks.redirect_outputs; raises ValueError if they are broken."""
    where = f"hooks.redirect_outputs in {CONFIG_NAME}"
    settings = section(config, "hooks").get("redirect_outputs")
    if not isinstance(settings, dict):
        raise ValueError(f"{where} must be a mapping with target and tools")
    target = settings.get("target")
    tools = settings.get("tools") or {}
    if not isinstance(target, str) or not target:
        raise ValueError(f"{where}: target must be a project-relative path")
    target_path = repo_path(target.replace(GUARD_PLACEHOLDER, GUARD_DIR))
    is_valid_mapping = isinstance(tools, dict) and all(
        isinstance(keys, list) and all(isinstance(key, str) and key for key in keys) for keys in tools.values())
    if not is_valid_mapping:
        raise ValueError(f"{where}: tools must map tool name patterns to lists of argument names")
    programs = settings.get("programs") or {}
    if not isinstance(programs, dict):
        raise ValueError(f"{where}: programs must be a mapping with options and locations")
    locations = programs.get("locations") or {}
    is_valid_locations = isinstance(locations, dict) and all(
        isinstance(entries, list) and all(program_entry(entry) is not None for entry in entries)
        for entries in locations.values())
    if not is_valid_locations:
        raise ValueError(f"{where}: programs.locations must map location patterns to lists of program file names, "
                         f"each a name or {{name: [options]}}")
    return RedirectRules(target_path, tools, program_locations(config),
                         text_list(programs, "options", f"{where}: programs"))


def make_target_directory(directory: str, rules: RedirectRules) -> None:
    """Create a folder below the target; raises ValueError if the target is outside the project. Checked only here,
    when something is redirected, so a guard installed outside the project ({guard}/tmp outside too) only fails the
    calls it would redirect."""
    if not is_under(rules.target, PROJECT):
        raise ValueError(f"hooks.redirect_outputs.target in {CONFIG_NAME}: {display_path(rules.target)} is outside "
                         f"the project")
    os.makedirs(directory, exist_ok=True)


def tool_label(tool: str) -> tuple[str, str]:
    """(folder name, short name) of a tool: mcp__<server>__<tool> -> (<server>.<tool>, <tool>)."""
    server, separator, name = tool.removeprefix("mcp__").partition("__")
    if tool.startswith("mcp__") and separator:
        return f"{server}.{name}", name
    return tool, tool


def points_outside(raw: str, cwd: str, boundary: Boundary) -> bool:
    """True for a Windows, absolute, home or parent path that resolves outside the boundary."""
    if not WINDOWS_PATH.match(raw) and not looks_like_path(raw):
        return False                                   # relative: the tool's own output folder
    return not boundary.is_allowed(boundary.resolve(raw, cwd))


def file_name_of(raw: str, fallback: str) -> str:
    """The last part of a Windows or Linux path, or fallback if there is none."""
    file_name = PATH_SEPARATORS.split(raw)[-1]
    if file_name in ("", ".", ".."):
        return fallback
    return file_name


def redirect_bash(command: str, cwd: str, rules: RedirectRules, boundary: Boundary) -> tuple[str, list[str]]:
    """(command, notes) with every outside value of an output option moved below the target, if the command runs a
    listed program in its location. The options are the program's own, else programs.options. A program on a Windows
    drive gets the target as a WSL share path, so without WSL, where there is none, its command stays unchanged."""
    programs = [program_path(word, cwd, rules.program_locations)
                for word, is_command in command_words(command) if is_command]
    program = next((path for path in programs
                    if path is not None and is_listed_program(path, rules.program_locations)), None)
    if program is None:
        return command, []
    options = own_program_options(program, rules.program_locations)
    if options is None:
        options = rules.program_options
    if not options:
        return command, []
    distro = os.environ.get("WSL_DISTRO_NAME")
    is_windows_program = bool(WINDOWS_DRIVE_MOUNT.match(program))
    if is_windows_program and not distro:
        return command, []
    directory = os.path.join(rules.target, os.path.splitext(os.path.basename(program))[0])
    option_value = re.compile(r"(?<![\w-])(?P<option>" + "|".join(re.escape(option) for option in options) + ")"
                              + OPTION_VALUE)
    notes = []

    def replace(match: re.Match[str]) -> str:
        """The option with its value moved below the target, or unchanged if the value is inside."""
        words = tokenize(match.group("value"))
        if not words or not points_outside(words[0], cwd, boundary):
            return match.group(0)
        file_name = file_name_of(words[0], match.group("option").lstrip("-")).replace("'", "")
        make_target_directory(directory, rules)
        linux_path = os.path.join(directory, file_name)
        notes.append(f"{words[0]} -> {display_path(linux_path)}")
        new_value = linux_path
        if is_windows_program:
            new_value = f"\\\\wsl.localhost\\{distro}" + linux_path.replace("/", "\\")
        return f"{match.group('option')}={shlex.quote(new_value)}"

    return option_value.sub(replace, command), notes


def redirect_outputs(event: dict[str, Any], config: Config) -> Redirect | None:
    """The event's tool input with every outside output path moved below the target, or None if nothing moves."""
    tool = str(event.get("tool_name", ""))
    tool_input = tool_input_of(event)
    rules = redirect_rules(config)
    boundary = Boundary.from_config(config, None)
    cwd = cwd_of(event)
    command = tool_input.get("command")
    if tool == "Bash" and isinstance(command, str):
        new_command, bash_notes = redirect_bash(command, cwd, rules, boundary)
        if not bash_notes:
            return None
        return Redirect(dict(tool_input, command=new_command), tuple(bash_notes))
    keys = [key for pattern, pattern_keys in rules.tools.items() if fnmatch.fnmatchcase(tool, pattern)
            for key in pattern_keys]
    folder, short_name = tool_label(tool)
    updated = dict(tool_input)
    notes = []
    for key in dict.fromkeys(keys):
        raw = tool_input.get(key)
        if not isinstance(raw, str) or not raw or not points_outside(raw, cwd, boundary):
            continue
        file_name = file_name_of(raw, short_name)
        directory = os.path.join(rules.target, folder)
        make_target_directory(directory, rules)
        updated[key] = os.path.join(directory, file_name)
        notes.append(f"{raw} -> {display_path(updated[key])}")
    if not notes:
        return None
    return Redirect(updated, tuple(notes))


def redirect_message(redirect: Redirect) -> str:
    """The warning for the user and the model: which paths moved where."""
    return (f"guard: redirected an output path outside the project into the project "
            f"(hooks.redirect_outputs in {CONFIG_NAME}): {'; '.join(redirect.notes)}")


# --- combined ----------------------------------------------------------------------------------------------------

def decide(event: dict[str, Any]) -> Verdict | None:
    """Policies 1 and 2 on the redirected call: see combine()."""
    config, config_error = load_config()
    try:
        redirect = redirect_outputs(event, config)
    except Exception as exc:                           # deliberately broad: prompt instead of crashing
        return Verdict("ask", f"guard error in hooks.redirect_outputs, see {CONFIG_NAME} "
                              f"({type(exc).__name__}: {exc})")
    if redirect is None:
        return check(event, config, config_error)
    verdict = check(dict(event, tool_input=redirect.tool_input), config, config_error)
    if verdict is None or verdict.decision == "allow":
        return Verdict("allow", redirect_message(redirect), redirect)
    if verdict.decision == "ask":
        return Verdict("ask", verdict.reason, redirect)
    return verdict


def check(event: dict[str, Any], config: Config, config_error: str | None) -> Verdict | None:
    """The agent-scope deny, else the boundary prompt, else the agent-scope allow, else None (stay silent)."""
    try:
        scope_result = scope_decision(event, config, config_error)
    except Denied as exc:
        return Verdict("deny", str(exc))
    except Exception as exc:                           # deliberately broad: fail closed, never crash
        return Verdict("deny", f"agent guard: {type(exc).__name__}: {exc}")
    boundary_result = boundary_verdict(event, config, config_error)
    if boundary_result is not None:
        return boundary_result
    if scope_result == "allow":
        return Verdict("allow", f"a configured command for this agent (hooks.agent_scopes in {CONFIG_NAME})")
    return None


def reply(verdict: Verdict) -> None:
    """Print the verdict as PreToolUse hook output; a redirect adds the new input and the warnings."""
    specific: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": verdict.decision,
        "permissionDecisionReason": verdict.reason,
    }
    output: dict[str, Any] = {"hookSpecificOutput": specific}
    if verdict.redirect is not None:
        message = redirect_message(verdict.redirect)
        specific["updatedInput"] = verdict.redirect.tool_input
        specific["additionalContext"] = f"{message}. Use the new path when you refer to the file."
        output["systemMessage"] = message
    print(json.dumps(output))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (OSError, ValueError) as exc:
        reply(Verdict("deny", f"guard: unreadable hook input ({exc})"))
        return 0
    if not isinstance(event, dict):
        reply(Verdict("deny", "guard: hook input is not a JSON object"))
        return 0
    verdict = decide(event)
    if verdict is not None:
        reply(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
