#!/usr/bin/env python3
"""Babel Linux miner: CPU/CUDA, read-only by default, pure-work mint only."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from Crypto.Hash import keccak
from requests.exceptions import HTTPError, ConnectionError, Timeout
from web3 import Web3
from core.safety import (
    SafetyError, RpcReadError, log, parse_cuda_devices, amount, fmt,
    MAX256, POLL_BACKOFF_MIN, POLL_BACKOFF_MAX, STALE_POLL,
    rpc_error_label, http_status, transient_rpc, retry_delay, read_retry,
    after_poll_error,
)
from core.ledger import Ledger as CoreLedger

HERE = Path(__file__).resolve().parent
CHAIN = 5042
CONTRACT = Web3.to_checksum_address('0x07b5AB324fFD5f2CcCfd178B8f225E5419C5736c')
RPC = 'https://towerofbabel.fly.dev/rpc/mainnet'
FIELDS = ['laid','seed','openAt','price','target','floor','pot','closeAt','coinWeight','sweatWeight','coinIn','tillPaid','day','buildersOwed','vault']
ABI = [
 {'type':'function','name':'state','stateMutability':'view','inputs':[], 'outputs':[{'type':'tuple','components':[{'name':k,'type':'bytes32' if k=='seed' else 'uint256'} for k in FIELDS]}]},
 {'type':'function','name':'targetAt','stateMutability':'view','inputs':[{'name':'n','type':'uint256'},{'name':'coinBps','type':'uint256'}],'outputs':[{'type':'uint256'}]},
 {'type':'function','name':'lay','stateMutability':'payable','inputs':[{'name':'sponsor','type':'uint256'},{'name':'nonce','type':'uint256'}],'outputs':[{'type':'uint256'}]},
]

def kh(data): return keccak.new(digest_bits=256, data=data).digest()
def work(seed, address, nonce):
    if len(seed)!=32 or not 0 <= nonce <= MAX256: raise SafetyError('无效 PoW 输入')
    return kh(seed + bytes.fromhex(address.removeprefix('0x')) + nonce.to_bytes(32,'big'))

@dataclass(frozen=True)
class Job:
    laid: int
    seed: bytes
    target: int
    block: int = 0
    price: int = 0
    open_at: int = 1
    def identity(self): return self.laid, self.seed, self.target

class Chain:
    def __init__(self, rpc):
        if not rpc.startswith('https://'): raise SafetyError('RPC 必须使用 HTTPS')
        self.w3=Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout':10}, exception_retry_configuration=None))
        self.contract=self.w3.eth.contract(address=CONTRACT, abi=ABI)
        self.verify()
    def verify(self):
        if self.w3.eth.chain_id!=CHAIN: raise SafetyError('链 ID 不匹配，必须是 Arc mainnet 5042')
        expected=json.loads((HERE/'protocol.json').read_text())['code_sha256']
        code=bytes(self.w3.eth.get_code(CONTRACT))
        if not code or hashlib.sha256(code).hexdigest()!=expected: raise SafetyError('合约字节码与固定版本不一致')
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
    def _snapshot(self, s, block):
        if s['laid']>=8190: raise SafetyError('所有公开砖块已铸造完毕')
        if not s['openAt']: raise SafetyError('铸造尚未开放')
        target=s['target']
        if not 0<target<=MAX256: raise SafetyError('纯挖矿 target 无效')
        return Job(s['laid'],bytes(s['seed']),target,block,s['price'],s['openAt'])
    def _poll_state(self):
        # One eth_call per poll so a shared website RPC is less likely to 429.
        return self._snapshot(dict(zip(FIELDS,self.contract.functions.state().call())), 0)
    def _job(self):
        block=self.w3.eth.block_number
        s=dict(zip(FIELDS,self.contract.functions.state().call(block_identifier=block)))
        job=self._snapshot(s,block)
        target=self.contract.functions.targetAt(s['laid'],0).call(block_identifier=block)
        if target!=job.target: raise SafetyError('纯挖矿 target 与 state 不一致')
        return job
    def show(self,j,address):
        expected=(1<<256)/j.target
        log(f'Arc chain={CHAIN} block={j.block} laid={j.laid}/8190 seed=0x{j.seed.hex()}')
        log(f'纯挖矿 value=0 USDC；付费铸造参考价={fmt(j.price)} USDC；E[hashes]={expected:.6g} bits={math.log2(expected):.3f}')
        if address: log(f'钱包 {address}，余额={fmt(self.w3.eth.get_balance(address))} USDC（native，18位精度）')

class Ledger(CoreLedger):
    """Lock per chain/contract/wallet. Reservations persist before signing."""
    def __init__(self, address):
        super().__init__(address, app='babel-miner', chain_id=CHAIN, contract=CONTRACT)

def validate_tx(tx,address,nonce,gas_cap):
    expected='0x'+(kh(b'lay(uint256,uint256)')[:4]+bytes(32)+nonce.to_bytes(32,'big')).hex()
    if tx.get('chainId')!=CHAIN or tx.get('to','').lower()!=CONTRACT.lower() or tx.get('from','').lower()!=address.lower(): raise SafetyError('交易目标或发送者错误')
    if tx.get('value')!=0 or tx.get('data')!=expected: raise SafetyError('只允许 sponsor=0、value=0 的纯挖矿 lay')
    if not 0<tx['gas']<=3_000_000 or not 0<=tx['maxPriorityFeePerGas']<=tx['maxFeePerGas'] or tx['maxFeePerGas']<=0: raise SafetyError('Gas 参数无效')
    if tx['gas']*tx['maxFeePerGas']>gas_cap: raise SafetyError('单笔 Gas 预留超出上限')
    if set(tx)!={'chainId','to','from','value','data','nonce','gas','maxFeePerGas','maxPriorityFeePerGas'}: raise SafetyError('交易存在未允许的字段')

def submit(chain,job,nonce,account,ledger,args,deadline):
    chain.verify()
    fresh=chain.job(deadline=deadline)
    if fresh.identity()!=job.identity(): log('解已过期，重新挖矿');return False
    if int.from_bytes(work(job.seed,account.address,nonce),'big')>=job.target: raise SafetyError('CPU 复核失败')
    w3=chain.w3
    fn=chain.contract.functions.lay(0,nonce)
    base={'from':account.address,'value':0}
    fn.call(base)  # eth_call: simulation only, no transaction sent
    gas=math.ceil(fn.estimate_gas(base)*1.2)
    tip=w3.eth.max_priority_fee
    fee=2*w3.eth.get_block('latest')['baseFeePerGas']+tip
    tx={'chainId':CHAIN,'from':account.address,'to':CONTRACT,'value':0,'data':fn._encode_transaction_data(),
        'nonce':w3.eth.get_transaction_count(account.address,'pending'),'gas':gas,'maxFeePerGas':fee,'maxPriorityFeePerGas':tip}
    validate_tx(tx,account.address,nonce,args.max_gas_cost)
    if w3.eth.get_balance(account.address)<gas*fee: raise SafetyError('Arc USDC 余额不足以覆盖最大 Gas')
    if chain.job(deadline=deadline).identity()!=job.identity(): log('签名前任务已变化，丢弃解');return False
    if time.monotonic()>=deadline: raise SafetyError('运行时间已到，禁止签名')
    ledger.reserve(tx,args.budget,args.max_txs)
    signed=account.sign_transaction(tx)
    from core.broadcast import broadcast_once
    return broadcast_once(w3, signed, deadline=deadline, symbol='USDC', explorer_base='https://explorer.arc.io/tx',
                          reserved_cost=gas*fee, fail_noun='砖块')

class Worker:
    def __init__(self,device):
        self.device=device
        binary=HERE/'bin'/('babel-cpu' if device=='cpu' else 'babel-cuda')
        if not binary.exists(): raise SafetyError(f'请先运行 bash build.sh {"cpu" if device=="cpu" else "cuda"}')
        env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('PRIVATE','SECRET','TOKEN','PASSWORD'))}
        self.p=subprocess.Popen([str(binary)]+([] if device=='cpu' else [device]),stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,bufsize=1,env=env)
        self.prefix=secrets.token_bytes(24);self.counter=0;self.pending=None;self.sent=0
    def send(self,job,address,count,prefix=None,counter=None):
        pref=self.prefix if prefix is None else prefix
        start=self.counter if counter is None else counter
        if start+count>=1<<64: raise SafetyError('nonce 空间耗尽')
        self.pending=(job,address,count,pref,start);self.sent=time.monotonic()
        self.p.stdin.write(f'{job.seed.hex()} {address[2:]} {pref.hex()} {start} {count} {job.target:064x}\n');self.p.stdin.flush()
        if counter is None: self.counter+=count
    def read(self):
        line=self.p.stdout.readline()
        if not line: raise SafetyError(f'worker {self.device} 意外退出')
        try:
            count,n,h=line.split();count=int(count);nonce=int(n,16);digest=bytes.fromhex(h)
            job,address,requested,prefix,start=self.pending
            if not 0<count<=requested or nonce>>64!=int.from_bytes(prefix,'big') or not start<=nonce%(1<<64)<start+requested: raise ValueError()
            if digest!=work(job.seed,address,nonce): raise ValueError()
        except (ValueError,TypeError): raise SafetyError('worker 返回无效结果／Keccak 复核失败') from None
        return job,nonce,count,int.from_bytes(digest,'big')<job.target
    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:self.p.wait(timeout=2)
            except subprocess.TimeoutExpired:self.p.kill();self.p.wait()
        self.p.stdin.close();self.p.stdout.close()

def selftest(worker):
    address='0x000000000000000000000000000000000000dEaD'
    for prefix,counter in [(bytes(24),0),(bytes.fromhex('80'+'ab'*23),0x100000001),(bytes.fromhex('ff'*24),(1<<64)-17)]:
        j=Job(0,bytes.fromhex('00'*31+'01'),0)
        worker.send(j,address,16,prefix,counter)
        ready=selectors.DefaultSelector()
        try:
            ready.register(worker.p.stdout,selectors.EVENT_READ)
            if not ready.select(60): raise SafetyError('worker 自测超时')
            _,_,count,found=worker.read()
            if count!=16 or found: raise SafetyError('worker 自测失败')
        finally:ready.close()
    log(f'{worker.device} Keccak 自测通过（含高位 nonce 与跨32位计数）')

def mine(args,chain,account=None,ledger=None):
    from core.engine import run_native
    bench=args.command=='bench'
    def fetch_job(deadline=None, poll=False):
        if bench: return Job(0,bytes.fromhex('22'*32),0)
        return chain.job(deadline=deadline, poll=poll)
    def on_hit(job,address,nonce,deadline):
        log(f'HIT nonce=0x{nonce:064x} hash=0x{work(job.seed,address,nonce).hex()}')
        if args.command=='dry':
            chain.contract.functions.lay(0,nonce).call({'from':address,'value':0})
            log('DRY RUN：链上 eth_call 模拟通过，没有签名或广播')
            return
        submit(chain,job,nonce,account,ledger,args,deadline)
    run_native(
        command=args.command, address=args.address, backend=args.backend, devices=args.devices,
        threads=args.threads, seconds=args.seconds, poll=args.poll, batch=args.batch,
        fetch_job=fetch_job, show_job=(lambda job, addr: chain.show(job, addr)),
        spawn_worker=Worker, selftest=selftest, on_hit=on_hit,
        job_target=lambda j: j.target, job_label=lambda j: f'laid={j.laid}',
    )

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['check','bench','dry','live'])
    p.add_argument('--rpc',default=RPC)
    p.add_argument('--address',help='公开钱包地址；dry/live 必填，live 会核对私钥派生地址')
    p.add_argument('--backend',choices=['cpu','cuda'],default='cpu')
    p.add_argument('--devices',default='0',help='CUDA 设备索引，如 0,1,2')
    p.add_argument('--threads',type=int,default=1,help='CPU 进程数')
    p.add_argument('--seconds',type=int,default=60)
    p.add_argument('--poll',type=float,default=8)
    p.add_argument('--batch',type=int,default=0)
    p.add_argument('--max-gas-cost',type=amount,help='单笔最高 Gas 成本，单位原生 USDC')
    p.add_argument('--budget',type=amount,help='本机钱包累计 Gas 预留预算，单位 USDC')
    p.add_argument('--max-txs',type=int,help='该钱包累计签名笔数上限，含历史预留')
    return p

def main():
    args=parser().parse_args()
    if not 1<=args.seconds<=86400 or not 1<=args.threads<=128 or not 0.5<=args.poll<=60 or not 0<=args.batch<=1<<26:raise SafetyError('时长/线程/轮询/批次参数越界')
    if args.max_txs is not None and args.max_txs<1:raise SafetyError('签名笔数上限必须≥1')
    if args.address:
        if not Web3.is_address(args.address):raise SafetyError('钱包地址格式无效')
        args.address=Web3.to_checksum_address(args.address)
    if args.command in ('dry','live') and not args.address:raise SafetyError('必须指定 --address 公开钱包地址')
    if args.command=='live' and (not args.budget or not args.max_gas_cost or not args.max_txs):raise SafetyError('正式模式必须设置 --budget、--max-gas-cost 与 --max-txs')
    chain=None if args.command=='bench' else Chain(args.rpc)
    if args.command=='check':chain.show(chain.job(),args.address);return
    if args.command!='live':mine(args,chain);return
    with contextlib.closing(Ledger(args.address)) as ledger:
        records=ledger.records()
        if len(records)>=args.max_txs or sum(r['reserved'] for r in records)>=args.budget:raise SafetyError('历史预留已用尽预算或签名次数')
        chain.show(chain.job(),args.address)
        print('正式模式会按设定时长持续挖矿。预算账本按本机用户和钱包保存，不随重启清零。')
        print('每笔预留 Gas；失败、未广播、pending 均不自动释放。找到解不会结束本次运行。')
        print(f'value=0，sponsor=0；单笔Gas≤{fmt(args.max_gas_cost)} USDC；累计≤{fmt(args.budget)} USDC；累计签名≤{args.max_txs}。')
        print('不同服务器请用不同钱包；跨服务器不共享预算和 nonce 锁。')
        if input('确认真实付费输入 START: ').strip()!='START':raise SafetyError('已取消')
        from core.keys import read_private_key
        key=read_private_key('BABEL_PRIVATE_KEY','POW_PRIVATE_KEY','PRIVATE_KEY')
        try:account=chain.w3.eth.account.from_key(key)
        except Exception:raise SafetyError('私钥格式无效') from None
        finally:key=None
        if account.address.lower()!=args.address.lower():raise SafetyError('私钥派生地址与 --address 不匹配')
        mine(args,chain,account,ledger)

if __name__=='__main__':
    def stop(signum,frame):raise KeyboardInterrupt
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,stop)
    try:main()
    except KeyboardInterrupt:log('停止；云服务器租金仍会继续计费');sys.exit(130)
    except Exception as exc:
        # Never print raw RPC requests, private keys or signed transaction bytes.
        if isinstance(exc,(SafetyError,argparse.ArgumentTypeError,FileNotFoundError)):log(f'停止：{exc}')
        else:log(f'停止：{rpc_error_label(exc)}；请检查 RPC、依赖和服务器环境，未自动重试交易')
        sys.exit(1)
