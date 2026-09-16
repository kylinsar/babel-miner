import argparse
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import babel as b
import parsec as p
from core.safety import MAX256, SafetyError

ADDRESS = '0x' + '11' * 20


class ParsecProtocolTests(unittest.TestCase):
    def test_same_keccak_packing_as_babel(self):
        seed = bytes.fromhex('00' * 31 + '01')
        nonce = 0
        self.assertEqual(
            p.work(seed, '0x000000000000000000000000000000000000dEaD', nonce),
            b.work(seed, '0x000000000000000000000000000000000000dEaD', nonce),
        )
        self.assertEqual(
            p.work(seed, '0x000000000000000000000000000000000000dEaD', nonce).hex(),
            '66a517900a5e1fad20e92c2f95fbbafcb5dbda299ba0201f5fde4e50dbbfbe16',
        )

    def test_proof_target_matches_solidity_div(self):
        self.assertEqual(p.proof_target(1), MAX256)
        self.assertEqual(p.proof_target(2), MAX256 // 2)
        self.assertEqual(p.proof_target(6675360404522), MAX256 // 6675360404522)
        with self.assertRaises(SafetyError):
            p.proof_target(0)

    def test_mint_calldata(self):
        self.assertEqual(p.mint_data(0), '0xa0712d68' + '00' * 32)
        self.assertEqual(p.mint_data(3), '0xa0712d68' + '00' * 31 + '03')

    def test_validate_tx_allowlist(self):
        tx = {
            'chainId': p.CHAIN, 'to': p.CONTRACT, 'from': ADDRESS, 'value': 0,
            'data': p.mint_data(3), 'nonce': 0, 'gas': 100000,
            'maxFeePerGas': 1000, 'maxPriorityFeePerGas': 1,
        }
        p.validate_tx(tx, ADDRESS, 3, 10 ** 18)
        with self.assertRaises(SafetyError):
            p.validate_tx(dict(tx, value=1), ADDRESS, 3, 10 ** 18)
        with self.assertRaises(SafetyError):
            p.validate_tx(dict(tx, data=p.mint_data(4)), ADDRESS, 3, 10 ** 18)
        with self.assertRaises(SafetyError):
            p.validate_tx(dict(tx, to=b.CONTRACT), ADDRESS, 3, 10 ** 18)

    def test_sold_out_snapshot(self):
        chain = object.__new__(p.Chain)
        chain.entropy = bytes(32)
        with self.assertRaisesRegex(SafetyError, '8800'):
            chain._snapshot(8800, 1)

    def test_checksum_contract(self):
        self.assertEqual(p.CONTRACT, '0x631f96907126Ae23313Ec8DF489da0879AACfC98')
        self.assertEqual(p.CHAIN, 5042)
        self.assertEqual(p.MAX_PARSECS, 8800)


class ParsecSubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.object(Path, 'home', return_value=Path(self.tmp.name)):
            self.ledger = p.Ledger(ADDRESS)

    def tearDown(self):
        self.ledger.close()

    def fixture(self):
        chain = Mock()
        job = p.Job(0, bytes(32), MAX256)
        chain.job.return_value = job
        fn = chain.contract.functions.mint.return_value
        fn.estimate_gas.return_value = 100000
        fn._encode_transaction_data.return_value = p.mint_data(3)
        chain.w3.eth.max_priority_fee = 0
        chain.w3.eth.get_block.return_value = {'baseFeePerGas': 500}
        chain.w3.eth.get_transaction_count.return_value = 0
        chain.w3.eth.get_balance.return_value = 10 ** 18
        from eth_account import Account
        account = Account.from_key('01' * 32)
        return chain, job, account, argparse.Namespace(max_gas_cost=10 ** 9, budget=10 ** 9, max_txs=1)

    def test_stale_never_signs(self):
        chain, job, account, args = self.fixture()
        chain.job.return_value = p.Job(1, bytes(32), MAX256)
        self.assertFalse(p.submit(chain, job, 3, account, self.ledger, args, time.monotonic() + 10))
        self.assertEqual(self.ledger.records(), [])
        chain.w3.eth.send_raw_transaction.assert_not_called()

    def test_success_value_zero(self):
        chain, job, account, args = self.fixture()

        def broadcast(raw):
            self.assertEqual(len(self.ledger.records()), 1)
            return p.kh(bytes(raw))

        chain.w3.eth.send_raw_transaction.side_effect = broadcast
        chain.w3.eth.get_transaction_receipt.return_value = {'status': 1, 'blockNumber': 123, 'gasUsed': 80000}
        self.assertTrue(p.submit(chain, job, 3, account, self.ledger, args, time.monotonic() + 10))
        self.assertEqual(self.ledger.records()[0]['tx']['value'], 0)

    def test_failed_broadcast_keeps_reservation(self):
        chain, job, account, args = self.fixture()
        chain.w3.eth.send_raw_transaction.side_effect = TimeoutError()
        with self.assertRaisesRegex(SafetyError, '广播结果未知'):
            p.submit(chain, job, 3, account, self.ledger, args, time.monotonic() + 10)
        self.assertEqual(len(self.ledger.records()), 1)


if __name__ == '__main__':
    unittest.main()
