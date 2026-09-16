import os
import unittest
from unittest.mock import patch

from core.keys import read_private_key
from core.safety import SafetyError


class KeyInputTests(unittest.TestCase):
    def test_reads_private_key_env(self):
        secret = 'ab' * 32
        with patch.dict(os.environ, {'PRIVATE_KEY': secret}, clear=False):
            self.assertEqual(read_private_key('PRIVATE_KEY'), secret)

    def test_prefers_project_env(self):
        with patch.dict(os.environ, {
            'BABEL_PRIVATE_KEY': 'cd' * 32,
            'PRIVATE_KEY': 'ab' * 32,
        }, clear=False):
            self.assertEqual(read_private_key('BABEL_PRIVATE_KEY', 'PRIVATE_KEY'), 'cd' * 32)

    def test_rejects_bad_env_key(self):
        with patch.dict(os.environ, {'PRIVATE_KEY': 'not-a-key'}, clear=False):
            with self.assertRaises(SafetyError):
                read_private_key('PRIVATE_KEY')

    def test_env_value_not_logged(self):
        secret = 'ef' * 32
        with patch.dict(os.environ, {'PRIVATE_KEY': secret}, clear=False), \
             patch('core.keys.log') as log:
            read_private_key('PRIVATE_KEY')
        msg = log.call_args[0][0]
        self.assertIn('PRIVATE_KEY', msg)
        self.assertNotIn(secret, msg)


if __name__ == '__main__':
    unittest.main()
