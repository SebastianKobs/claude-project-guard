"""guard.py, agent scopes: the subagents in hooks.agent_scopes (.claude/guard.yml) run only their configured commands
and write only inside their configured paths, every other subagent writes only inside hooks.subagent_write (default:
the project), no subagent writes the guard's own files, and how that combines with the project boundary. Fixture
config: .claude/tests/fixture-guard.yml (doc-writer, test-writer, test-surveyor) with
.claude/tests/fixture-tests.yml."""
import json
import os
import re
import sys
import unittest

from helpers import HOOK
from helpers import ProjectTestCase

PEST = "vendor/bin/pest --colors=never"


class AgentHookCase(ProjectTestCase):
    def decision(self, tool, agent=None, raw_stdin=None, **tool_input):
        """Run the fixture's hook for a call by agent (None: the main session); return (decision, reason), or
        (None, None) when it stays silent."""
        event = {"tool_name": tool, "tool_input": tool_input, "cwd": self.project.root}
        if agent is not None:
            event["agent_type"] = agent
        stdin = raw_stdin if raw_stdin is not None else json.dumps(event)
        result = self.project.run([sys.executable, self.project.path(HOOK)], input=stdin)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout:
            return None, None
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        return output["permissionDecision"], output.get("permissionDecisionReason", "")

    def bash(self, command, agent="test-writer"):
        return self.decision("Bash", agent=agent, command=command)[0]

    def write(self, path, agent="test-writer", tool="Write"):
        return self.decision(tool, agent=agent, file_path=path, content="x")[0]

    def scopes(self, **entries):
        """Replace hooks.agent_scopes in the fixture's .claude/guard.yml."""
        self.project.configure_hooks(agent_scopes=entries)


class WriterBashTest(AgentHookCase):
    def test_configured_command_on_a_test_file_is_allowed(self):
        self.assertEqual(self.bash(f"{PEST} tests/Unit/lib/FooTest.php"), "allow")

    def test_several_test_files_and_support_files_are_allowed(self):
        self.assertEqual(self.bash(f"{PEST} tests/Unit/lib/FooTest.php tests/Support/DbDoubleTest.php"), "allow")

    def test_absolute_test_path_inside_the_repo_is_allowed(self):
        self.assertEqual(self.bash(f"{PEST} {self.project.root}/tests/Unit/FooTest.php"), "allow")

    def test_command_without_test_files_is_denied(self):
        self.assertEqual(self.bash(PEST), "deny")

    def test_file_outside_the_test_dirs_is_denied(self):
        self.assertEqual(self.bash(f"{PEST} src/lib/Foo.php"), "deny")

    def test_parent_escape_is_denied(self):
        self.assertEqual(self.bash(f"{PEST} tests/Unit/../../src/lib/Foo.php"), "deny")

    def test_extra_option_is_denied(self):
        self.assertEqual(self.bash(f"{PEST} --filter foo tests/Unit/FooTest.php"), "deny")

    def test_shell_operators_are_denied(self):
        for command in (f"{PEST} tests/Unit/FooTest.php; rm -rf src", f"{PEST} tests/Unit/FooTest.php && ls",
                        f"{PEST} tests/Unit/FooTest.php > out.txt", f"{PEST} tests/Unit/$(whoami)Test.php",
                        f"{PEST} tests/Unit/FooTest.php | tee x"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), "deny")

    def test_other_commands_are_denied_with_the_allowed_one_named(self):
        decision, reason = self.decision("Bash", agent="test-writer", command="cat src/lib/Foo.php")
        self.assertEqual(decision, "deny")
        self.assertIn("vendor/bin/pest --colors=never {paths}", reason)


class WriterFileTest(AgentHookCase):
    def test_writes_in_test_and_support_dirs_stay_silent(self):
        self.assertIsNone(self.write("tests/Unit/lib/FooTest.php"))
        self.assertIsNone(self.write(self.project.path("tests/Support/Db.php"), tool="Edit"))

    def test_write_outside_is_denied(self):
        for path in ("src/lib/Foo.php", "tests/Pest.php", "config/tests.yml", "tests/Unit/../../src/x.php"):
            with self.subTest(path=path):
                self.assertEqual(self.write(path), "deny")

    def test_write_through_a_symlink_out_of_the_test_dir_is_denied(self):
        os.makedirs(self.project.path("tests/Unit"))
        os.makedirs(self.project.path("src"))
        os.symlink(self.project.path("src"), self.project.path("tests/Unit/link"))
        self.assertEqual(self.write("tests/Unit/link/Foo.php"), "deny")

    def test_other_edit_tools_are_checked_too(self):
        self.assertEqual(self.write("src/x.php", tool="MultiEdit"), "deny")
        self.assertEqual(self.decision("NotebookEdit", agent="test-writer",
                                       notebook_path="src/x.ipynb")[0], "deny")


class SurveyorTest(AgentHookCase):
    def test_bash_is_denied(self):
        self.assertEqual(self.bash("ls", agent="test-surveyor"), "deny")
        self.assertEqual(self.bash(f"{PEST} tests/Unit/FooTest.php", agent="test-surveyor"), "deny")

    def test_only_the_fixtures_doc_may_be_written(self):
        self.assertIsNone(self.write("tests/FIXTURES.md", agent="test-surveyor"))
        self.assertEqual(self.write("tests/Unit/FooTest.php", agent="test-surveyor"), "deny")


class DocWriterTest(AgentHookCase):
    def test_writes_below_the_docs_dir_stay_silent(self):
        self.assertIsNone(self.write("docs-refactor/src/lib/Foo.php.md", agent="doc-writer"))

    def test_write_outside_the_docs_dir_is_denied(self):
        for path in ("src/lib/Foo.php", "docs-refactor/STATUS.md", "CLAUDE.md"):
            with self.subTest(path=path):
                self.assertEqual(self.write(path, agent="doc-writer"), "deny")

    def test_bash_is_denied(self):
        self.assertEqual(self.bash("ls", agent="doc-writer"), "deny")


class DispatchTest(AgentHookCase):
    """The event's agent_type picks the scope."""

    def test_main_session_events_are_left_alone(self):
        self.assertEqual(self.decision("Bash", command="ls"), (None, None))

    def test_unlisted_agents_may_write_in_the_project(self):
        self.assertEqual(self.decision("Write", agent="code-surveyor", file_path="src/x.php"),
                         (None, None))

    def test_unlisted_agents_bash_is_left_alone(self):
        self.assertEqual(self.decision("Bash", agent="code-surveyor", command="rm -rf src"), (None, None))

    def test_read_tools_stay_silent(self):
        self.assertEqual(self.decision("Read", agent="test-writer", file_path="src/lib/Foo.php"),
                         (None, None))


class ScopeConfigTest(AgentHookCase):
    def test_literal_paths_and_commands(self):
        self.scopes(helper={"write": ["notes"], "commands": ["make lint"]})
        self.assertIsNone(self.write("notes/a.md", agent="helper"))
        self.assertEqual(self.write("src/a.php", agent="helper"), "deny")
        self.assertEqual(self.bash("make lint", agent="helper"), "allow")
        self.assertEqual(self.bash("make lint extra", agent="helper"), "deny")

    def test_any_of_several_commands_is_allowed(self):
        self.scopes(helper={"write": ["notes"], "commands": ["make lint", "cat {paths}"]})
        self.assertEqual(self.bash("make lint", agent="helper"), "allow")
        self.assertEqual(self.bash("cat notes/a.md", agent="helper"), "allow")
        self.assertEqual(self.bash("cat src/a.php", agent="helper"), "deny")

    def test_agent_without_write_key_may_not_write(self):
        self.scopes(helper={"commands": ["make lint"]})
        self.assertEqual(self.write("notes/a.md", agent="helper"), "deny")

    def test_reference_to_an_agents_yml_value(self):
        self.scopes(helper={"write": ["{agents.docs.dir}"]})
        self.assertIsNone(self.write("docs-refactor/STATUS.md", agent="helper"))

    def test_unknown_reference_is_denied_with_reason(self):
        self.scopes(helper={"write": ["{agents.docs.nothing}"]})
        decision, reason = self.decision("Write", agent="helper", file_path="docs-refactor/a.md")
        self.assertEqual(decision, "deny")
        self.assertIn("agents.docs.nothing", reason)

    def test_unknown_entry_key_is_denied_with_reason(self):
        self.scopes(helper={"writes": ["notes"]})
        decision, reason = self.decision("Write", agent="helper", file_path="notes/a.md")
        self.assertEqual(decision, "deny")
        self.assertIn("writes", reason)

    def test_write_value_that_is_no_list_is_denied(self):
        self.scopes(helper={"write": "notes"})
        self.assertEqual(self.write("notes/a.md", agent="helper"), "deny")

    def test_broken_entry_affects_only_its_agent(self):
        self.scopes(helper="everything", **{"doc-writer": {"write": ["{agents.docs.src_dir}"]}})
        self.assertEqual(self.write("notes/a.md", agent="helper"), "deny")
        self.assertIsNone(self.write("docs-refactor/src/a.md", agent="doc-writer"))

    def test_without_agent_scopes_every_agent_is_left_alone(self):
        self.scopes()
        self.assertEqual(self.decision("Bash", agent="test-writer", command="ls"), (None, None))


class FailClosedTest(AgentHookCase):
    def test_missing_tests_config_denies_the_test_agents_with_reason(self):
        os.remove(self.project.path("config/tests.yml"))
        decision, reason = self.decision("Bash", agent="test-writer",
                                         command=f"{PEST} tests/Unit/FooTest.php")
        self.assertEqual(decision, "deny")
        self.assertIn("config/tests.yml", reason)

    def test_missing_tests_config_leaves_the_doc_writer_working(self):
        os.remove(self.project.path("config/tests.yml"))
        self.assertIsNone(self.write("docs-refactor/src/a.md", agent="doc-writer"))

    def test_missing_agents_config_falls_back_to_the_defaults(self):
        os.remove(self.project.path(".claude/guard.yml"))
        self.assertEqual(self.decision("Bash", agent="test-writer", command="ls"), (None, None))
        self.assertIsNone(self.write("src/x.php"))
        self.assertEqual(self.write("/etc/x.php"), "deny")

    def test_broken_agents_config_denies_every_subagent_with_reason(self):
        self.project.write(".claude/guard.yml", "hooks: [unclosed\n")
        for tool_input in ({"tool": "Bash", "command": "ls"}, {"tool": "Write", "file_path": "src/x.php"}):
            with self.subTest(tool=tool_input["tool"]):
                decision, reason = self.decision(agent="code-surveyor", **tool_input)
                self.assertEqual(decision, "deny")
                self.assertIn(".claude/guard.yml", reason)

    def test_agents_config_that_is_no_mapping_denies_every_subagent(self):
        self.project.write(".claude/guard.yml", "- a list\n")
        decision, reason = self.decision("Bash", agent="code-surveyor", command="ls")
        self.assertEqual(decision, "deny")
        self.assertIn("not a mapping", reason)

    def test_missing_pyyaml_denies_every_subagent_with_reason(self):
        self.project.write(f"{os.path.dirname(HOOK)}/yaml.py", "raise ImportError('no yaml')\n")   # shadows PyYAML
        decision, reason = self.decision("Bash", agent="code-surveyor", command="ls")
        self.assertEqual(decision, "deny")
        self.assertIn("PyYAML not installed", reason)

    def test_broken_agents_config_leaves_reads_alone(self):
        self.project.write(".claude/guard.yml", "hooks: [unclosed\n")
        self.assertEqual(self.decision("Read", agent="code-surveyor", file_path="src/x.php"), (None, None))

    def test_missing_agents_config_leaves_the_main_session_alone(self):
        os.remove(self.project.path(".claude/guard.yml"))
        self.assertEqual(self.decision("Bash", command="ls"), (None, None))

    def test_broken_input_is_denied(self):
        self.assertEqual(self.decision("Bash", raw_stdin="{not json")[0], "deny")

    def test_input_that_is_no_object_is_denied(self):
        self.assertEqual(self.decision("Bash", raw_stdin="[]")[0], "deny")

    def test_broken_agent_scopes_leave_the_main_session_alone(self):
        self.project.configure_hooks(agent_scopes="everything")
        self.assertEqual(self.decision("Bash", command="ls"), (None, None))
        self.assertEqual(self.decision("Bash", agent="doc-writer", command="ls")[0], "deny")


class DefaultScopeTest(AgentHookCase):
    """Subagents not in hooks.agent_scopes write only inside hooks.subagent_write, by default the project."""

    def test_write_outside_the_project_is_denied_with_reason(self):
        decision, reason = self.decision("Write", agent="code-surveyor", file_path="/tmp/x.txt", content="x")
        self.assertEqual(decision, "deny")
        self.assertIn("the project directory", reason)
        self.assertIn("hooks.subagent_write", reason)

    def test_every_edit_tool_is_checked(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            with self.subTest(tool=tool):
                self.assertEqual(self.write("../other/x.py", agent="code-surveyor", tool=tool), "deny")
        self.assertEqual(self.decision("NotebookEdit", agent="code-surveyor", notebook_path="/tmp/x.ipynb")[0],
                         "deny")

    def test_session_directories_are_outside_too(self):
        slug = re.sub(r"[^A-Za-z0-9]", "-", self.project.root)
        self.assertEqual(self.write(f"/tmp/claude-{os.getuid()}/{slug}/scratchpad/x.txt", agent="code-surveyor"),
                         "deny")

    def test_write_through_a_symlink_out_of_the_project_is_denied(self):
        os.symlink("/tmp", self.project.path("out"))
        self.assertEqual(self.write("out/x.txt", agent="code-surveyor"), "deny")

    def test_write_without_a_path_is_denied(self):
        self.assertEqual(self.decision("Write", agent="code-surveyor", content="x")[0], "deny")

    def test_configured_paths_replace_the_project(self):
        self.project.configure_hooks(subagent_write=["notes"])
        self.assertIsNone(self.write("notes/a.md", agent="code-surveyor"))
        self.assertEqual(self.write("src/a.php", agent="code-surveyor"), "deny")

    def test_empty_list_allows_no_writes(self):
        self.project.configure_hooks(subagent_write=[])
        decision, reason = self.decision("Write", agent="code-surveyor", file_path="src/a.php")
        self.assertEqual(decision, "deny")
        self.assertIn("may not write files", reason)

    def test_key_without_value_keeps_the_project(self):
        self.project.configure_hooks(subagent_write=None)
        self.assertIsNone(self.write("src/a.php", agent="code-surveyor"))

    def test_value_that_is_no_list_is_denied(self):
        self.project.configure_hooks(subagent_write="notes")
        self.assertEqual(self.write("notes/a.md", agent="code-surveyor"), "deny")

    def test_listed_agents_keep_their_own_scope(self):
        self.project.configure_hooks(subagent_write=["src"])
        self.assertEqual(self.write("src/a.php", agent="doc-writer"), "deny")

    def test_main_session_is_not_limited(self):
        self.assertEqual(self.decision("Write", file_path="/etc/x")[0], "ask")


class ProtectedFilesTest(AgentHookCase):
    """No subagent writes the files that enforce the guard, whatever its scope."""
    PROTECTED = (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/guard.py",
                 ".claude/hooks/new-hook.sh", ".claude/guard.yml", ".claude/guard.local.yml")

    def test_edit_tools_are_denied_with_reason(self):
        for path in self.PROTECTED:
            with self.subTest(path=path):
                decision, reason = self.decision("Edit", agent="code-surveyor", file_path=path)
                self.assertEqual(decision, "deny")
                self.assertIn("guard's own files", reason)

    def test_user_settings_are_protected(self):
        self.assertEqual(self.write("~/.claude/settings.json", agent="code-surveyor"), "deny")

    def test_absolute_and_parent_paths_are_caught(self):
        self.assertEqual(self.write(self.project.path(".claude/guard.yml"), agent="code-surveyor"), "deny")
        self.assertEqual(self.write("src/../.claude/hooks/guard.py", agent="code-surveyor"), "deny")

    def test_symlink_to_a_protected_file_is_caught(self):
        os.symlink(self.project.path(".claude/guard.yml"), self.project.path("link.yml"))
        self.assertEqual(self.write("link.yml", agent="code-surveyor"), "deny")

    def test_bash_naming_a_protected_file_is_denied(self):
        for command in ("sed -i s/a/b/ .claude/hooks/guard.py", "echo x >.claude/guard.yml",
                        "cd .claude && rm settings.json", "cat .claude/guard.local.yml"):
            with self.subTest(command=command):
                decision, reason = self.decision("Bash", agent="code-surveyor", command=command)
                self.assertEqual(decision, "deny")
                self.assertIn("Read", reason)

    def test_a_wide_scope_does_not_lift_it(self):
        self.scopes(wide={"write": ["."], "commands": ["sed -i s/a/b/ {paths}"]})
        self.assertEqual(self.write(".claude/hooks/guard.py", agent="wide"), "deny")
        self.assertEqual(self.bash("sed -i s/a/b/ .claude/guard.yml", agent="wide"), "deny")
        self.assertEqual(self.bash("sed -i s/a/b/ src/a.php", agent="wide"), "allow")

    def test_broken_config_still_names_the_protected_file(self):
        self.project.write(".claude/guard.yml", "hooks: [unclosed\n")
        decision, reason = self.decision("Edit", agent="code-surveyor", file_path=".claude/guard.yml")
        self.assertEqual(decision, "deny")
        self.assertIn("guard's own files", reason)

    def test_neighbouring_files_stay_writable(self):
        for path in (".claude/agents/x.md", ".claude/tests/test_x.py", "config/tests.yml", "config/other.yml"):
            with self.subTest(path=path):
                self.assertIsNone(self.write(path, agent="code-surveyor"))

    def test_reads_stay_silent(self):
        self.assertEqual(self.decision("Read", agent="code-surveyor", file_path=".claude/hooks/guard.py"),
                         (None, None))

    def test_main_session_may_edit_them(self):
        self.assertEqual(self.decision("Edit", file_path=".claude/hooks/guard.py"), (None, None))
        self.assertEqual(self.decision("Bash", command="sed -i s/a/b/ .claude/guard.yml"), (None, None))


class CombinedDecisionTest(AgentHookCase):
    """The agent scopes and the project boundary in one hook: deny beats ask, ask beats allow."""

    def test_configured_command_naming_an_outside_path_asks(self):
        self.scopes(helper={"commands": ["cat /etc/hosts"]})
        decision, reason = self.decision("Bash", agent="helper", command="cat /etc/hosts")
        self.assertEqual(decision, "ask")
        self.assertIn("/etc/hosts", reason)

    def test_write_outside_the_project_by_a_listed_agent_is_denied(self):
        self.assertEqual(self.write("/etc/x.php"), "deny")

    def test_outside_read_by_a_listed_agent_asks(self):
        self.assertEqual(self.decision("Read", agent="test-writer", file_path="/etc/hosts")[0], "ask")

    def test_outside_write_by_an_unlisted_agent_is_denied(self):
        self.assertEqual(self.decision("Write", agent="code-surveyor", file_path="/etc/x")[0], "deny")

    def test_outside_read_by_an_unlisted_agent_asks(self):
        self.assertEqual(self.decision("Read", agent="code-surveyor", file_path="/etc/hosts")[0], "ask")

    def test_outside_bash_by_an_unlisted_agent_asks(self):
        self.assertEqual(self.bash("cat /etc/hosts", agent="code-surveyor"), "ask")

    def test_outside_path_by_the_main_session_asks(self):
        self.assertEqual(self.decision("Read", file_path="/etc/hosts")[0], "ask")


class RedirectTargetTest(AgentHookCase):
    """Subagents may write the guard's tmp/, where redirected output goes, but nothing else of the guard."""

    def test_subagent_may_write_the_guards_tmp(self):
        self.assertIsNone(self.write(".claude/hooks/project-guard/tmp/x.txt", agent="code-surveyor"))

    def test_subagent_may_not_write_the_rest_of_the_guard(self):
        self.assertEqual(self.write(".claude/hooks/project-guard/guard.yml", agent="code-surveyor"), "deny")

    def test_subagents_redirected_command_is_not_denied(self):
        decision, _ = self.decision("Bash", agent="code-surveyor",
                                    command="/usr/bin/chromium --screenshot=/home/other/x.png http://x/")
        self.assertEqual(decision, "allow")


if __name__ == "__main__":
    unittest.main()
