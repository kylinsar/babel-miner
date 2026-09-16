"""Unified CLI: pow.py <plugin> <command> ..."""
import argparse
import sys
from pathlib import Path

from core.plugin import available_plugins, load_plugin
from core.safety import SafetyError, rpc_error_label, log

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        names = ', '.join(available_plugins())
        print(f'用法: pow.py <plugin> <命令>...\n插件: {names}\n也可用 babel.py / parsec.py 直接跑对应项目。')
        return 0
    name = argv[0]
    rest = argv[1:]
    try:
        plugin = load_plugin(name)
        return plugin.run_cli(rest)
    except SafetyError as exc:
        log(f'停止：{exc}')
        return 1
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        log(f'停止：{rpc_error_label(exc)}；请检查 RPC、依赖和服务器环境，未自动重试交易')
        return 1


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
