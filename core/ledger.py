"""Durable pre-sign reservations. Never auto-release. App name isolates projects."""
import fcntl
import json
import os
from pathlib import Path
import time
from core.safety import SafetyError


class Ledger:
    def __init__(self, address, *, app, chain_id, contract):
        self.directory = Path.home() / '.local/state' / app
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stem = f'{int(chain_id)}-{str(contract).lower()}-{address.lower()}'
        self.path = self.directory / (stem + '.jsonl')
        self.fd = os.open(self.directory / (stem + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise SafetyError('本机同一钱包已有正式进程')

    def close(self):
        os.close(self.fd)

    def records(self):
        if not self.path.exists():
            return []
        try:
            records = [json.loads(s) for s in self.path.read_text().splitlines()]
            for r in records:
                reserved = r.get('reserved', r.get('reserved_wei'))
                if type(reserved) is not int or reserved <= 0 or type(r['nonce']) is not int or r['nonce'] < 0:
                    raise ValueError()
                r['reserved'] = reserved
            return records
        except (KeyError, ValueError, TypeError):
            raise SafetyError('预算账本损坏，停止签名')

    def used(self):
        rec = self.records()
        return len(rec), sum(r['reserved'] for r in rec)

    def reserve(self, tx, budget, max_txs, cost=None):
        records = self.records()
        if cost is None:
            cost = tx['gas'] * tx['maxFeePerGas']
        if type(cost) is not int or cost <= 0:
            raise SafetyError('预留金额无效')
        if len(records) >= max_txs:
            raise SafetyError('累计签名笔数已达上限')
        if any(r['nonce'] == tx['nonce'] for r in records):
            raise SafetyError('nonce 已有预留，请核查历史交易')
        if sum(r['reserved'] for r in records) + cost > budget:
            raise SafetyError('累计 Gas 预算不足')
        record = {'nonce': tx['nonce'], 'reserved': cost, 'time': int(time.time()), 'tx': tx}
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as f:
            f.write(json.dumps(record) + '\n')
            f.flush()
            os.fsync(f.fileno())
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
