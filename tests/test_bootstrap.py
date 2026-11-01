"""Exercise the shell installer in a pseudo-terminal with all mutations mocked."""
import fcntl
import io
import os
from pathlib import Path
import pty
import subprocess
import tarfile
import tempfile
import termios
import unittest


class BootstrapTests(unittest.TestCase):
    def run_script(self, existing=False, download_fails=False):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bins = root / 'bin'
            bins.mkdir()
            log = root / 'calls'
            archive = root / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as tf:
                info = tarfile.TarInfo('kontinentAi-main/install.py')
                content = b'# installation fixture\n'
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
            commands = {
                'sudo': 'if [[ "$1" == "-v" ]]; then exit 0; fi\nexec "$@"\n',
                'apt-get': 'echo apt-get >> "$TEST_LOG"\n',
                'systemctl': 'exit 0\n',
                'python3': 'echo "python3 $*" >> "$TEST_LOG"\n',
                'curl': ('exit 22\n' if download_fails else
                         'while [[ $# -gt 0 ]]; do if [[ "$1" == "--output" ]]; then cp "$TEST_ARCHIVE" "$2"; exit; fi; shift; done\nexit 1\n'),
            }
            for name, body in commands.items():
                path = bins / name
                path.write_text('#!/bin/bash\nset -e\n' + body)
                path.chmod(0o755)
            target = root / 'installed'
            if existing:
                target.mkdir()
            text = (Path(__file__).resolve().parents[1] / 'install.sh').read_text()
            text = text.replace('/opt/kontinent-bot', str(target))
            text = text.replace('/etc/systemd/system/kontinent-bot.service', str(root / 'service'))
            script = root / 'install.sh'
            script.write_text(text)
            master, slave = pty.openpty()
            def session():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            env = dict(os.environ, PATH=str(bins) + ':' + os.environ['PATH'],
                       TEST_LOG=str(log), TEST_ARCHIVE=str(archive), TMPDIR=str(root))
            try:
                p = subprocess.Popen(['bash', str(script)], stdin=slave, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, preexec_fn=session)
                out, err = p.communicate(timeout=15)
            finally:
                os.close(master)
                os.close(slave)
            calls = log.read_text() if log.exists() else ''
            leftovers = list(root.glob('kontinentAi-install-*'))
            return p.returncode, calls, (out + err).decode(), leftovers

    def test_fresh_install_reattaches_terminal_and_cleans_temp_source(self):
        code, calls, output, leftovers = self.run_script()
        self.assertEqual(code, 0, output)
        self.assertIn('python3 ', calls)
        self.assertIn('/source/install.py', calls)
        self.assertEqual(leftovers, [])

    def test_existing_install_stops_before_apt_or_python(self):
        code, calls, output, leftovers = self.run_script(existing=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, '')
        self.assertIn('already installed', output)

    def test_download_failure_never_executes_python(self):
        code, calls, output, leftovers = self.run_script(download_fails=True)
        self.assertNotEqual(code, 0)
        self.assertNotIn('python3 ', calls)
        self.assertEqual(leftovers, [])
