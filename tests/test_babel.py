import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import babel as b

ADDRESS='0x'+'11'*20

class ProtocolTests(unittest.TestCase):
    def test_site_vector(self):
        # Produced independently by the live site's gpuMiner-Di_dSJHy.js exports k/p/h.
        self.assertEqual(b.work(bytes.fromhex('00'*31+'01'),'0x000000000000000000000000000000000000dEaD',0).hex(),'66a517900a5e1fad20e92c2f95fbbafcb5dbda299ba0201f5fde4e50dbbfbe16')
    def test_amount(self):
        self.assertEqual(b.amount('0.045'),45000000000000000)
        self.assertEqual(b.amount('999999999.123456789012345678'),999999999123456789012345678)
        for s in ('0','-1','1e3','nan','1.0000000000000000001','０.１'):
            with self.assertRaises(argparse.ArgumentTypeError):b.amount(s)
    def test_worker_against_independent_keccak(self):
        rng=random.Random(42)
        with contextlib.closing(b.Worker('cpu')) as w:
            b.selftest(w)
            for _ in range(20):
                j=b.Job(0,rng.randbytes(32),0)
                address='0x'+rng.randbytes(20).hex()
                w.send(j,address,31,rng.randbytes(24),rng.randrange(1<<63))
                self.assertEqual(w.read()[2:],(31,False))
            j=b.Job(0,rng.randbytes(32),b.MAX256)
            w.send(j,ADDRESS,100)
            self.assertTrue(w.read()[3])
    def test_worker_bad_hex_exits(self):
        p=subprocess.run([str(b.HERE/'bin/babel-cpu')],input='xx '+'11'*20+' '+'00'*24+' 0 1 '+'00'*32+'\n',text=True,capture_output=True)
        self.assertNotEqual(p.returncode,0)
    def test_worker_out_of_range_fails(self):
        p=subprocess.run([str(b.HERE/'bin/babel-cpu')],input='00'*32+' '+'11'*20+' '+'00'*24+f' {(1<<64)-1} 2 '+'00'*32+'\n',text=True,capture_output=True)
        self.assertNotEqual(p.returncode,0)
    def test_runtime_stops(self):
        p=subprocess.run([sys.executable,str(b.HERE/'babel.py'),'bench','--seconds','1','--threads','2'],text=True,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        last=json.loads(p.stdout.splitlines()[-1]);self.assertGreater(last['hashes'],0)

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.home=patch.object(b.Path,'home',return_value=Path(self.tmp.name));self.home.start()
        self.ledger=b.Ledger(ADDRESS)
        self.tx={'chainId':b.CHAIN,'to':b.CONTRACT,'from':ADDRESS,'value':0,'data':'0x'+(b.kh(b'lay(uint256,uint256)')[:4]+bytes(32)+(3).to_bytes(32,'big')).hex(),'nonce':0,'gas':100000,'maxFeePerGas':1000,'maxPriorityFeePerGas':0}
    def tearDown(self):self.ledger.close();self.home.stop();self.tmp.cleanup()
    def test_transaction_allowlist(self):
        b.validate_tx(self.tx,ADDRESS,3,10**9)
        for key,value in [('value',1),('chainId',1),('to',ADDRESS),('data','0x'),('gas',3_000_001),('maxPriorityFeePerGas',1001),('accessList',[])]:
            with self.subTest(key=key), self.assertRaises(b.SafetyError):b.validate_tx(dict(self.tx,**{key:value}),ADDRESS,3,10**9)
        with self.assertRaises(b.SafetyError):b.validate_tx(self.tx,ADDRESS,3,1)
    def test_durable_budget_nonce_count(self):
        self.ledger.reserve(self.tx,10**9,2)
        with self.assertRaises(b.SafetyError):self.ledger.reserve(self.tx,10**9,2)
        tx=dict(self.tx,nonce=1)
        with self.assertRaises(b.SafetyError):self.ledger.reserve(tx,10**9,1)
        with self.assertRaises(b.SafetyError):self.ledger.reserve(tx,100000001,2)
        self.assertEqual(len(self.ledger.records()),1)
    def test_corrupt_ledger(self):
        self.ledger.path.write_text('{broken')
        with self.assertRaises(b.SafetyError):self.ledger.reserve(self.tx,10**9,2)
    def test_lock(self):
        with self.assertRaises(b.SafetyError):b.Ledger(ADDRESS)
    def fixture(self):
        chain=Mock();job=b.Job(0,bytes(32),b.MAX256)
        chain.job.return_value=job
        fn=chain.contract.functions.lay.return_value
        fn.estimate_gas.return_value=100000;fn._encode_transaction_data.return_value=self.tx['data']
        chain.w3.eth.max_priority_fee=0;chain.w3.eth.get_block.return_value={'baseFeePerGas':500}
        chain.w3.eth.get_transaction_count.return_value=0;chain.w3.eth.get_balance.return_value=10**18
        from eth_account import Account
        account=Account.from_key('01'*32)
        # Actual signer matches --address; patch spy for whether signing was invoked.
        return chain,job,account,argparse.Namespace(max_gas_cost=10**9,budget=10**9,max_txs=1)
    def test_stale_never_signs(self):
        chain,job,account,args=self.fixture();chain.job.return_value=b.Job(1,bytes(32),b.MAX256)
        self.assertFalse(b.submit(chain,job,3,account,self.ledger,args,time.monotonic()+10))
        self.assertEqual(self.ledger.records(),[]);chain.w3.eth.send_raw_transaction.assert_not_called()
    def test_expired_never_signs(self):
        chain,job,account,args=self.fixture()
        with self.assertRaises(b.SafetyError):b.submit(chain,job,3,account,self.ledger,args,0)
        self.assertEqual(self.ledger.records(),[]);chain.w3.eth.send_raw_transaction.assert_not_called()
    def test_success_uses_zero_value_and_reserved_before_broadcast(self):
        chain,job,account,args=self.fixture()
        def broadcast(raw):
            self.assertEqual(len(self.ledger.records()),1)
            return b.kh(bytes(raw))
        chain.w3.eth.send_raw_transaction.side_effect=broadcast
        chain.w3.eth.get_transaction_receipt.return_value={'status':1,'blockNumber':123,'gasUsed':80000}
        self.assertTrue(b.submit(chain,job,3,account,self.ledger,args,time.monotonic()+10))
        self.assertEqual(self.ledger.records()[0]['tx']['value'],0)
    def test_failed_broadcast_keeps_reservation(self):
        chain,job,account,args=self.fixture();chain.w3.eth.send_raw_transaction.side_effect=TimeoutError()
        with self.assertRaisesRegex(b.SafetyError,'广播结果未知'):b.submit(chain,job,3,account,self.ledger,args,time.monotonic()+10)
        self.assertEqual(len(self.ledger.records()),1)
    def test_estimate_revert_does_not_sign(self):
        chain,job,account,args=self.fixture();chain.contract.functions.lay.return_value.estimate_gas.side_effect=ValueError('revert')
        with self.assertRaises(ValueError):b.submit(chain,job,3,account,self.ledger,args,time.monotonic()+10)
        self.assertEqual(self.ledger.records(),[]);chain.w3.eth.send_raw_transaction.assert_not_called()
    def test_low_balance_does_not_sign(self):
        chain,job,account,args=self.fixture();chain.w3.eth.get_balance.return_value=0
        with self.assertRaisesRegex(b.SafetyError,'余额不足'):b.submit(chain,job,3,account,self.ledger,args,time.monotonic()+10)
        self.assertEqual(self.ledger.records(),[]);chain.w3.eth.send_raw_transaction.assert_not_called()
    def test_sign_failure_keeps_reservation(self):
        chain,job,real,args=self.fixture();account=Mock(address=real.address)
        account.sign_transaction.side_effect=ValueError('signer failure')
        with self.assertRaises(ValueError):b.submit(chain,job,3,account,self.ledger,args,time.monotonic()+10)
        self.assertEqual(len(self.ledger.records()),1);chain.w3.eth.send_raw_transaction.assert_not_called()
    def test_revert_keeps_reservation(self):
        chain,job,account,args=self.fixture()
        chain.w3.eth.send_raw_transaction.side_effect=lambda raw:b.kh(bytes(raw))
        chain.w3.eth.get_transaction_receipt.return_value={'status':0}
        with self.assertRaisesRegex(b.SafetyError,'交易失败'):b.submit(chain,job,3,account,self.ledger,args,time.monotonic()+10)
        self.assertEqual(len(self.ledger.records()),1)
    def test_reservation_write_failure_does_not_sign(self):
        chain,job,real,args=self.fixture();account=Mock(address=real.address)
        with patch.object(self.ledger,'reserve',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):b.submit(chain,job,3,account,self.ledger,args,time.monotonic()+10)
        account.sign_transaction.assert_not_called();chain.w3.eth.send_raw_transaction.assert_not_called()
    def test_chain_id_and_code_checks(self):
        chain=object.__new__(b.Chain);chain.w3=Mock();chain.w3.eth.chain_id=1
        with self.assertRaises(b.SafetyError):chain.verify()
        chain.w3.eth.chain_id=b.CHAIN;chain.w3.eth.get_code.return_value=b'not the contract'
        with self.assertRaises(b.SafetyError):chain.verify()

if __name__=='__main__':unittest.main()
