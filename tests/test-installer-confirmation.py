#!/usr/bin/env python3
"""End-to-end confirmation tests, isolated projects and a real controlling PTY."""
import errno
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMMAND = 'bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude'
BEGIN = b'<!-- BEGIN SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING -->'


def terminal(command, answers, before_answer=None):
    pid, master = pty.fork()
    if pid == 0:
        os.execv('/bin/bash', ['bash', '-c', command])
    output = bytearray()
    sent = 0
    status = None
    deadline = time.monotonic() + 40
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b''
                output.extend(chunk)
                while output.count(b'[y/N] ') > sent and sent < len(answers):
                    if before_answer:
                        before_answer(sent, bytes(output))
                    os.write(master, answers[sent].encode() + b'\n')
                    sent += 1
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = child_status
                break
        if status is None:
            raise AssertionError('installer hung waiting for confirmation: ' + output.decode(errors='replace'))
        while select.select([master], [], [], .1)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        return os.waitstatus_to_exitcode(status), bytes(output), sent
    finally:
        if status is None:
            os.killpg(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.area = tempfile.TemporaryDirectory(prefix='flywheel-confirmation-')
        self.addCleanup(self.area.cleanup)
        self.project = Path(self.area.name) / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        self.agents = self.project / 'AGENTS.md'
        self.settings = self.project / '.claude/settings.json'
        self.original = b'# User instructions\n\nKeep every original byte.\r\n'
        self.config = {'env': {'KEEP': 'yes'}, 'permissions': {'allow': ['Read']},
                       'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo stop'}]}],
                                 'SessionStart': [{'matcher': 'startup', 'extra': 123,
                                                   'hooks': [{'type': 'command', 'command': 'echo hello', 'timeout': 7}]}]},
                       'unknown': {'nested': [False, None, 12]}}

    def command(self, no_hooks=False):
        return shlex.join(['bash', str(ROOT / 'install.sh'), '--target', str(self.project)] +
                          (['--no-hooks'] if no_hooks else []))

    def existing_settings(self):
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps(self.config, indent=2) + '\n')
        return self.settings.read_bytes()

    def assert_added_hook_only(self):
        actual = json.loads(self.settings.read_bytes())
        added = actual['hooks']['SessionStart'].pop()
        self.assertEqual(added, {'matcher': 'startup|resume|clear|compact|fork',
                                 'hooks': [{'type': 'command', 'command': COMMAND}]})
        self.assertEqual(actual, self.config)

    def test_agents_yes_and_retry(self):
        self.agents.write_bytes(self.original)
        code, output, sent = terminal(self.command(True), ['y'])
        self.assertEqual(sent, 1, output)
        self.assertEqual(code, 0, output)
        self.assertIn(b'--- AGENTS.md (current)', output)
        self.assertIn(b'+++ AGENTS.md (proposed)', output)
        self.assertTrue(self.agents.read_bytes().startswith(BEGIN))
        self.assertTrue(self.agents.read_bytes().endswith(self.original))
        first = self.agents.read_bytes()
        code, output, sent = terminal(self.command(True), [])
        self.assertEqual((code, sent), (0, 0), output)
        self.assertEqual(self.agents.read_bytes(), first)

    def test_claude_yes_exact_diff_and_retry(self):
        self.existing_settings()
        def inspect(_index, output):
            self.assertIn(b'--- .claude/settings.json (current)', output)
            self.assertIn(b'+++ .claude/settings.json (proposed)', output)
            self.assertIn(b'+', output)
            # This observer runs after the identity snapshot. Do not mutate
            # atime ourselves and turn a positive test into a metadata race.
            # Field preservation is also checked after the confirmed update.
            if hasattr(os, 'O_NOATIME'):
                fd = os.open(self.settings, os.O_RDONLY | os.O_NOATIME)
                try:
                    self.assertEqual(json.loads(os.read(fd, 1024 * 1024)), self.config)
                finally:
                    os.close(fd)
        code, output, sent = terminal(self.command(), ['y'], inspect)
        self.assertEqual(sent, 1, output)
        self.assertEqual(code, 0, output)
        self.assert_added_hook_only()
        first = self.settings.read_bytes()
        code, output, sent = terminal(self.command(), [])
        self.assertEqual((code, sent), (0, 0), output)
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertTrue((self.project / '.codex/hooks.json').exists())

    def test_refusal_default_and_non_yes(self):
        for answer in ['n', '', 'yes', 'Yess', ' y ', 'N']:
            with self.subTest(answer=answer):
                self.agents.write_bytes(self.original)
                code, output, sent = terminal(self.command(True), [answer])
                self.assertEqual((code, sent), (4, 1), output)
                self.assertEqual(self.agents.read_bytes(), self.original)
                self.assertIn(b'PRESERVED', output)
                self.assertTrue((self.project / 'prompts/bootstrap-project.md').exists())

    def test_claude_decline_independent_provider(self):
        original = self.existing_settings()
        for answer in ['n', '', 'yes', '\x04']:
            with self.subTest(answer=answer):
                code, output, sent = terminal(self.command(), [answer])
                self.assertEqual((code, sent), (0, 1), output)
                self.assertEqual(self.settings.read_bytes(), original)
        self.assertTrue((self.project / '.codex/hooks.json').exists())
        self.assertTrue((self.project / 'prompts/bootstrap-project.md').exists())
        self.assertIn(b'skipped', output)

    def test_claude_additive_json_shapes_and_numeric_spelling(self):
        self.settings.parent.mkdir()
        for original in [b'{}\n', b'{"env":{"A":"b"}}\n',
                         b'{"hooks":{"Stop":[]}}\n',
                         b'{"hooks":{"SessionStart":[]},"precise":0.123456789012345678901234567890}\n',
                         b'{\r\n "hooks": {"SessionStart": []}, "number": 1e1000\r\n}\r\n']:
            with self.subTest(original=original):
                self.settings.write_bytes(original)
                code, output, sent = terminal(self.command(), ['Y'])
                self.assertEqual((code, sent), (0, 1), output)
                updated = self.settings.read_bytes()
                # Every old byte stays in order, with precisely one inserted span.
                prefix = 0
                while prefix < len(original) and original[prefix] == updated[prefix]:
                    prefix += 1
                self.assertTrue(updated.endswith(original[prefix:]))
                actual = json.loads(updated)
                added = actual['hooks']['SessionStart'].pop()
                self.assertEqual(added['hooks'][0]['command'], COMMAND)
                expected = json.loads(original)
                expected.setdefault('hooks', {}).setdefault('SessionStart', [])
                self.assertEqual(actual, expected)

    def test_no_tty_never_reads_yes_from_stdin(self):
        self.agents.write_bytes(self.original)
        original_settings = self.existing_settings()
        result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--target', str(self.project)],
                                input=b'y\ny\n', stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True, timeout=30)
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertNotIn(b'[y/N]', result.stdout)
        self.assertIn(b'no interactive terminal', result.stdout)
        self.assertEqual(self.agents.read_bytes(), self.original)
        self.assertEqual(self.settings.read_bytes(), original_settings)
        self.assertTrue((self.project / '.codex/hooks.json').exists())

    def test_script_stdin_pipe_uses_controlling_terminal(self):
        self.agents.write_bytes(self.original)
        # Stream the actual entry point. A local clone shim avoids any network
        # or real target project; subsequent invocations use the real git.
        mockbin = Path(self.area.name) / 'bin'
        mockbin.mkdir()
        git = mockbin / 'git'
        git.write_text('#!/bin/bash\nif [[ "$1" == clone ]]; then\n'
                       'mkdir -p "${@: -1}"\ncp -R "$TEST_SOURCE"/. "${@: -1}"/\n'
                       'else\nexec /usr/bin/git "$@"\nfi\n')
        git.chmod(0o755)
        command = ('cd ' + shlex.quote(str(self.project)) + '; export TEST_SOURCE=' + shlex.quote(str(ROOT)) +
                   ' PATH=' + shlex.quote(str(mockbin)) + ':"$PATH"; cat ' +
                   shlex.quote(str(ROOT / 'install.sh')) + ' | bash -s -- --no-hooks')
        code, output, sent = terminal(command, ['y'])
        self.assertEqual((code, sent), (0, 1), output)
        self.assertTrue(self.agents.read_bytes().startswith(BEGIN))

    def test_confirmed_external_changes_preserved(self):
        for relative in ['AGENTS.md', '.claude/settings.json']:
            for replacement in [False, True]:
                with self.subTest(relative=relative, replacement=replacement):
                    path = self.project / relative
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(self.original if relative == 'AGENTS.md' else b'{}\n')
                    external = b'# External update\n' if relative == 'AGENTS.md' else b'{"external":true}\n'
                    def change(_index, _output):
                        if replacement:
                            temporary = path.with_name('external.tmp')
                            temporary.write_bytes(external)
                            temporary.replace(path)
                        else:
                            path.write_bytes(external)
                    code, output, sent = terminal(self.command(relative == 'AGENTS.md'), ['y'], change)
                    self.assertEqual(sent, 1, output)
                    self.assertEqual(path.read_bytes(), external)
                    self.assertNotEqual(code, 0, output)
                    # Reset to avoid another proposal in the next subcase.
                    path.unlink()

    def test_both_confirmations_are_independent(self):
        self.agents.write_bytes(self.original)
        self.existing_settings()
        code, output, sent = terminal(self.command(), ['n', 'y'])
        self.assertEqual((code, sent), (4, 2), output)
        self.assertEqual(self.agents.read_bytes(), self.original)
        self.assert_added_hook_only()

    def test_invalid_other_provider_preflight_before_any_confirmation(self):
        self.existing_settings()
        codex = self.project / '.codex/hooks.json'
        codex.parent.mkdir()
        codex.write_text('{"hooks":null}')
        code, output, sent = terminal(self.command(), [])
        self.assertEqual((code, sent), (3, 0), output)
        self.assertFalse(self.agents.exists())

    def test_redirected_diff_does_not_prompt_on_hidden_terminal(self):
        self.agents.write_bytes(self.original)
        output_file = Path(self.area.name) / 'redirected.log'
        code, output, sent = terminal(self.command(True) + ' > ' + shlex.quote(str(output_file)) + ' 2>&1', [])
        self.assertEqual((code, sent), (4, 0), output)
        self.assertEqual(self.agents.read_bytes(), self.original)
        self.assertIn(b'no interactive terminal', output_file.read_bytes())


if __name__ == '__main__':
    unittest.main()
