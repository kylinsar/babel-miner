import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.plugin import available_plugins, load_plugin
from core.ledger import Ledger
from core.safety import SafetyError, parse_cuda_devices, amount, fmt


class PluginRegistryTests(unittest.TestCase):
    def test_known_plugins(self):
        self.assertEqual(available_plugins(), ['babel', 'parsec', 'hashcats'])
        babel = load_plugin('babel')
        self.assertEqual(babel.name, 'babel')
        self.assertEqual(babel.engine, 'native')
        self.assertEqual(babel.chain_id, 5042)
        parsec = load_plugin('parsec')
        self.assertEqual(parsec.name, 'parsec')
        self.assertEqual(parsec.engine, 'native')
        self.assertEqual(parsec.chain_id, 5042)
        self.assertEqual(parsec.contract, '0x631f96907126Ae23313Ec8DF489da0879AACfC98')
        cats = load_plugin('hashcats')
        self.assertEqual(cats.engine, 'upstream')
        self.assertEqual(cats.chain_id, 4663)
        with self.assertRaises(KeyError):
            load_plugin('unknown')

    def test_cuda_devices_and_amount(self):
        self.assertEqual(parse_cuda_devices('0，1、2'), ['0', '1', '2'])
        self.assertEqual(amount('0.13'), 130000000000000000)
        self.assertEqual(fmt(130412520020359920), '0.13041252002035992')

    def test_shared_ledger_cost_override(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(Path, 'home', return_value=Path(tmp.name)):
            ledger = Ledger('0x' + '11' * 20, app='pow-test', chain_id=1, contract='0xabc')
            tx = {'nonce': 0, 'gas': 100, 'maxFeePerGas': 2}
            ledger.reserve(tx, budget=1000, max_txs=2, cost=50)
            self.assertEqual(ledger.records()[0]['reserved'], 50)
            with self.assertRaises(SafetyError):
                ledger.reserve(dict(tx, nonce=1), budget=1000, max_txs=2, cost=1000)
            ledger.close()


class HashcatsDispatchTests(unittest.TestCase):
    def setUp(self):
        self.control = Mock()
        self.control.SafetyError = type('UpstreamSafety', (Exception,), {})
        self.control.run_mode = Mock()
        self.control.install = Mock()
        self.control.gpu_inventory = Mock(return_value={0: {'uuid': 'u', 'cc': '8.9'}})
        self.control.parse_devices = Mock(return_value=[0])
        self.control.build_binary = Mock(return_value=(Path(tempfile.mkdtemp()), Path('/tmp/keccak_miner')))
        self.plugin = load_plugin('hashcats')

    def test_maps_modes_to_run_mode(self):
        work = Path(tempfile.mkdtemp())
        with patch('plugins.hashcats_plugin._load_control', return_value=self.control), \
             patch.dict(os.environ, {'HASHCATS_WORKDIR': str(work)}):
            for mode in ('bench', 'check', 'dry', 'live'):
                self.control.run_mode.reset_mock()
                self.plugin.run_cli([mode, '--rpc', 'https://rpc.example', '--devices', '0,1'])
                self.control.run_mode.assert_called_once_with(
                    work.resolve(), 'https://rpc.example', '0,1', mode)

    def test_install_and_build_flags(self):
        work = Path(tempfile.mkdtemp())
        with patch('plugins.hashcats_plugin._load_control', return_value=self.control), \
             patch.dict(os.environ, {'HASHCATS_WORKDIR': str(work)}):
            self.plugin.run_cli(['install'])
            self.control.install.assert_called_once_with(work.resolve())
            self.plugin.run_cli(['--build', 'cuda'])
            self.control.build_binary.assert_called()
            with self.assertRaises(SafetyError):
                self.plugin.run_cli(['--build', 'cpu'])

    def test_live_uses_env_key_reader(self):
        work = Path(tempfile.mkdtemp())
        with patch('plugins.hashcats_plugin._load_control', return_value=self.control), \
             patch.dict(os.environ, {'HASHCATS_WORKDIR': str(work), 'PRIVATE_KEY': 'aa' * 32}):
            self.plugin.run_cli(['live', '--rpc', 'https://rpc.example', '--devices', '0'])
            self.assertEqual(self.control.read_key(), 'aa' * 32)

    def test_upstream_safety_becomes_kernel_safety(self):
        self.control.run_mode.side_effect = self.control.SafetyError('请在 Linux GPU 服务器上运行菜单')
        with patch('plugins.hashcats_plugin._load_control', return_value=self.control):
            with self.assertRaisesRegex(SafetyError, 'Linux GPU'):
                self.plugin.run_cli(['bench', '--devices', 'all'])

    def test_pow_menu_hashcats_no_longer_defers(self):
        root = Path(__file__).resolve().parents[1]
        text = root.joinpath('pow-menu.sh').read_text()
        self.assertNotIn('请用 hashcats-menu', text)
        self.assertIn('1 系统信息  2 设置  3 安装  4 编译', text)
        self.assertIn('run "$mode" --rpc "$rpc" --devices "$devices"', text)
        state = tempfile.TemporaryDirectory(dir=str(root))
        self.addCleanup(state.cleanup)
        env = os.environ.copy()
        env['POW_PLUGIN'] = 'hashcats'
        env['POW_STATE_DIR'] = state.name
        p = subprocess.run(['bash', str(root / 'pow-menu.sh')], input='0\n', capture_output=True, text=True, timeout=5, env=env, encoding='utf-8', errors='replace')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Hashcats | CUDA GPU=all', p.stdout)

    def test_pow_menu_parsec_defaults(self):
        root = Path(__file__).resolve().parents[1]
        text = root.joinpath('pow-menu.sh').read_text()
        self.assertIn('3 Parsec', text)
        self.assertIn('parsec', text)
        state = tempfile.TemporaryDirectory(dir=str(root))
        self.addCleanup(state.cleanup)
        env = os.environ.copy()
        env['POW_PLUGIN'] = 'parsec'
        env['POW_STATE_DIR'] = state.name
        p = subprocess.run(['bash', str(root / 'pow-menu.sh')], input='0\n', capture_output=True, text=True, timeout=5, env=env, encoding='utf-8', errors='replace')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Parsec | CPU进程=1', p.stdout)

    def test_pow_menu_settings_persist_project(self):
        root = Path(__file__).resolve().parents[1]
        state = tempfile.TemporaryDirectory(dir=str(root))
        self.addCleanup(state.cleanup)
        env = os.environ.copy()
        env.pop('POW_PLUGIN', None)
        env['POW_STATE_DIR'] = state.name
        first = subprocess.run(['bash', str(root / 'pow-menu.sh')], input='0\n', capture_output=True, text=True, timeout=5, env=env, encoding='utf-8', errors='replace')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('1 系统信息  2 设置  3 安装  4 编译', first.stdout)
        self.assertIn('Babel | CPU进程=1', first.stdout)
        saved = subprocess.run(
            ['bash', str(root / 'pow-menu.sh')],
            input='2\n2\n\n\n\n\n\n\n0\n',
            capture_output=True, text=True, timeout=5, env=env, encoding='utf-8', errors='replace',
        )
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertIn('已保存到', saved.stdout)
        self.assertIn('Hashcats | CUDA GPU=all', saved.stdout)
        again = subprocess.run(['bash', str(root / 'pow-menu.sh')], input='0\n', capture_output=True, text=True, timeout=5, env=env, encoding='utf-8', errors='replace')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn('Hashcats | CUDA GPU=all', again.stdout)


if __name__ == '__main__':
    unittest.main()
