"""PARSEC adapter: native Keccak worker, Arc USDC gas, mint(nonce) value=0."""
import subprocess
import sys
from pathlib import Path

import parsec as p
from core.plugin import Plugin

ROOT = Path(__file__).resolve().parents[1]


class ParsecPlugin(Plugin):
    name = 'parsec'
    title = 'Parsec'
    engine = 'native'
    native_symbol = 'USDC'
    default_rpc = p.RPC
    recommended_rpc = 'https://rpc.mainnet.arc.io'
    chain_id = p.CHAIN
    contract = str(p.CONTRACT)
    ledger_app = 'parsec-miner'
    menu_live_hint = '只支付网络 Gas；不会支付发现铸造价。金额单位是 Arc 原生 USDC，不是 ETH。不实现 Buy / Combine。'

    def run_cli(self, argv):
        sys.argv = [str(ROOT / 'parsec.py'), *argv]
        p.main()

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


PLUGIN = ParsecPlugin()
