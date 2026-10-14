"""Tests for the root cleanup and self-contained CLI API.

PowerShell 5.1 execution must still be tested on Windows: Linux tests only check
static bootstrap invariants, not the actual Windows process lifecycle.
"""
from __future__ import annotations
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from src import cli
from src.server import app
from src.settings import ROOT


class LauncherTests(unittest.TestCase):
    def test_root_is_small_and_all_docs_are_reachable(self):
        children = {p.name for p in ROOT.iterdir() if not p.name.startswith('.')}
        for removed in ('publish.py', 'sync.py', 'verify.py', 'start.sh', 'tools', 'docs'):
            self.assertNotIn(removed, children)
        for required in ('run.bat', 'README.md', 'LICENSE', 'config', 'src', 'tests', 'xhs', 'zhihu'):
            self.assertIn(required, children)
        for name in ('FIRST_LOGIN', 'ARCHITECTURE', 'UPDATING', 'BATCH_PUBLISHING', 'VALIDATION'):
            self.assertTrue((ROOT / 'config/docs' / (name + '.md')).is_file())

    def test_bootstrap_and_log_routes(self):
        bat = (ROOT / 'run.bat').read_text(encoding='utf-8')
        ps = (ROOT / 'src/launch.ps1').read_text(encoding='utf-8')
        self.assertIn('src\\launch.ps1', bat)
        self.assertNotIn(' -m venv ', bat)
        for expected in ('-m venv', 'requirements.sha256', '-m pip install',
                         'pip check', 'install.log', 'startup-error.html',
                         'Test-PythonImport', '127.0.0.1'):
            self.assertIn(expected, ps)
        self.assertIn("$ErrorActionPreference = 'Continue'", ps)
        self.assertNotIn('首次', bat + ps)
        self.assertNotIn('启动失败', bat + ps)
        self.assertIn('Zhihu image support is unavailable', ps)

    def test_cli_help_is_english(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            with self.assertRaises(SystemExit) as result:
                cli.main(['--help'])
        self.assertEqual(result.exception.code, 0)
        output = stream.getvalue()
        for command in ('publish', 'sync', 'verify'):
            self.assertIn(command, output)
        self.assertNotIn('发布指定', output)

    def test_cli_preview_never_submits(self):
        mock_manager = type('Manager', (), {
            'catalog': lambda self: [{'folder': 'test', 'uid': 'abc', 'title': 'Local note', 'image_count': 1}],
            'publish': lambda self, *_: (_ for _ in ()).throw(AssertionError('must not publish'))
        })()
        with patch('src.workspace.backend', return_value=mock_manager):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(['publish', 'test', '--platform', 'xhs'])
        self.assertEqual(code, 0)
        self.assertIn('Preview only', output.getvalue())

    def test_health_endpoint(self):
        response = TestClient(app, base_url='http://127.0.0.1:8767').get('/api/health')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['service'], 'BlogNotes')
        self.assertEqual(data['root'], str(ROOT.resolve()))
        self.assertIn('|', data['build_id'])
        self.assertEqual(len(data['ui_revision']), 12)
        self.assertIn('no-store', TestClient(app, base_url='http://127.0.0.1:8767').get('/').headers['cache-control'])



if __name__ == '__main__':
    unittest.main()
