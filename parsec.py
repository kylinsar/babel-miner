#!/usr/bin/env python3
"""PARSEC Scan · PoW miner: Arc USDC gas only, mint(nonce) value=0."""
import argparse
import contextlib
import hashlib
import json
import math
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from requests.exceptions import HTTPError, ConnectionError, Timeout
from web3 import Web3

from babel import Worker, selftest, work, kh
from core.safety import SafetyError, RpcReadError, log, amount, fmt, MAX256, read_retry, rpc_error_label, transient_rpc
from core.ledger import Ledger as CoreLedger

HERE = Path(__file__).resolve().parent
CHAIN = 5042
CONTRACT = Web3.to_checksum_address('0x631f96907126Ae23313Ec8DF489da0879AACfC98')
RPC = 'https://rpc.mainnet.arc.io'
MAX_PARSECS = 8800
ABI = [
    {'type': 'function', 'name': 'nextTokenId', 'stateMutability': 'view', 'inputs': [], 'outputs': [{'type': 'uint256'}]},
    {'type': 'function', 'name': 'entropy', 'stateMutability': 'view', 'inputs': [], 'outputs': [{'type': 'bytes32'}]},
    {'type': 'function', 'name': 'difficulty', 'stateMutability': 'view', 'inputs': [], 'outputs': [{'type': 'uint256'}]},
    {'type': 'function', 'name': 'paused', 'stateMutability': 'view', 'inputs': [], 'outputs': [{'type': 'bool'}]},
    {'type': 'function', 'name': 'priceAt', 'stateMutability': 'view', 'inputs': [{'name': 'tokenId', 'type': 'uint256'}], 'outputs': [{'type': 'uint256'}]},
    {'type': 'function', 'name': 'requiredWorkFor', 'stateMutability': 'view', 'inputs': [{'name': 'tokenId', 'type': 'uint256'}, {'name': 'paid', 'type': 'uint256'}], 'outputs': [{'type': 'uint256'}]},
    {'type': 'function', 'name': 'proofTarget', 'stateMutability': 'pure', 'inputs': [{'name': 'expectedWork', 'type': 'uint256'}], 'outputs': [{'type': 'uint256'}]},
    {'type': 'function', 'name': 'mint', 'stateMutability': 'payable', 'inputs': [{'name': 'nonce', 'type': 'uint256'}], 'outputs': [{'type': 'uint256'}]},
]


def proof_target(work_hashes):
    if type(work_hashes) is not int or work_hashes <= 0:
        raise SafetyError('纯扫描 requiredWork 无效')
    return MAX256 // work_hashes


@dataclass(frozen=True)
class Job:
    token_id: int
    seed: bytes
    target: int
    work: int = 0
    price: int = 0
    difficulty: int = 0
    block: int = 0

    def identity(self):
        return self.token_id, self.seed, self.target


class Chain:
    def __init__(self, rpc):
        if not rpc.startswith('https://'):
            raise SafetyError('RPC 必须使用 HTTPS')
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 10}, exception_retry_configuration=None))
        self.contract = self.w3.eth.contract(address=CONTRACT, abi=ABI)
        self.entropy = None
        self.verify()

    def verify(self):
        if self.w3.eth.chain_id != CHAIN:
            raise SafetyError('链 ID 不匹配，必须是 Arc mainnet 5042')
        expected = json.loads((HERE / 'parsec-protocol.json').read_text())['code_sha256']
        code = bytes(self.w3.eth.get_code(CONTRACT))
        if not code or hashlib.sha256(code).hexdigest() != expected:
            raise SafetyError('合约字节码与固定版本不一致')
        if self.entropy is None:
            entropy = bytes(self.contract.functions.entropy().call())
            if len(entropy) != 32:
                raise SafetyError('entropy 无效')
            self.entropy = entropy

    def job(self, deadline=None, *, poll=False):
        if poll:
            try:
                return self._poll_state()
            except (HTTPError, ConnectionError, Timeout) as exc:
                label = rpc_error_label(exc)
                if not transient_rpc(exc):
                    raise SafetyError(f'RPC任务读取失败：{label}；已停止，没有自动重发交易') from None
                raise RpcReadError(label) from None
        return read_retry(self._job, deadline)

    def _snapshot(self, token_id, work_hashes, *, price=0, difficulty=0, block=0):
        if token_id >= MAX_PARSECS:
            raise SafetyError('全部 8800 个 parsec 已编目完毕')
        target = proof_target(work_hashes)
        if not 0 < target <= MAX256:
            raise SafetyError('纯扫描 target 无效')
        return Job(token_id, self.entropy, target, work_hashes, price, difficulty, block)

    def _poll_state(self):
        if self.contract.functions.paused().call():
            raise SafetyError('调查已暂停')
        token_id = self.contract.functions.nextTokenId().call()
        if token_id >= MAX_PARSECS:
            raise SafetyError('全部 8800 个 parsec 已编目完毕')
        work_hashes = self.contract.functions.requiredWorkFor(token_id, 0).call()
        return self._snapshot(token_id, work_hashes)

    def _job(self):
        self.verify()
        if self.contract.functions.paused().call():
            raise SafetyError('调查已暂停')
        block = self.w3.eth.block_number
        token_id = self.contract.functions.nextTokenId().call(block_identifier=block)
        if token_id >= MAX_PARSECS:
            raise SafetyError('全部 8800 个 parsec 已编目完毕')
        work_hashes = self.contract.functions.requiredWorkFor(token_id, 0).call(block_identifier=block)
        chain_target = self.contract.functions.proofTarget(work_hashes).call(block_identifier=block)
        job = self._snapshot(
            token_id, work_hashes,
            price=self.contract.functions.priceAt(token_id).call(block_identifier=block),
            difficulty=self.contract.functions.difficulty().call(block_identifier=block),
            block=block,
        )
        if chain_target != job.target:
            raise SafetyError('纯扫描 target 与 proofTarget 不一致')
        return job

    def show(self, j, address):
        expected = (1 << 256) / j.target
        log(f'Arc chain={CHAIN} block={j.block} token={j.token_id}/{MAX_PARSECS} entropy=0x{j.seed.hex()}')
        log(f'纯扫描 mint value=0 USDC；付费铸造参考价={fmt(j.price)} USDC；E[hashes]={expected:.6g} bits={math.log2(expected):.3f}')
        if address:
            log(f'钱包 {address}，余额={fmt(self.w3.eth.get_balance(address))} USDC（native，18位精度）')


class Ledger(CoreLedger):
    def __init__(self, address):
        super().__init__(address, app='parsec-miner', chain_id=CHAIN, contract=CONTRACT)


def mint_data(nonce):
    return '0x' + (kh(b'mint(uint256)')[:4] + nonce.to_bytes(32, 'big')).hex()


def validate_tx(tx, address, nonce, gas_cap):
    if tx.get('chainId') != CHAIN or tx.get('to', '').lower() != CONTRACT.lower() or tx.get('from', '').lower() != address.lower():
        raise SafetyError('交易目标或发送者错误')
    if tx.get('value') != 0 or tx.get('data') != mint_data(nonce):
        raise SafetyError('只允许 value=0 的纯扫描 mint')
    if not 0 < tx['gas'] <= 3_000_000 or not 0 <= tx['maxPriorityFeePerGas'] <= tx['maxFeePerGas'] or tx['maxFeePerGas'] <= 0:
        raise SafetyError('Gas 参数无效')
    if tx['gas'] * tx['maxFeePerGas'] > gas_cap:
        raise SafetyError('单笔 Gas 预留超出上限')
    if set(tx) != {'chainId', 'to', 'from', 'value', 'data', 'nonce', 'gas', 'maxFeePerGas', 'maxPriorityFeePerGas'}:
        raise SafetyError('交易存在未允许的字段')


def submit(chain, job, nonce, account, ledger, args, deadline):
    chain.verify()
    fresh = chain.job(deadline=deadline)
    if fresh.identity() != job.identity():
        log('解已过期，重新挖矿')
        return False
    digest = int.from_bytes(work(job.seed, account.address, nonce), 'big')
    if digest > job.target:
        raise SafetyError('CPU 复核失败')
    w3 = chain.w3
    fn = chain.contract.functions.mint(nonce)
    base = {'from': account.address, 'value': 0}
    fn.call(base)
    gas = math.ceil(fn.estimate_gas(base) * 1.2)
    tip = w3.eth.max_priority_fee
    fee = 2 * w3.eth.get_block('latest')['baseFeePerGas'] + tip
    tx = {
        'chainId': CHAIN, 'from': account.address, 'to': CONTRACT, 'value': 0,
        'data': fn._encode_transaction_data(), 'nonce': w3.eth.get_transaction_count(account.address, 'pending'),
        'gas': gas, 'maxFeePerGas': fee, 'maxPriorityFeePerGas': tip,
    }
    validate_tx(tx, account.address, nonce, args.max_gas_cost)
    if w3.eth.get_balance(account.address) < gas * fee:
        raise SafetyError('Arc USDC 余额不足以覆盖最大 Gas')
    if chain.job(deadline=deadline).identity() != job.identity():
        log('签名前任务已变化，丢弃解')
        return False
    if time.monotonic() >= deadline:
        raise SafetyError('运行时间已到，禁止签名')
    ledger.reserve(tx, args.budget, args.max_txs)
    signed = account.sign_transaction(tx)
    from core.broadcast import broadcast_once
    return broadcast_once(
        w3, signed, deadline=deadline, symbol='USDC', explorer_base='https://explorer.arc.io/tx',
        reserved_cost=gas * fee, fail_noun='parsec',
    )


def mine(args, chain, account=None, ledger=None):
    from core.engine import run_native
    bench = args.command == 'bench'

    def fetch_job(deadline=None, poll=False):
        if bench:
            return Job(0, bytes.fromhex('22' * 32), 0)
        return chain.job(deadline=deadline, poll=poll)

    def on_hit(job, address, nonce, deadline):
        log(f'HIT nonce=0x{nonce:064x} hash=0x{work(job.seed, address, nonce).hex()}')
        if args.command == 'dry':
            chain.contract.functions.mint(nonce).call({'from': address, 'value': 0})
            log('DRY RUN：链上 eth_call 模拟通过，没有签名或广播')
            return
        submit(chain, job, nonce, account, ledger, args, deadline)

    run_native(
        command=args.command, address=args.address, backend=args.backend, devices=args.devices,
        threads=args.threads, seconds=args.seconds, poll=args.poll, batch=args.batch,
        fetch_job=fetch_job, show_job=(lambda job, addr: chain.show(job, addr)),
        spawn_worker=Worker, selftest=selftest, on_hit=on_hit,
        job_target=lambda j: j.target, job_label=lambda j: f'token={j.token_id}',
    )


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['check', 'bench', 'dry', 'live'])
    p.add_argument('--rpc', default=RPC)
    p.add_argument('--address', help='公开钱包地址；dry/live 必填，live 会核对私钥派生地址')
    p.add_argument('--backend', choices=['cpu', 'cuda'], default='cpu')
    p.add_argument('--devices', default='0', help='CUDA 设备索引，如 0,1,2')
    p.add_argument('--threads', type=int, default=1, help='CPU 进程数')
    p.add_argument('--seconds', type=int, default=60)
    p.add_argument('--poll', type=float, default=8)
    p.add_argument('--batch', type=int, default=0)
    p.add_argument('--max-gas-cost', type=amount, help='单笔最高 Gas 成本，单位原生 USDC')
    p.add_argument('--budget', type=amount, help='本机钱包累计 Gas 预留预算，单位 USDC')
    p.add_argument('--max-txs', type=int, help='该钱包累计签名笔数上限，含历史预留')
    return p


def main():
    args = parser().parse_args()
    if not 1 <= args.seconds <= 86400 or not 1 <= args.threads <= 128 or not 0.5 <= args.poll <= 60 or not 0 <= args.batch <= 1 << 26:
        raise SafetyError('时长/线程/轮询/批次参数越界')
    if args.max_txs is not None and args.max_txs < 1:
        raise SafetyError('签名笔数上限必须≥1')
    if args.address:
        if not Web3.is_address(args.address):
            raise SafetyError('钱包地址格式无效')
        args.address = Web3.to_checksum_address(args.address)
    if args.command in ('dry', 'live') and not args.address:
        raise SafetyError('必须指定 --address 公开钱包地址')
    if args.command == 'live' and (not args.budget or not args.max_gas_cost or not args.max_txs):
        raise SafetyError('正式模式必须设置 --budget、--max-gas-cost 与 --max-txs')
    chain = None if args.command == 'bench' else Chain(args.rpc)
    if args.command == 'check':
        chain.show(chain.job(), args.address)
        return
    if args.command != 'live':
        mine(args, chain)
        return
    with contextlib.closing(Ledger(args.address)) as ledger:
        records = ledger.records()
        if len(records) >= args.max_txs or sum(r['reserved'] for r in records) >= args.budget:
            raise SafetyError('历史预留已用尽预算或签名次数')
        chain.show(chain.job(), args.address)
        print('正式模式会按设定时长持续挖矿。预算账本按本机用户和钱包保存，不随重启清零。')
        print('每笔预留 Gas；失败、未广播、pending 均不自动释放。找到解不会结束本次运行。')
        print(f'value=0 mint(nonce)；单笔Gas≤{fmt(args.max_gas_cost)} USDC；累计≤{fmt(args.budget)} USDC；累计签名≤{args.max_txs}。')
        print('不同服务器请用不同钱包；跨服务器不共享预算和 nonce 锁。')
        if input('确认真实付费输入 START: ').strip() != 'START':
            raise SafetyError('已取消')
        from core.keys import read_private_key
        key = read_private_key('PARSEC_PRIVATE_KEY', 'POW_PRIVATE_KEY', 'PRIVATE_KEY')
        try:
            account = chain.w3.eth.account.from_key(key)
        except Exception:
            raise SafetyError('私钥格式无效') from None
        finally:
            key = None
        if account.address.lower() != args.address.lower():
            raise SafetyError('私钥派生地址与 --address 不匹配')
        mine(args, chain, account, ledger)


if __name__ == '__main__':
    def stop(signum, frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        main()
    except KeyboardInterrupt:
        log('停止；云服务器租金仍会继续计费')
        sys.exit(130)
    except Exception as exc:
        if isinstance(exc, (SafetyError, argparse.ArgumentTypeError, FileNotFoundError)):
            log(f'停止：{exc}')
        else:
            log(f'停止：{rpc_error_label(exc)}；请检查 RPC、依赖和服务器环境，未自动重试交易')
        sys.exit(1)
