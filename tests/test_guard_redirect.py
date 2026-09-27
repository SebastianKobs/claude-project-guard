"""guard.py, output redirects: an output file argument of a tool in hooks.redirect_outputs.tools (default: the
Playwright MCP tools' `filename`) or an output option of a listed program in Bash that points outside the project is
rewritten to <guard>/tmp/<server>.<tool>/<file name> (or <program>/<file name>), and the call is allowed with a
warning for the user and the model, so an approved prompt can't write outside."""
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest

from helpers import HOOK
from helpers import TARGET
from helpers import TMP_DIR
from helpers import ProjectTestCase

SCREENSHOT = "mcp__playwright__browser_take_screenshot"
PLUGIN_PDF = "mcp__plugin_playwright_playwright__browser_pdf_save"


class RedirectCase(ProjectTestCase):
    def output(self, tool, agent=None, **tool_input):
        """Run the fixture's hook; return its whole JSON output, or None when it stays silent."""
        event = {"tool_name": tool, "tool_input": tool_input, "cwd": self.project.root}
        if agent is not None:
            event["agent_type"] = agent
        result = self.project.run([sys.executable, self.project.path(HOOK)], input=json.dumps(event))
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout:
            return None
        return json.loads(result.stdout)

    def redirected(self, tool, **tool_input):
        """The rewritten tool input of a call the hook must redirect."""
        output = self.output(tool, **tool_input)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        return output["updatedInput"]


class RedirectTest(RedirectCase):
    def test_windows_path_goes_to_the_project_tmp(self):
        updated = self.redirected(SCREENSHOT, filename="C:/Users/seb/Desktop/shot.png", type="png")
        expected = self.project.path(f"{TARGET}/playwright.browser_take_screenshot/shot.png")
        self.assertEqual(updated["filename"], expected)

    def test_other_arguments_are_kept(self):
        updated = self.redirected(SCREENSHOT, filename="C:/Users/seb/shot.png", type="png", fullPage=True)
        self.assertEqual((updated["type"], updated["fullPage"]), ("png", True))

    def test_windows_backslash_and_unc_paths_are_redirected(self):
        for path in [r"C:\Users\seb\shot.png", r"\\server\share\shot.png"]:
            with self.subTest(path=path):
                updated = self.redirected(SCREENSHOT, filename=path)
                self.assertEqual(os.path.basename(updated["filename"]), "shot.png")

    def test_posix_path_outside_is_redirected(self):
        for path in ["/home/other/shot.png", "~/shot.png", "../shot.png"]:
            with self.subTest(path=path):
                updated = self.redirected(SCREENSHOT, filename=path)
                self.assertTrue(updated["filename"].startswith(self.project.path(TARGET)), updated["filename"])

    def test_plugin_server_names_match_too(self):
        updated = self.redirected(PLUGIN_PDF, filename="C:/Users/seb/page.pdf")
        self.assertEqual(updated["filename"],
                         self.project.path(f"{TARGET}/plugin_playwright_playwright.browser_pdf_save/page.pdf"))

    def test_target_directory_is_created(self):
        updated = self.redirected(SCREENSHOT, filename="C:/Users/seb/shot.png")
        self.assertTrue(os.path.isdir(os.path.dirname(updated["filename"])))

    def test_path_without_file_name_gets_the_tool_name(self):
        updated = self.redirected(SCREENSHOT, filename="C:/Users/seb/")
        self.assertEqual(os.path.basename(updated["filename"]), "browser_take_screenshot")

    def test_user_and_model_are_told_where_it_went(self):
        output = self.output(SCREENSHOT, filename="C:/Users/seb/shot.png")
        target = f"{TARGET}/playwright.browser_take_screenshot/shot.png"
        self.assertIn("C:/Users/seb/shot.png", output["systemMessage"])
        self.assertIn(target, output["systemMessage"])
        self.assertIn(target, output["hookSpecificOutput"]["additionalContext"])

    def test_subagents_are_redirected_too(self):
        output = self.output(SCREENSHOT, agent="doc-writer", filename="C:/Users/seb/shot.png")
        self.assertIn("updatedInput", output["hookSpecificOutput"])


class NoRedirectTest(RedirectCase):
    def test_relative_file_name_is_left_alone(self):
        self.assertIsNone(self.output(SCREENSHOT, filename="shot.png"))

    def test_path_inside_the_project_is_left_alone(self):
        self.assertIsNone(self.output(SCREENSHOT, filename=self.project.path("shots/shot.png")))

    def test_call_without_the_argument_is_left_alone(self):
        self.assertIsNone(self.output(SCREENSHOT, type="png"))

    def test_other_tools_are_left_alone(self):
        self.assertIsNone(self.output("mcp__other__save", filename="C:/Users/seb/shot.png"))

    def test_input_files_are_not_redirected(self):
        output = self.output("mcp__playwright__browser_file_upload", paths=["/etc/hosts"])
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertNotIn("updatedInput", output["hookSpecificOutput"])


class RedirectCombinedTest(RedirectCase):
    def test_other_outside_path_still_prompts_with_the_rewrite(self):
        output = self.output(SCREENSHOT, filename="C:/Users/seb/shot.png", path="/etc/hosts")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")
        self.assertIn("/etc/hosts", output["permissionDecisionReason"])
        self.assertTrue(output["updatedInput"]["filename"].startswith(self.project.path(TARGET)))

    def test_configured_tools_and_target_replace_the_default(self):
        self.project.configure_hooks(redirect_outputs={"target": "out", "tools": {"mcp__saver__*": ["outputPath"]}})
        updated = self.redirected("mcp__saver__save", outputPath="/var/x.json")
        self.assertEqual(updated["outputPath"], self.project.path("out/saver.save/x.json"))
        self.assertIsNone(self.output(SCREENSHOT, filename="shot.png"))

    def test_target_outside_the_project_is_refused(self):
        self.project.configure_hooks(redirect_outputs={"target": "../elsewhere"})
        output = self.output(SCREENSHOT, filename="C:/Users/seb/shot.png")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")
        self.assertIn("redirect_outputs", output["permissionDecisionReason"])
        self.assertNotIn("updatedInput", output)

    def test_guard_outside_the_project_prompts_only_for_a_redirect(self):
        os.makedirs(self.project.path("src"))
        event = {"tool_name": SCREENSHOT, "tool_input": {"filename": "C:/Users/seb/shot.png"}, "cwd": self.project.root}
        result = self.project.run([sys.executable, self.project.path(HOOK)], input=json.dumps(event),
                                  env={"CLAUDE_PROJECT_DIR": self.project.path("src")})
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")
        self.assertIn("is outside the project", output["permissionDecisionReason"])
        self.assertFalse(os.path.exists(self.project.path(TARGET)))

    def test_broken_config_prompts_instead_of_crashing(self):
        self.project.configure_hooks(redirect_outputs={"tools": ["filename"]})
        output = self.output(SCREENSHOT, filename="C:/Users/seb/shot.png")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")
        self.assertIn("redirect_outputs", output["permissionDecisionReason"])


CHROME = "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"
EDGE = "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
DISTRO = "Ubuntu-Test"


class BashRedirectCase(RedirectCase):
    def bash(self, command, distro=DISTRO, path=None):
        """The hook's whole output for a Bash call, run as in WSL with the distro `distro` (None: not in WSL) and with
        PATH set to `path` if given."""
        event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.project.root}
        env = {"WSL_DISTRO_NAME": distro}
        if path is not None:
            env["PATH"] = path
        result = self.project.run([sys.executable, self.project.path(HOOK)], input=json.dumps(event), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout:
            return None
        return json.loads(result.stdout)

    def share_path(self, relative):
        """The Windows form (\\\\wsl.localhost\\<distro>\\...) of a project-relative path."""
        return f"\\\\wsl.localhost\\{DISTRO}" + self.project.path(relative).replace("/", "\\")

    def outside_program(self, name, entry=None):
        """An executable `name` in a fresh folder outside the project, listed there as `entry` (default: the bare
        name); its path."""
        folder = os.path.realpath(tempfile.mkdtemp(dir=TMP_DIR))
        self.addCleanup(shutil.rmtree, folder)
        program = os.path.join(folder, name)
        with open(program, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(program, 0o755)
        entries = [entry if entry is not None else name]
        self.project.configure_hooks(redirect_outputs={"programs": {"locations": {f"{folder}/": entries}}})
        return program


class BashRedirectTest(BashRedirectCase):
    def test_chrome_screenshot_goes_to_the_project_tmp(self):
        command = f'"{CHROME}" --headless --screenshot="C:\\Users\\Public\\dash.png" http://127.0.0.1:8765/'
        updated = self.bash(command)["hookSpecificOutput"]["updatedInput"]["command"]
        target = self.share_path(f"{TARGET}/chrome/dash.png")
        self.assertEqual(updated, f"\"{CHROME}\" --headless --screenshot='{target}' http://127.0.0.1:8765/")

    def test_rewritten_command_passes_the_path_unchanged(self):
        command = f"{CHROME.replace(' ', '\\ ')} --print-to-pdf=C:/Users/Public/page.pdf x.html"
        updated = self.bash(command)["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn(f"--print-to-pdf={self.share_path(f'{TARGET}/chrome/page.pdf')}", shlex.split(updated))

    def test_edge_is_redirected_too(self):
        output = self.bash(f"'{EDGE}' --headless --screenshot='C:\\Users\\Public\\x.png' http://x/")
        updated = output["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn(self.share_path(f"{TARGET}/msedge/x.png"), updated)

    def test_redirect_is_reported(self):
        output = self.bash(f"'{CHROME}' --screenshot='C:\\Users\\Public\\x.png' http://x/")
        self.assertIn("C:\\Users\\Public\\x.png", output["systemMessage"])
        self.assertIn("tmp/chrome/x.png", output["systemMessage"])
        self.assertIn("tmp/chrome/x.png", output["hookSpecificOutput"]["additionalContext"])

    def test_listed_program_in_its_location_runs_without_a_prompt(self):
        output = self.bash(f"'{CHROME}' --screenshot='C:\\Users\\Public\\x.png' http://x/")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertIn(self.share_path(f"{TARGET}/chrome/x.png"), output["updatedInput"]["command"])

    def test_program_after_a_wrapper_runs_without_a_prompt(self):
        command = f"timeout 60 '{CHROME}' --headless --screenshot='C:\\Users\\Public\\x.png' http://x/"
        self.assertEqual(self.bash(command)["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_target_directory_is_created(self):
        self.bash(f"'{CHROME}' --screenshot='C:\\Users\\Public\\x.png' http://x/")
        self.assertTrue(os.path.isdir(self.project.path(f"{TARGET}/chrome")))

    def test_linux_program_gets_the_plain_project_path(self):
        updated = self.bash("/usr/bin/chromium --screenshot=/home/other/x.png http://x/", distro=None)
        words = shlex.split(updated["hookSpecificOutput"]["updatedInput"]["command"])
        self.assertIn(f"--screenshot={self.project.path(f'{TARGET}/chromium/x.png')}", words)

    def test_program_in_a_configured_location_runs_without_a_prompt(self):
        program = self.outside_program("Fake Browser")
        output = self.bash(f"'{program}' --screenshot=/home/other/x.png http://x/")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        words = shlex.split(output["updatedInput"]["command"])
        self.assertIn(self.project.path(f"{TARGET}/Fake Browser/x.png"), words[1])

    def test_bare_name_is_looked_up_in_path(self):
        program = self.outside_program("fakebrowser")
        output = self.bash("fakebrowser --screenshot=/home/other/x.png http://x/",
                           path=f"{os.path.dirname(program)}:/usr/bin:/bin")
        updated = output["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn(self.project.path(f"{TARGET}/fakebrowser/x.png"), updated)


class ProgramOptionsTest(BashRedirectCase):
    def test_own_options_are_redirected(self):
        program = self.outside_program("tool", {"tool": ["--out"]})
        output = self.bash(f"{program} --out=/home/other/x.txt")["hookSpecificOutput"]
        words = shlex.split(output["updatedInput"]["command"])
        self.assertIn(f"--out={self.project.path(f'{TARGET}/tool/x.txt')}", words)

    def test_own_options_replace_the_shared_ones(self):
        program = self.outside_program("tool", {"tool": ["--out"]})
        output = self.bash(f"{program} --screenshot=/home/other/x.png")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")
        self.assertNotIn("updatedInput", output)

    def test_plain_names_keep_the_shared_options(self):
        program = self.outside_program("tool")
        output = self.bash(f"{program} --out=/home/other/x.txt")["hookSpecificOutput"]
        self.assertNotIn("updatedInput", output)

    def test_broken_entry_prompts(self):
        for entry in [{"tool": "--out"}, {"tool": ["--out"], "other": ["--x"]}, 5]:
            with self.subTest(entry=entry):
                program = self.outside_program("tool", entry)
                output = self.bash(f"{program} --out=/home/other/x.txt")["hookSpecificOutput"]
                self.assertEqual(output["permissionDecision"], "ask")
                self.assertIn("programs.locations", output["permissionDecisionReason"])


class BashNoRedirectTest(BashRedirectCase):
    def test_program_without_outside_paths_stays_silent(self):
        self.assertIsNone(self.bash(f"'{CHROME}' --headless --dump-dom http://x/"))

    def test_other_windows_path_of_the_program_still_prompts(self):
        output = self.bash(f"'{CHROME}' --user-data-dir='C:\\Users\\x' http://x/")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")
        self.assertIn("C:\\Users\\x", output["permissionDecisionReason"])

    def test_program_named_in_a_script_text_is_left_alone(self):
        command = f"python3 - <<'EOF'\nprint(\"'{CHROME}' --screenshot=C:/Users/Public/x.png\")\nEOF"
        output = self.bash(command)
        self.assertNotIn("updatedInput", (output or {}).get("hookSpecificOutput", {}))

    def test_program_path_as_an_argument_prompts(self):
        output = self.bash(f"cp chrome.exe '{CHROME}'")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")

    def test_unlisted_program_in_a_location_prompts(self):
        output = self.bash("'/mnt/c/Program Files/Tool/tool.exe' --headless")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")

    def test_listed_name_outside_its_location_prompts(self):
        output = self.bash("/home/other/chrome.exe --headless")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")

    def test_bare_name_of_an_unlisted_program_is_not_redirected(self):
        output = self.bash("tool.exe --screenshot='C:\\Users\\Public\\x.png'")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")
        self.assertNotIn("updatedInput", output)

    def test_output_inside_the_project_is_left_alone(self):
        self.assertIsNone(self.bash(f"'{CHROME}' --screenshot='{self.share_path(f'{TARGET}/x.png')}' http://x/"))

    def test_outside_wsl_the_windows_program_is_not_redirected(self):
        output = self.bash(f"'{CHROME}' --screenshot='C:\\Users\\Public\\x.png' http://x/", distro=None)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertNotIn("updatedInput", output["hookSpecificOutput"])

    def test_broken_programs_config_prompts(self):
        self.project.configure_hooks(redirect_outputs={"programs": {"locations": ["/mnt/?/"]}})
        output = self.bash(f"'{CHROME}' --screenshot='C:\\Users\\Public\\x.png' http://x/")["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")
        self.assertIn("programs.locations", output["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
