"""Support for the guard tests: each test gets a throwaway project under tests/.tmp/ with a copy of the hook at
.claude/hooks/project-guard/guard.py and, unless the test case sets `config = False`, the fixture config as the
project's .claude/guard.yml and config/tests.yml. The guard's own guard.yml is not copied, so the DEFAULTS apply."""
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
GUARD_DIR = os.path.dirname(TESTS_DIR)
HOST_PROJECT = os.path.realpath(os.path.join(GUARD_DIR, "..", "..", ".."))     # the project the guard is installed in
TMP_DIR = os.path.join(TESTS_DIR, ".tmp")
HOOK = ".claude/hooks/project-guard/guard.py"                                   # in a throwaway project
TARGET = ".claude/hooks/project-guard/tmp"                                      # the default redirect target there
FIXTURES = {".claude/guard.yml": "fixture-guard.yml", "config/tests.yml": "fixture-tests.yml"}


class Project:
    """A throwaway project directory with the hook in it."""

    def __init__(self, root):
        self.root = root

    def path(self, relative):
        """The absolute path of a project-relative path."""
        return os.path.join(self.root, relative)

    def write(self, relative, text):
        """Write a project file, creating its directories."""
        path = self.path(relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def run(self, command, input, env=None):
        """Run command in the project with CLAUDE_PROJECT_DIR set to it; env adds variables, None values remove."""
        environment = dict(os.environ, CLAUDE_PROJECT_DIR=self.root)
        for name, value in (env or {}).items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        return subprocess.run(command, input=input, capture_output=True, text=True, cwd=self.root, env=environment,
                              check=False)

    def configure_hooks(self, **values):
        """Set keys of the hooks section in .claude/guard.yml."""
        path = self.path(".claude/guard.yml")
        with open(path, encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        config.setdefault("hooks", {}).update(values)
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle)


class ProjectTestCase(unittest.TestCase):
    """A test case with self.project, a fresh throwaway project per test."""
    config = True

    def setUp(self):
        os.makedirs(TMP_DIR, exist_ok=True)
        root = os.path.realpath(tempfile.mkdtemp(dir=TMP_DIR))
        self.addCleanup(shutil.rmtree, root)
        self.project = Project(root)
        os.makedirs(self.project.path(os.path.dirname(HOOK)))
        shutil.copy(os.path.join(GUARD_DIR, "guard.py"), self.project.path(HOOK))
        if self.config:
            for target, fixture in FIXTURES.items():
                os.makedirs(os.path.dirname(self.project.path(target)), exist_ok=True)
                shutil.copy(os.path.join(TESTS_DIR, fixture), self.project.path(target))
