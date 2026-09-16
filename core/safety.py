"""Process-wide safety helpers. Plugins must not retry broadcasts."""
import argparse
import math
import re
import time
from requests.exceptions import HTTPError, ConnectionError, Timeout

MAX256 = (1 << 256) - 1
POLL_BACKOFF_MIN = 15
POLL_BACKOFF_MAX = 60
STALE_POLL = 180


class SafetyError(Exception):
    pass


class RpcReadError(SafetyError):
    pass


def log(msg):
    print(time.strftime('[%H:%M:%S]'), msg, flush=True)


def parse_cuda_devices(raw):
    parts = [p.strip() for p in re.split(r'[,，、;；]+', raw.strip()) if p.strip()]
    if not parts or not all(p.isdigit() for p in parts) or len(set(parts)) != len(parts):
        raise SafetyError(f'devices 必须为不重复的阿拉伯数字索引，用英文逗号分隔，例如 0,1,2；当前={raw!r}')
    return parts


def amount(raw):
    if not re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,18})?', raw):
        raise argparse.ArgumentTypeError('请填普通正数，最多18位小数')
    whole, _, frac = raw.partition('.')
    value = int(whole) * 10**18 + int(frac.ljust(18, '0'))
    if value <= 0:
        raise argparse.ArgumentTypeError('金额必须大于0')
    return value


def fmt(n, decimals=18):
    whole, frac = divmod(int(n), 10**decimals)
    tail = f'{frac:0{decimals}d}'.rstrip('0')
    return str(whole) + ('.' + tail if tail else '')


def rpc_error_label(exc):
    if isinstance(exc, HTTPError):
        response = exc.response
        return f'HTTP {response.status_code}' if response is not None else 'HTTPError（无状态码）'
    return type(exc).__name__


def http_status(exc):
    if isinstance(exc, HTTPError) and exc.response is not None:
        return exc.response.status_code
    return None


def transient_rpc(exc):
    if not isinstance(exc, HTTPError):
        return True
    return http_status(exc) in (408, 425, 429, 500, 502, 503, 504)


def retry_delay(exc, attempt):
    response = exc.response if isinstance(exc, HTTPError) else None
    raw = response.headers.get('Retry-After') if response is not None and response.headers else None
    if raw and re.fullmatch(r'[0-9]{1,4}', raw.strip()):
        return min(max(int(raw.strip()), 1), POLL_BACKOFF_MAX)
    if http_status(exc) == 429:
        return min(POLL_BACKOFF_MIN * (2 ** attempt), POLL_BACKOFF_MAX)
    return 2 ** (attempt + 1)


def read_retry(fn, deadline=None):
    """Only call with idempotent task reads; never wrap signing/broadcast."""
    attempt = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise SafetyError('运行时间已到，停止读取重试')
        try:
            return fn()
        except (HTTPError, ConnectionError, Timeout) as exc:
            label = rpc_error_label(exc)
            if not transient_rpc(exc) or (deadline is None and attempt >= 3):
                raise RpcReadError(f'RPC任务读取失败：{label}；已停止，没有自动重发交易') from None
            delay = retry_delay(exc, min(attempt, 4))
            if deadline is not None:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    raise SafetyError('运行时间已到，停止读取重试') from None
                delay = min(delay, remain)
            log(f'RPC任务读取失败：{label}；{delay:g}秒后重试（第{attempt+1}次；不重发交易）')
            time.sleep(delay)
            attempt += 1


def after_poll_error(label, job, poll_wait, last_good, now):
    if now - last_good > STALE_POLL:
        raise SafetyError(f'RPC 已连续 {STALE_POLL} 秒不可用，已停止，没有自动重发交易')
    wait = min(max(poll_wait * 2, POLL_BACKOFF_MIN), POLL_BACKOFF_MAX)
    laid = getattr(job, 'laid', getattr(job, 'id', '?'))
    log(f'RPC轮询 {label}；GPU继续当前 laid={laid}，{wait:g}秒后再查（不停止、不重发交易）')
    return wait


def expected_hashes(target):
    if not target:
        return 0
    return (1 << 256) / target
