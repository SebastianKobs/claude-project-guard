"""guard.py, project boundary: tool calls that touch a path outside the repo or a never_access path prompt, in the
main session and every subagent."""
import json
import os
import re
import runpy
import sys
import unittest

import yaml

from helpers import GUARD_DIR
from helpers import HOOK
from helpers import HOST_PROJECT
from helpers import ProjectTestCase

NEVER = "src/prod-link"                      # never_access in the fixture config


class HookCase(ProjectTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.project.path("src"))
        os.symlink("/usr", self.project.path(NEVER))    # stands in for a symlink to a production system
        self.slug = re.sub(r"[^A-Za-z0-9]", "-", self.project.root)
        self.scratch = f"/tmp/claude-{os.getuid()}/{self.slug}/scratchpad/x.txt"
        self.memory = f"~/.claude/projects/{self.slug}/memory/MEMORY.md"

    def run_hook(self, stdin):
        return self.project.run([sys.executable, self.project.path(HOOK)], input=stdin)

    def hook(self, tool, **tool_input):
        """Run the fixture's hook; return the permission reason, or None when the call is allowed."""
        event = {"tool_name": tool, "tool_input": tool_input, "cwd": self.project.root}
        result = self.run_hook(json.dumps(event))
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout:
            return None
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")
        return output["permissionDecisionReason"]


class GuardHookTest(HookCase):
    def test_repo_file_allowed(self):
        self.assertIsNone(self.hook("Read", file_path=".claude/guard.yml"))
        self.assertIsNone(self.hook("Read", file_path=self.project.path("src/index.php")))

    def test_never_access_prompts(self):
        reason = self.hook("Read", file_path=f"{NEVER}/index.php")
        self.assertIn(f"{NEVER}/index.php", reason)
        self.assertNotIn("config not loaded", reason)

    def test_never_access_relative_in_bash_prompts(self):
        self.assertIsNotNone(self.hook("Bash", command=f"cat {NEVER}/index.php"))

    def test_session_paths_allowed(self):
        self.assertIsNone(self.hook("Write", file_path=self.scratch))
        self.assertIsNone(self.hook("Read", file_path=self.memory))

    def test_other_projects_session_paths_prompt(self):
        self.assertIsNotNone(self.hook("Read", file_path="~/.claude/projects/-other-repo/memory/MEMORY.md"))

    def test_bash_system_programs_allowed(self):
        self.assertIsNone(self.hook("Bash", command="/usr/bin/env python3 x.py 2>/dev/null"))

    def test_bash_outside_data_paths_prompt(self):
        reason = self.hook("Bash", command="cat /etc/passwd ~/.bashrc")
        self.assertIn("/etc/passwd", reason)
        self.assertIn(".bashrc", reason)

    def test_bash_parent_dir_prompts(self):
        self.assertIsNotNone(self.hook("Bash", command="ls ../"))

    def test_bash_cd_outside_then_relative_path_prompts(self):
        self.assertIsNotNone(self.hook("Bash", command="cd /etc && cat hosts"))

    def test_bash_windows_paths_prompt(self):
        for argument in ['"C:\\Users\\Public\\x.png"', "C:/Users/Public/x.png", '--out="D:\\x.pdf"',
                         "'\\\\server\\share\\x.png'"]:
            with self.subTest(argument=argument):
                self.assertIsNotNone(self.hook("Bash", command=f"tool.exe {argument}"))

    def test_bash_windows_path_names_the_path(self):
        self.assertIn("C:\\Users\\Public\\x.png", self.hook("Bash", command='tool.exe "C:\\Users\\Public\\x.png"'))

    def test_bash_wsl_share_path_into_the_project_allowed(self):
        self.assertEqual(self.share_path_decision("Ubuntu-Test", "tmp/x.png"), "")

    def test_bash_wsl_share_path_of_another_distro_prompts(self):
        self.assertIn('"ask"', self.share_path_decision("Other-Distro", "tmp/x.png"))

    def share_path_decision(self, distro, relative):
        """The hook's output for a Bash command naming `relative` as a path on the WSL share of `distro`, run in the
        distro Ubuntu-Test."""
        windows = f"\\\\wsl.localhost\\{distro}" + self.project.path(relative).replace("/", "\\")
        event = {"tool_name": "Bash", "tool_input": {"command": f"tool.exe '{windows}'"}, "cwd": self.project.root}
        result = self.project.run([sys.executable, self.project.path(HOOK)], input=json.dumps(event),
                                  env={"WSL_DISTRO_NAME": "Ubuntu-Test"})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_windows_path_argument_of_a_tool_prompts(self):
        self.assertIsNotNone(self.hook("Read", file_path="C:\\Users\\Public\\x.png"))

    def test_allowed_exact_only_for_exact_paths(self):
        self.assertIsNone(self.hook("Read", file_path="/dev/null"))
        self.assertIsNotNone(self.hook("Read", file_path="/dev/sda"))

    def test_mcp_path_argument_outside_prompts(self):
        self.assertIsNotNone(self.hook("mcp__codebase-memory-mcp__index_repository", repo_path="/var/www"))

    def test_glob_pattern_outside_prompts(self):
        self.assertIsNotNone(self.hook("Glob", pattern="/etc/**/*.conf"))

    def test_local_extra_path_keeps_session_paths(self):
        self.project.write(".claude/guard.local.yml", "hooks:\n  extra_allowed_paths: [/opt/shared]\n")
        self.assertIsNone(self.hook("Read", file_path="/opt/shared/x.md"))
        self.assertIsNone(self.hook("Write", file_path=self.scratch))

    def test_tool_input_that_is_no_mapping_is_ignored(self):
        result = self.run_hook('{"tool_name": "Read", "tool_input": []}')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_unreadable_input_is_denied(self):
        for stdin in ["not json", "[]"]:
            with self.subTest(stdin=stdin):
                result = self.run_hook(stdin)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(output["permissionDecision"], "deny")

    def test_configured_list_replaces_the_default(self):
        self.project.write(".claude/guard.local.yml", "hooks:\n  session_paths: []\n")
        self.assertIsNotNone(self.hook("Write", file_path=self.scratch))

    def test_key_without_value_keeps_the_default(self):
        self.project.write(".claude/guard.local.yml", "hooks:\n  session_paths:\n")
        self.assertIsNone(self.hook("Write", file_path=self.scratch))

    def test_malformed_hooks_section_prompts_instead_of_crashing(self):
        self.project.write(".claude/guard.local.yml", "hooks:\n  session_paths: 5\n")
        self.assertIn("guard error", self.hook("Read", file_path=".claude/guard.yml"))


class ProjectDirTest(HookCase):
    """The project directory is $CLAUDE_PROJECT_DIR, or else the directory two levels above the hook."""

    def decision(self, env, path):
        event = {"tool_name": "Read", "tool_input": {"file_path": path}, "cwd": self.project.root}
        result = self.project.run([sys.executable, self.project.path(HOOK)], input=json.dumps(event), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_without_the_variable_the_hook_location_counts(self):
        self.assertEqual(self.decision({"CLAUDE_PROJECT_DIR": None}, self.project.path("a.txt")), "")

    def test_the_variable_wins_over_the_hook_location(self):
        other = self.project.path("src")
        self.assertEqual(self.decision({"CLAUDE_PROJECT_DIR": other}, self.project.path("src/a.txt")), "")
        self.assertIn('"ask"', self.decision({"CLAUDE_PROJECT_DIR": other}, self.project.path("a.txt")))


SETTINGS = os.path.join(HOST_PROJECT, ".claude", "settings.json")
OWN_SETTINGS = os.path.join(GUARD_DIR, ".claude", "settings.json")


class RegistrationCase:
    """Checks that a settings file registers the one guard, by its command, for every checked tool."""

    settings = ""
    command = ""

    def test_settings_register_the_one_guard_for_every_checked_tool(self):
        with open(self.settings, encoding="utf-8") as settings_file:
            entries = json.load(settings_file)["hooks"]["PreToolUse"]
        commands = [hook["command"] for entry in entries for hook in entry["hooks"]]
        self.assertEqual(commands, [self.command])
        matcher = entries[0]["matcher"]
        for tool in ("Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "mcp__x"):
            with self.subTest(tool=tool):
                self.assertRegex(tool, f"^(?:{matcher})$")


@unittest.skipUnless(os.path.exists(SETTINGS), "the guard is not installed in a project")
class RegistrationTest(RegistrationCase, unittest.TestCase):
    settings = SETTINGS
    command = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/project-guard/guard.py"'


class OwnRegistrationTest(RegistrationCase, unittest.TestCase):
    settings = OWN_SETTINGS
    command = 'python3 "$CLAUDE_PROJECT_DIR/guard.py"'


class GuardHookWithoutConfigTest(HookCase):
    config = False

    def test_repo_file_allowed(self):
        self.assertIsNone(self.hook("Read", file_path=".claude/guard.yml"))

    def test_symlink_out_of_repo_still_prompts(self):
        self.assertIsNotNone(self.hook("Read", file_path=f"{NEVER}/index.php"))

    def test_default_session_paths_allowed(self):
        self.assertIsNone(self.hook("Write", file_path=self.scratch))
        self.assertIsNone(self.hook("Read", file_path=self.memory))

    def test_outside_prompt_says_nothing_about_the_config(self):
        self.assertNotIn("config not loaded", self.hook("Read", file_path="/etc/hosts"))

    def test_without_config_files_pyyaml_is_not_needed(self):
        self.project.write(f"{os.path.dirname(HOOK)}/yaml.py", "raise ImportError('no yaml')\n")   # shadows PyYAML
        self.assertIsNone(self.hook("Write", file_path=self.scratch))

    def test_missing_pyyaml_is_reported(self):
        self.project.write(f"{os.path.dirname(HOOK)}/yaml.py", "raise ImportError('no yaml')\n")
        self.project.write(".claude/guard.yml", "hooks:\n  allowed_exact: [/etc/hosts]\n")
        self.assertIn("guard config not loaded: PyYAML not installed", self.hook("Read", file_path="/etc/hosts"))

    def test_invalid_yaml_is_reported(self):
        self.project.write(".claude/guard.yml", "shared: [unclosed\n")
        self.assertIn("guard config not loaded: .claude/guard.yml", self.hook("Read", file_path="/etc/hosts"))


class ConfigFilesTest(HookCase):
    """The guard's guard.yml holds the defaults; the project's .claude/guard.yml and then .claude/guard.local.yml
    override it."""
    config = False

    def test_the_guards_own_config_is_read(self):
        self.project.write(f"{os.path.dirname(HOOK)}/guard.yml", "hooks:\n  extra_allowed_paths: [/opt/shared]\n")
        self.assertIsNone(self.hook("Read", file_path="/opt/shared/x.md"))

    def test_the_project_config_overrides_the_guards(self):
        self.project.write(f"{os.path.dirname(HOOK)}/guard.yml", "hooks:\n  extra_allowed_paths: [/opt/shared]\n")
        self.project.write(".claude/guard.yml", "hooks:\n  extra_allowed_paths: []\n")
        self.assertIsNotNone(self.hook("Read", file_path="/opt/shared/x.md"))

    def test_the_personal_config_overrides_the_projects(self):
        self.project.write(".claude/guard.yml", "hooks:\n  extra_allowed_paths: []\n")
        self.project.write(".claude/guard.local.yml", "hooks:\n  extra_allowed_paths: [/opt/shared]\n")
        self.assertIsNone(self.hook("Read", file_path="/opt/shared/x.md"))


class ShippedConfigTest(unittest.TestCase):
    def test_shipped_config_documents_the_defaults(self):
        with open(os.path.join(GUARD_DIR, "guard.yml"), encoding="utf-8") as config_file:
            shipped = yaml.safe_load(config_file)
        namespace = runpy.run_path(os.path.join(GUARD_DIR, "guard.py"))
        defaults = namespace["DEFAULTS"]
        self.assertEqual(shipped, {"shared": {"secrets": defaults["shared"]["secrets"]}, "hooks": defaults["hooks"]})


if __name__ == "__main__":
    unittest.main()
