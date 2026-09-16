import io
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.broadcast import broadcast_once, keccak256
from core.engine import run_native
from core.safety import RpcReadError, SafetyError


class Job:
    def __init__(self, laid=1, target=1):
        self.laid, self.target = laid, target

    def identity(self):
        return self.laid


class FakeWorker:
    def __init__(self, device, found=False):
        self.device = device
        r, w = os.pipe()
        self._w = w
        self.p = types.SimpleNamespace(stdout=open(r, 'rb', buffering=0), poll=lambda: None)
        self.sent = time.monotonic()
        self.job = None
        self._found = found
        os.write(self._w, b'\n')

    def send(self, job, address, batch):
        self.job = job
        self.sent = time.monotonic()
        os.write(self._w, b'\n')

    def read(self):
        os.read(self.p.stdout.fileno(), 64)
        found, self._found = self._found, False
        return self.job, 7, 8, found

    def close(self):
        self.p.stdout.close()
        try:
            os.close(self._w)
        except OSError:
            pass


class EngineTests(unittest.TestCase):
    def test_bench_emits_json(self):
        def spawn(_):
            return FakeWorker('cpu')

        out = io.StringIO()
        with patch('sys.stdout', out):
            run_native(
                command='bench', address=None, backend='cpu', devices='0', threads=1,
                seconds=0.05, poll=8, batch=16, fetch_job=lambda **k: Job(),
                show_job=lambda *a: None, spawn_worker=spawn, selftest=lambda w: None,
                on_hit=lambda *a: True,
            )
        last = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(last['type'], 'bench')
        self.assertGreaterEqual(last['hashes'], 0)

    def test_hit_continues_until_deadline(self):
        hits = []

        def spawn(_):
            return FakeWorker('cpu', found=True)

        out = io.StringIO()
        with patch('sys.stdout', out):
            run_native(
                command='dry', address='0x' + '11' * 20, backend='cpu', devices='0', threads=1,
                seconds=0.15, poll=8, batch=16, fetch_job=lambda **k: Job(),
                show_job=lambda *a: None, spawn_worker=spawn, selftest=lambda w: None,
                on_hit=lambda job, address, nonce, deadline: hits.append(nonce),
            )
        self.assertEqual(hits, [7])
        last = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(last['type'], 'stopped')

    def test_budget_error_still_stops(self):
        def spawn(_):
            return FakeWorker('cpu', found=True)

        with self.assertRaisesRegex(SafetyError, '累计签名'):
            run_native(
                command='live', address='0x' + '11' * 20, backend='cpu', devices='0', threads=1,
                seconds=5, poll=8, batch=16, fetch_job=lambda **k: Job(),
                show_job=lambda *a: None, spawn_worker=spawn, selftest=lambda w: None,
                on_hit=lambda *a: (_ for _ in ()).throw(SafetyError('累计签名笔数已达上限')),
            )

    def test_poll_error_keeps_hashing(self):
        def fetch_job(deadline=None, poll=False):
            if poll:
                raise RpcReadError('HTTP 429')
            return Job()

        def spawn(_):
            return FakeWorker('cpu')

        with patch('core.engine.after_poll_error', side_effect=lambda *a: 15) as after:
            run_native(
                command='dry', address='0x' + '11' * 20, backend='cpu', devices='0', threads=1,
                seconds=0.3, poll=0.05, batch=16, fetch_job=fetch_job,
                show_job=lambda *a: None, spawn_worker=spawn, selftest=lambda w: None,
                on_hit=lambda *a: False,
            )
        self.assertTrue(after.called)


class BroadcastTests(unittest.TestCase):
    def test_keccak_matches_pycryptodome(self):
        self.assertEqual(keccak256(b'abc').hex(), '4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45')

    def test_failed_send_does_not_retry(self):
        w3 = Mock()
        w3.eth.send_raw_transaction.side_effect = TimeoutError()
        signed = Mock(raw_transaction=b'\x01' * 32)
        with self.assertRaisesRegex(SafetyError, '不会自动重试'):
            broadcast_once(w3, signed, deadline=time.monotonic() + 10, symbol='USDC',
                           explorer_base='https://explorer.arc.io/tx', reserved_cost=12)
        self.assertEqual(w3.eth.send_raw_transaction.call_count, 1)


if __name__ == '__main__':
    unittest.main()
