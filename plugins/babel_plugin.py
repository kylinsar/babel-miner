"""Babel adapter: native Keccak worker, Arc USDC gas, value=0 lay()."""
import subprocess
import sys
from pathlib import Path

import babel as b
from core.plugin import Plugin

ROOT = Path(__file__).resolve().parents[1]


class BabelPlugin(Plugin):
    name = 'babel'
    title = 'Babel'
    engine = 'native'
    native_symbol = 'USDC'
    default_rpc = b.RPC
    recommended_rpc = 'https://rpc.mainnet.arc.io'
    chain_id = b.CHAIN
    contract = str(b.CONTRACT)
    ledger_app = 'babel-miner'
    menu_live_hint = '只支付网络 Gas；不会支付砖块铸造价。金额单位是 Arc 原生 USDC，不是 ETH。'

    def run_cli(self, argv):
        sys.argv = [str(ROOT / 'babel.py'), *argv]
        b.main()

    def build(self, backend):
        subprocess.check_call(['bash', str(ROOT / 'build.sh'), backend], cwd=ROOT)

    def env_lines(self):
        lines = []
        for cmd in (['python3', '--version'], ['bash', '-lc', 'command -v c++ && command -v nvcc && nvcc --version && nvidia-smi -L']):
            try:
                lines.append(subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip())
            except Exception as exc:
                lines.append(str(exc))
        return lines


PLUGIN = BabelPlugin()
