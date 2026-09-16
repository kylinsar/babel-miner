"""Plugin registry. A plugin describes a project; the kernel owns keys and broadcast."""
from importlib import import_module

_PLUGINS = {
    'babel': 'plugins.babel_plugin',
    'parsec': 'plugins.parsec_plugin',
    'hashcats': 'plugins.hashcats_plugin',
}


class Plugin:
    """Project adapter. Must not sign or send_raw_transaction."""
    name = ''
    title = ''
    engine = 'native'  # native stdin/stdout worker, or upstream supervisor
    native_symbol = 'ETH'
    default_rpc = ''
    recommended_rpc = ''
    chain_id = 0
    contract = ''
    ledger_app = ''
    requires_linux = False
    menu_live_hint = ''

    def run_cli(self, argv):
        raise NotImplementedError

    def build(self, backend):
        raise NotImplementedError

    def env_lines(self):
        return []


def available_plugins():
    return list(_PLUGINS)


def load_plugin(name):
    if name not in _PLUGINS:
        raise KeyError(f'未知插件：{name}；可选 {", ".join(available_plugins())}')
    mod = import_module(_PLUGINS[name])
    return mod.PLUGIN
