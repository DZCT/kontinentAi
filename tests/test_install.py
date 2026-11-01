import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import configure
import install


class FreshInstallTests(unittest.TestCase):
    def test_config_has_no_injected_lines(self):
        valid = ('123456:' + 'a' * 30, '123456', 'sk-kont-example123')
        text = configure.make_config(*valid)
        self.assertIn('DEFAULT_MODEL=deepseek-v4.1-flash\n', text)
        self.assertEqual(len(text.splitlines()), 7)
        for token, owner, key in [('', valid[1], valid[2]), (valid[0], '-4', valid[2]),
                                  (valid[0], valid[1], 'bad\nSECRET=value')]:
            with self.assertRaises(ValueError):
                configure.make_config(token, owner, key)

    def test_existing_config_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / '.env'
            target.write_text('old-secret')
            with patch.object(configure.getpass, 'getpass') as prompt:
                with self.assertRaises(ValueError):
                    configure.configure(target)
                prompt.assert_not_called()
            self.assertEqual(target.read_text(), 'old-secret')

    def test_installer_refuses_existing_bot_before_system_changes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / 'installed'
            target.mkdir()
            with patch.object(install, 'TARGET', target), patch.object(install, 'SERVICE', root / 'service'), \
                 patch.object(install, 'run') as run:
                with self.assertRaises(RuntimeError):
                    install.install(root)
                run.assert_not_called()

    def test_release_source_is_complete(self):
        install.validate_source(Path(__file__).resolve().parents[1])
