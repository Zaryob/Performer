"""Installed packages and checkouts identify the actual source contents."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from performer import provenance


class SourceIdentityTests(unittest.TestCase):
    def test_fingerprint_stable_across_checkout_and_package_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/'checkout', Path(tmp)/'package'
            for root in (a, b):
                (root/'collector/performer').mkdir(parents=True)
                (root/'collector/performer/main.py').write_text('print("hello")\n')
            (a/'collector/performer/_build.py').write_text('BUILD_COMMIT = None')
            (b/'collector/performer/_build.py').write_text('BUILD_COMMIT = "abcd"')
            self.assertEqual(provenance.source_fingerprint(a), provenance.source_fingerprint(b))
            (b/'collector/performer/main.py').write_text('print("changed")\n')
            self.assertNotEqual(provenance.source_fingerprint(a), provenance.source_fingerprint(b))

    def test_probe_or_profile_change_changes_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = provenance.source_fingerprint(root)
            for folder, name in (('probes', 'oncpu.bt'), ('schema', 'manifest.schema.json'), ('collector/profiles', 'light.yaml')):
                (root/folder).mkdir(parents=True, exist_ok=True)
                (root/folder/name).write_text('changed')
                current = provenance.source_fingerprint(root)
                self.assertNotEqual(previous, current)
                previous = current

    def test_packaged_commit_does_not_depend_on_git_installation(self):
        with patch('performer.provenance.BUILD_COMMIT', 'a'*40), patch('performer.provenance.subprocess.run') as git:
            self.assertEqual(provenance.source_commit(Path('/missing')), 'a'*40)
            git.assert_not_called()

    def test_missing_git_is_unknown_not_an_invented_commit(self):
        with tempfile.TemporaryDirectory() as tmp, patch('performer.provenance.BUILD_COMMIT', None):
            root = Path(tmp)
            self.assertIsNone(provenance.source_commit(root))
            (root/'.git').mkdir()
            with patch('performer.provenance.subprocess.run', side_effect=FileNotFoundError):
                self.assertIsNone(provenance.source_commit(root))
            with patch('performer.provenance.subprocess.run', return_value=Mock(returncode=0, stdout='a'*40)):
                self.assertEqual(provenance.source_commit(root), 'a'*40)
            with patch('performer.provenance.subprocess.run', return_value=Mock(returncode=0, stdout='invalid')):
                self.assertIsNone(provenance.source_commit(root))
