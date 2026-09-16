"""Hashcats adapter: supervised upstream miner, Robinhood ETH mint price + gas."""
import argparse
import importlib.util
import os
import shutil
import sys
from pathlib import Path

from core.plugin import Plugin
from core.safety import SafetyError

ROOT = Path(__file__).resolve().parents[1]
COLLECTION = '0xca75df55cc9c476db27a7375d1fc8e794cf80721'
REV = 'bfee889dfa9d5c4daa7ad985c3c18f5b3bedef65'
MODES = ('bench', 'check', 'dry', 'live')


def _control_dir():
    env = os.environ.get('HASHCATS_CONTROL')
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.extend([
        ROOT.parent / 'hashcats-control',
        Path('/workspace/hashcats-control'),
        ROOT / 'vendor' / 'hashcats-control',
    ])
    for path in candidates:
        if (path / 'hashcats_control.py').is_file():
            return path
    return None


def _work_dir():
    return Path(os.environ.get('HASHCATS_WORKDIR', '/workspace/adssdadas')).resolve()


def _load_control():
    directory = _control_dir()
    if directory is None:
        raise SafetyError(
            '未找到 hashcats-control。请 clone 到仓库的上一级目录，或设置 HASHCATS_CONTROL：\n'
            'mkdir -p /workspace && cd /workspace && git clone https://github.com/kylinsar/hashcats-control.git '
            f'&& cd hashcats-control && git checkout --detach {REV}'
        )
    spec = importlib.util.spec_from_file_location('hashcats_control', directory / 'hashcats_control.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call(control, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except control.SafetyError as exc:
        raise SafetyError(str(exc)) from None


class HashcatsPlugin(Plugin):
    name = 'hashcats'
    title = 'Hashcats'
    engine = 'upstream'
    native_symbol = 'ETH'
    default_rpc = 'https://rpc.mainnet.chain.robinhood.com'
    recommended_rpc = 'https://rpc.mainnet.chain.robinhood.com'
    chain_id = 4663
    contract = COLLECTION
    ledger_app = 'hashcats-control'
    requires_linux = True
    menu_live_hint = '正式模式会支付铸造价 + Gas，单位 ETH。预算按 value + gas × maxFee 预留。'

    def run_cli(self, argv):
        control = _load_control()
        argv = list(argv)
        if not argv:
            sys.argv = [str(_control_dir() / 'hashcats_control.py')]
            try:
                raise SystemExit(control.main() or 0)
            except control.SafetyError as exc:
                raise SafetyError(str(exc)) from None
        if argv[0] == '--build':
            backend = argv[1] if len(argv) > 1 else 'cuda'
            self.build(backend)
            return 0
        parser = argparse.ArgumentParser(prog='pow.py hashcats')
        parser.add_argument('command', choices=['install', 'build', *MODES])
        parser.add_argument('--rpc', default=self.default_rpc)
        parser.add_argument('--devices', default='all')
        parser.add_argument('--backend', default='cuda')
        args = parser.parse_args(argv)
        work = _work_dir()
        if args.command == 'install':
            _call(control, control.install, work)
            return 0
        if args.command == 'build':
            self.build(args.backend, devices=args.devices)
            return 0
        if args.command in ('dry', 'live'):
            from core.keys import read_private_key
            control.read_key = lambda: read_private_key(
                'HASHCATS_PRIVATE_KEY', 'POW_PRIVATE_KEY', 'PRIVATE_KEY')
        _call(control, control.run_mode, work, args.rpc, args.devices, args.command)
        return 0

    def build(self, backend, devices=None):
        if backend != 'cuda':
            raise SafetyError('Hashcats GPU 模式需要 cuda')
        control = _load_control()
        work = _work_dir()
        inv = _call(control, control.gpu_inventory)
        ids = _call(control, control.parse_devices, devices or 'all', inv)
        directory, _binary = _call(control, control.build_binary, work, inv, ids)
        shutil.rmtree(directory)

    def env_lines(self):
        try:
            control = _load_control()
            return [control.command(['nvcc', '--version']), str(control.gpu_inventory())]
        except Exception as exc:
            return [str(exc)]


PLUGIN = HashcatsPlugin()
