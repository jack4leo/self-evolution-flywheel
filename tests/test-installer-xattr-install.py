#!/usr/bin/env python3
"""Isolated PTY install with real attributes; Darwin hides every os xattr API."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load('xattr_install_helper', ROOT / '.agent-maintenance/installer-transaction.py')
PTY = load('xattr_install_pty', ROOT / 'tests/test-installer-confirmation.py')


class XattrInstallTests(unittest.TestCase):
    def test_confirmed_install_and_retry_preserve_real_attributes(self):
        with tempfile.TemporaryDirectory(prefix='xattr-pty-install-') as directory:
            area = Path(directory).resolve()
            project = area / 'project'
            project.mkdir()
            subprocess.run(['git', 'init', '-q', str(project)], check=True)
            agents = project / 'AGENTS.md'
            settings = project / '.claude/settings.json'
            settings.parent.mkdir()
            agents.write_bytes(b'# Keep original instructions\n')
            settings.write_bytes(b'{"env":{"KEEP":"yes"}}\n')
            attrs = {'user.flywheel.binary': b'\x00\x01\xff', 'user.flywheel.empty': b''}
            if sys.platform == 'darwin':
                attrs['com.apple.quarantine'] = b'0081;65f2;Safari;test-fixture'
            for path in (agents, settings):
                fd = os.open(path, os.O_RDWR)
                try:
                    for name, value in attrs.items():
                        M.xattr_set(fd, name, value)
                finally:
                    os.close(fd)
            modules = area / 'modules'
            modules.mkdir()
            # Every Python subprocess sees the same old-build surface. Linux
            # retains its real API; only the actual Darwin libc can replace it.
            (modules / 'sitecustomize.py').write_text(
                'import os, sys\n'
                'if sys.platform == "darwin":\n'
                '    for name in ("listxattr", "getxattr", "setxattr", "removexattr"):\n'
                '        if hasattr(os, name): delattr(os, name)\n'
            )
            command = shlex.join([
                'env', 'PYTHONPATH=' + str(modules), 'PYTHONDONTWRITEBYTECODE=1',
                'bash', str(ROOT / 'install.sh'), '--target', str(project),
            ])
            code, output, sent = PTY.terminal(command, ['y', 'y'])
            self.assertEqual((code, sent), (0, 2), output.decode(errors='replace'))
            self.assertTrue(agents.read_bytes().startswith(PTY.BEGIN))
            self.assertEqual(json.loads(settings.read_bytes())['env'], {'KEEP': 'yes'})
            snapshots = {path: path.read_bytes() for path in (agents, settings)}
            for path in (agents, settings):
                backups = list(path.parent.glob('.' + path.name + '.confirm-backup-*'))
                self.assertEqual(len(backups), 1)
                for candidate in [path, *backups]:
                    fd = os.open(candidate, os.O_RDONLY)
                    try:
                        self.assertEqual(set(M.xattr_list(fd)), set(attrs))
                        for name, value in attrs.items():
                            self.assertEqual(M.xattr_get(fd, name), value)
                    finally:
                        os.close(fd)
            code, output, sent = PTY.terminal(command, [])
            self.assertEqual((code, sent), (0, 0), output.decode(errors='replace'))
            for path, before in snapshots.items():
                self.assertEqual(path.read_bytes(), before)
                fd = os.open(path, os.O_RDONLY)
                try:
                    for name, value in attrs.items():
                        self.assertEqual(M.xattr_get(fd, name), value)
                finally:
                    os.close(fd)


if __name__ == '__main__':
    unittest.main(verbosity=2)
