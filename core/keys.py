"""Private key input. Env is optional; never log the secret or write menu.conf."""
import getpass
import os
import re
import warnings
from core.safety import SafetyError, log

_KEY = re.compile(r'(?:0[xX])?[0-9a-fA-F]{64}')
ENV_NAMES = ('POW_PRIVATE_KEY', 'BABEL_PRIVATE_KEY', 'PARSEC_PRIVATE_KEY', 'HASHCATS_PRIVATE_KEY', 'PRIVATE_KEY')


def read_private_key(*env_names):
    names = env_names or ENV_NAMES
    for name in names:
        raw = os.environ.get(name, '').strip()
        if not raw:
            continue
        if not _KEY.fullmatch(raw):
            raise SafetyError('环境变量私钥格式错误')
        log(f'已从环境变量 {name} 读取私钥')
        return raw
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            key = getpass.getpass('独立钱包私钥（隐藏输入，不保存；也可设 PRIVATE_KEY）: ').strip()
        except getpass.GetPassWarning:
            raise SafetyError('不能安全关闭终端回显，已拒绝输入') from None
    if not _KEY.fullmatch(key):
        raise SafetyError('私钥格式错误')
    return key
