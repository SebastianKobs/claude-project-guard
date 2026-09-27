"""guard.py, secrets: a tool call that names a file of shared.secrets (.env, private keys, ~/.ssh, ...), inside the
project or outside, gets a short warning. In the main session a Bash command prompts, with the warning as a comment
on top of the command, since some permission dialogs show only the call; every other tool is denied, as it has no
place for the warning. Subagents are always denied. HOME points to a throwaway folder next to the project, so no
test touches the real home directory."""
import json
import os
import shutil
import sys
import tempfile
import unittest

import yaml

from helpers import HOOK
from helpers import ProjectTestCase
from helpers import TMP_DIR


class SecretsCase(ProjectTestCase):
    def setUp(self):
        super().setUp()
        self.home = os.path.realpath(tempfile.mkdtemp(dir=TMP_DIR))
        self.addCleanup(shutil.rmtree, self.home)
        os.makedirs(os.path.join(self.home, ".ssh"))

    def output(self, tool, agent=None, **tool_input):
        """Run the hook; return its whole answer, or None when it stays silent."""
        event = {"tool_name": tool, "tool_input": tool_input, "cwd": self.project.root}
        if agent:
            event["agent_type"] = agent
        result = self.project.run([sys.executable, self.project.path(HOOK)], input=json.dumps(event),
                                  env={"HOME": self.home})
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout:
            return None
        return json.loads(result.stdout)

    def hook(self, tool, agent=None, **tool_input):
        """Run the hook; return (decision, reason), or None when it stays silent."""
        output = self.output(tool, agent, **tool_input)
        if output is None:
            return None
        specific = output["hookSpecificOutput"]
        return specific["permissionDecision"], specific["permissionDecisionReason"]

    def assert_secret(self, answer, name, decision):
        """answer is `decision` with the short secret warning that names `name`."""
        self.assertIsNotNone(answer)
        self.assertEqual(answer[0], decision)
        self.assertTrue(answer[1].startswith("SECRET: "), answer[1])
        self.assertIn(name, answer[1])

    def assert_secret_prompt(self, answer, name):
        """answer is a prompt with the short secret warning that names `name`."""
        self.assert_secret(answer, name, "ask")

    def assert_secret_denied(self, answer, name):
        """answer is a deny with the short secret warning that names `name`."""
        self.assert_secret(answer, name, "deny")

    def configure_secrets(self, secrets):
        """Set shared.secrets in the project's .claude/guard.yml."""
        path = self.project.path(".claude/guard.yml")
        with open(path, encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        config.setdefault("shared", {})["secrets"] = secrets
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle)


class InsideProjectTest(SecretsCase):
    def test_read_env_file_is_denied(self):
        self.assert_secret_denied(self.hook("Read", file_path=".env"), ".env")

    def test_read_env_variant_is_denied(self):
        self.assert_secret_denied(self.hook("Read", file_path=self.project.path("app/.env.production")),
                                  ".env.production")

    def test_env_template_stays_silent(self):
        self.assertIsNone(self.hook("Read", file_path=".env.example"))

    def test_private_key_file_is_denied(self):
        self.assert_secret_denied(self.hook("Read", file_path="certs/server.pem"), "server.pem")

    def test_public_key_stays_silent(self):
        self.assertIsNone(self.hook("Read", file_path="keys/id_ed25519.pub"))

    def test_writing_a_secret_is_denied(self):
        self.assert_secret_denied(self.hook("Write", file_path=".env", content="KEY=1"), ".env")

    def test_grep_glob_naming_a_secret_is_denied(self):
        self.assert_secret_denied(self.hook("Grep", pattern="KEY", glob=".env"), ".env")

    def test_symlink_to_a_secret_is_denied(self):
        target = os.path.join(self.home, ".ssh", "id_rsa")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("key")
        os.symlink(target, self.project.path("creds"))
        self.assert_secret_denied(self.hook("Read", file_path="creds"), "id_rsa")

    def test_ordinary_file_stays_silent(self):
        self.assertIsNone(self.hook("Read", file_path="src/environment.py"))


class BashTest(SecretsCase):
    def test_relative_secret_in_command_prompts(self):
        self.assert_secret_prompt(self.hook("Bash", command="cat .env | grep TOKEN"), ".env")

    def test_reason_does_not_repeat_the_command(self):
        _, reason = self.hook("Bash", command="cat .env | grep TOKEN")
        self.assertNotIn("grep", reason)
        self.assertLess(len(reason), 100)

    def test_relative_secret_after_cd_prompts(self):
        self.assert_secret_prompt(self.hook("Bash", command="cd config && source .env.local"), "config/.env.local")

    def test_option_value_prompts(self):
        self.assert_secret_prompt(self.hook("Bash", command="docker run --env-file=.env app"), ".env")

    def test_home_variable_prompts(self):
        self.assert_secret_prompt(self.hook("Bash", command="ls $HOME/.aws"), ".aws")

    def test_several_secrets_are_counted_not_listed(self):
        _, reason = self.hook("Bash", command="cat .env id_rsa certs/a.pem")
        self.assertIn("+2 more", reason)

    def test_word_that_only_contains_a_secret_name_stays_silent(self):
        self.assertIsNone(self.hook("Bash", command="echo dotenv envfile"))


class OutsideProjectTest(SecretsCase):
    def test_ssh_directory_gets_secret_warning(self):
        answer = self.hook("Read", file_path="~/.ssh/config")
        self.assert_secret_denied(answer, "~/.ssh/config")
        self.assertNotIn("outside the project", answer[1])

    def test_windows_ssh_key_prompts(self):
        self.assert_secret_prompt(self.hook("Bash", command="cat 'C:\\Users\\me\\.ssh\\id_ed25519'"), "id_ed25519")

    def test_mounted_windows_home_is_denied(self):
        self.assert_secret_denied(self.hook("Read", file_path="/mnt/c/Users/me/.aws/credentials"), "credentials")

    def test_other_outside_path_keeps_boundary_prompt(self):
        _, reason = self.hook("Read", file_path="/etc/hosts")
        self.assertIn("outside the project", reason)


class WarningTest(SecretsCase):
    """Not every permission dialog shows the reason (the VS Code extension shows only the call), so the warning is
    put where the user sees it."""

    def test_bash_prompt_puts_the_warning_on_top_of_the_command(self):
        output = self.output("Bash", command="cat .env | grep TOKEN")
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["updatedInput"]["command"],
                         f"# {specific['permissionDecisionReason']}\ncat .env | grep TOKEN")

    def test_bash_prompt_keeps_the_other_arguments(self):
        output = self.output("Bash", command="cat .env", description="Show env", timeout=5000)
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual((updated["description"], updated["timeout"]), ("Show env", 5000))

    def test_command_that_carries_the_warning_is_not_marked_twice(self):
        warned = self.output("Bash", command="cat .env")["hookSpecificOutput"]["updatedInput"]["command"]
        again = self.output("Bash", command=warned)["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertEqual(again, warned)

    def test_bash_prompt_warns_the_user(self):
        output = self.output("Bash", command="cat .env")
        self.assertEqual(output["systemMessage"], output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_denied_tool_tells_the_model_to_ask_the_user(self):
        _, reason = self.hook("Read", file_path=".env")
        self.assertIn("ask the user", reason)

    def test_boundary_prompt_has_no_warning(self):
        self.assertNotIn("systemMessage", self.output("Read", file_path="/etc/hosts"))


class SubagentTest(SecretsCase):
    def test_subagent_read_is_denied(self):
        decision, reason = self.hook("Read", agent="helper", file_path=".env")
        self.assertEqual(decision, "deny")
        self.assertTrue(reason.startswith("SECRET: "), reason)

    def test_subagent_bash_is_denied(self):
        decision, _ = self.hook("Bash", agent="helper", command="cat ~/.ssh/id_rsa")
        self.assertEqual(decision, "deny")


class ConfigTest(SecretsCase):
    def test_project_list_replaces_defaults(self):
        self.configure_secrets(["*.secret"])
        self.assertIsNone(self.hook("Read", file_path=".env"))
        self.assert_secret_denied(self.hook("Read", file_path="db.secret"), "db.secret")

    def test_project_relative_path_pattern_is_denied(self):
        self.configure_secrets(["config/credentials"])
        self.assert_secret_denied(self.hook("Read", file_path="config/credentials/db.yml"), "config/credentials")

    def test_broken_list_prompts_with_error(self):
        self.configure_secrets(".env")
        decision, reason = self.hook("Read", file_path="README.md")
        self.assertEqual(decision, "ask")
        self.assertIn("shared.secrets", reason)


if __name__ == "__main__":
    unittest.main()
