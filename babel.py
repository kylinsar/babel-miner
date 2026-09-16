#!/usr/bin/env python3
"""Babel Linux miner: CPU/CUDA, read-only by default, pure-work mint only."""
import argparse
import contextlib
import fcntl
import getpass
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
import warnings
from dataclasses import dataclass
from Crypto.Hash import keccak
from requests.exceptions import HTTPError, ConnectionError, Timeout
from web3 import Web3

HERE = Path(__file__).resolve().parent
CHAIN = 5042
CONTRACT = Web3.to_checksum_address('0x07b5AB324fFD5f2CcCfd178B8f225E5419C5736c')
RPC = 'https://towerofbabel.fly.dev/rpc/mainnet'
MAX256 = (1 << 256) - 1
FIELDS = ['laid','seed','openAt','price','target','floor','pot','closeAt','coinWeight','sweatWeight','coinIn','tillPaid','day','buildersOwed','vault']
ABI = [
 {'type':'function','name':'state','stateMutability':'view','inputs':[], 'outputs':[{'type':'tuple','components':[{'name':k,'type':'bytes32' if k=='seed' else 'uint256'} for k in FIELDS]}]},
 {'type':'function','name':'targetAt','stateMutability':'view','inputs':[{'name':'n','type':'uint256'},{'name':'coinBps','type':'uint256'}],'outputs':[{'type':'uint256'}]},
 {'type':'function','name':'lay','stateMutability':'payable','inputs':[{'name':'sponsor','type':'uint256'},{'name':'nonce','type':'uint256'}],'outputs':[{'type':'uint256'}]},
]

class SafetyError(Exception): pass

def log(msg): print(time.strftime('[%H:%M:%S]'), msg, flush=True)
def kh(data): return keccak.new(digest_bits=256, data=data).digest()
def work(seed, address, nonce):
    if len(seed)!=32 or not 0 <= nonce <= MAX256: raise SafetyError('无效 PoW 输入')
    return kh(seed + bytes.fromhex(address.removeprefix('0x')) + nonce.to_bytes(32,'big'))
def amount(raw):
    if not re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,18})?', raw): raise argparse.ArgumentTypeError('请填普通正数，最多18位小数')
    whole, _, frac = raw.partition('.')
    value=int(whole)*10**18+int(frac.ljust(18,'0'))
    if value<=0: raise argparse.ArgumentTypeError('金额必须大于0')
    return value

def fmt(n):
    whole, frac = divmod(n, 10**18)
    tail = f'{frac:018d}'.rstrip('0')
    return str(whole) + ('.' + tail if tail else '')

@dataclass(frozen=True)
class Job:
    laid: int
    seed: bytes
    target: int
    block: int = 0
    price: int = 0
    open_at: int = 1
    def identity(self): return self.laid, self.seed, self.target

def rpc_error_label(exc):
    # Do not expose URL credentials, request bodies or signed transaction bytes.
    if isinstance(exc, HTTPError):
        response = exc.response
        return f'HTTP {response.status_code}' if response is not None else 'HTTPError（无状态码）'
    return type(exc).__name__

def read_retry(fn, deadline=None):
    """Only call with idempotent task reads; never wrap signing/broadcast."""
    for attempt in range(4):
        if deadline is not None and time.monotonic() >= deadline:
            raise SafetyError('运行时间已到，停止读取重试')
        try:
            return fn()
        except (HTTPError, ConnectionError, Timeout) as exc:
            status = exc.response.status_code if isinstance(exc, HTTPError) and exc.response is not None else None
            transient = not isinstance(exc, HTTPError) or status in (408, 425, 429, 500, 502, 503, 504)
            label = rpc_error_label(exc)
            if not transient or attempt == 3:
                raise SafetyError(f'RPC任务读取失败：{label}；已停止，没有自动重发交易') from None
            delay = 2 ** (attempt + 1)
            if deadline is not None:
                delay = min(delay, max(0, deadline-time.monotonic()))
            log(f'RPC任务读取失败：{label}；暂停下发计算任务，{delay:g}秒后重试（{attempt+1}/3）')
            time.sleep(delay)


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
    def job(self, deadline=None):
        return read_retry(self._job, deadline)
    def _job(self):
        block=self.w3.eth.block_number
        s=dict(zip(FIELDS,self.contract.functions.state().call(block_identifier=block)))
        if s['laid']>=8190: raise SafetyError('所有公开砖块已铸造完毕')
        if not s['openAt']: raise SafetyError('铸造尚未开放')
        target=self.contract.functions.targetAt(s['laid'],0).call(block_identifier=block)
        if target!=s['target'] or not 0<target<=MAX256: raise SafetyError('纯挖矿 target 与 state 不一致')
        return Job(s['laid'],bytes(s['seed']),target,block,s['price'],s['openAt'])
    def show(self,j,address):
        expected=(1<<256)/j.target
        log(f'Arc chain={CHAIN} block={j.block} laid={j.laid}/8190 seed=0x{j.seed.hex()}')
        log(f'纯挖矿 value=0 USDC；付费铸造参考价={fmt(j.price)} USDC；E[hashes]={expected:.6g} bits={math.log2(expected):.3f}')
        if address: log(f'钱包 {address}，余额={fmt(self.w3.eth.get_balance(address))} USDC（native，18位精度）')

class Ledger:
    """Lock per chain/contract/wallet. Reservations persist before signing."""
    def __init__(self, address):
        self.directory=Path.home()/'.local/state/babel-miner'
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        stem=f'{CHAIN}-{CONTRACT.lower()}-{address.lower()}'
        self.path=self.directory/(stem+'.jsonl')
        self.fd=os.open(self.directory/(stem+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try: fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd);raise SafetyError('本机同一钱包已有正式进程')
    def close(self): os.close(self.fd)
    def records(self):
        if not self.path.exists(): return []
        try:
            records=[json.loads(s) for s in self.path.read_text().splitlines()]
            for r in records:
                if type(r['reserved']) is not int or r['reserved']<=0 or type(r['nonce']) is not int or r['nonce']<0: raise ValueError()
            return records
        except (KeyError,ValueError,TypeError): raise SafetyError('预算账本损坏，停止签名')
    def reserve(self, tx, budget, max_txs):
        records=self.records();cost=tx['gas']*tx['maxFeePerGas']
        if len(records)>=max_txs: raise SafetyError('累计签名笔数已达上限')
        if any(r['nonce']==tx['nonce'] for r in records): raise SafetyError('nonce 已有预留，请核查历史交易')
        if sum(r['reserved'] for r in records)+cost>budget: raise SafetyError('累计 Gas 预算不足')
        record={'nonce':tx['nonce'],'reserved':cost,'time':int(time.time()),'tx':tx}
        fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as f: f.write(json.dumps(record)+'\n'); f.flush();os.fsync(f.fileno())
        fd=os.open(self.directory,os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

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
    local_hash=Web3.to_hex(kh(bytes(signed.raw_transaction)))
    # This hash is known even if the network loses the broadcast response.
    log(f'签名交易 {local_hash}；最高 Gas={fmt(gas*fee)} USDC；预留不会自动释放')
    if time.monotonic()>=deadline: raise SafetyError('签名后到期，未广播；预留保留')
    try: returned=w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception: raise SafetyError(f'广播结果未知，请查询 {local_hash}；不会自动重试或释放预算') from None
    if Web3.to_hex(returned).lower()!=local_hash.lower(): raise SafetyError('RPC 返回的交易哈希不匹配')
    log(f'已广播 https://explorer.arc.io/tx/{local_hash}')
    until=min(deadline,time.monotonic()+120)
    from web3.exceptions import TransactionNotFound
    while time.monotonic()<until:
        try: receipt=w3.eth.get_transaction_receipt(returned)
        except TransactionNotFound: time.sleep(1);continue
        if receipt['status']!=1: raise SafetyError('交易失败：Gas 已消耗，未获得砖块；程序停止')
        log(f'铸造成功 block={receipt["blockNumber"]} gasUsed={receipt["gasUsed"]}；本次运行结束')
        return True
    raise SafetyError(f'交易仍待确认：{local_hash}；停止，不重发')

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
    bench=args.command=='bench';address=args.address
    if bench: address=address or '0x'+'11'*20
    job=Job(0,bytes.fromhex('22'*32),0) if bench else chain.job()
    if not bench: chain.show(job,address)
    devices=['cpu']*args.threads if args.backend=='cpu' else args.devices.split(',')
    if args.backend=='cuda' and (not all(d.isdigit() for d in devices) or len(set(devices))!=len(devices)):raise SafetyError('devices 必须为不重复的索引，例如 0,1,2')
    workers=[];sel=selectors.DefaultSelector()
    try:
        for d in devices:
            w=Worker(d);workers.append(w);selftest(w);sel.register(w.p.stdout,selectors.EVENT_READ,w)
        batch=args.batch or (4096 if args.backend=='cpu' else 1<<20)
        start=time.monotonic();deadline=start+args.seconds;last_poll=start;last_log=start;hashes=0
        for w in workers:w.send(job,address,batch)
        while time.monotonic()<deadline:
            now=time.monotonic()
            if not bench and now-last_poll>=args.poll:
                fresh=chain.job(deadline=deadline)  # Workers finish at most their current batch, then wait.
                paused=time.monotonic()-now
                for worker in workers: worker.sent+=paused
                if fresh.identity()!=job.identity():job=fresh;log(f'新任务 laid={job.laid} E[hashes]={(1<<256)/job.target:.6g}')
                last_poll=time.monotonic()
            if time.monotonic()>=deadline:break
            for key,_ in sel.select(min(.2,max(0,deadline-time.monotonic()))):
                w=key.data;old,nonce,count,found=w.read();hashes+=count
                if time.monotonic()>=deadline:break
                if found and old.identity()==job.identity():
                    fresh=chain.job(deadline=deadline)
                    if fresh.identity()==old.identity():
                        log(f'HIT nonce=0x{nonce:064x} hash=0x{work(old.seed,address,nonce).hex()}')
                        if args.command=='dry':
                            chain.contract.functions.lay(0,nonce).call({'from':address,'value':0})
                            log('DRY RUN：链上 eth_call 模拟通过，没有签名或广播');return
                        if submit(chain,old,nonce,account,ledger,args,deadline):return
                    job=chain.job(deadline=deadline)
                w.send(job,address,batch)
            now=time.monotonic()
            if any(w.p.poll() is not None for w in workers):raise SafetyError('计算进程已退出')
            if any(now-w.sent>60 for w in workers):raise SafetyError('计算批次超过60秒；请减小 --batch')
            if now-last_log>=5:
                rate=hashes/(now-start);eta=(1<<256)/job.target/rate if job.target and rate else 0
                log(f'{len(workers)} workers {rate/1e9:.6f} GH/s hashes={hashes}'+(f' eta~{eta/3600:.2f}h（统计平均）' if job.target else ''))
                last_log=now
        elapsed=time.monotonic()-start
        print(json.dumps({'type':'bench' if bench else 'stopped','hashes':hashes,'seconds':round(elapsed,3),'hps':int(hashes/elapsed)}),flush=True)
    finally:
        for w in workers:w.close()
        sel.close()

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['check','bench','dry','live'])
    p.add_argument('--rpc',default=RPC)
    p.add_argument('--address',help='公开钱包地址；dry/live 必填，live 会核对私钥派生地址')
    p.add_argument('--backend',choices=['cpu','cuda'],default='cpu')
    p.add_argument('--devices',default='0',help='CUDA 设备索引，如 0,1,2')
    p.add_argument('--threads',type=int,default=1,help='CPU 进程数')
    p.add_argument('--seconds',type=int,default=60)
    p.add_argument('--poll',type=float,default=2)
    p.add_argument('--batch',type=int,default=0)
    p.add_argument('--max-gas-cost',type=amount,help='单笔最高 Gas 成本，单位原生 USDC')
    p.add_argument('--budget',type=amount,help='本机钱包累计 Gas 预留预算，单位 USDC')
    p.add_argument('--max-txs',type=int,default=1,help='累计签名次数；每次运行最多提交一笔')
    return p

def main():
    args=parser().parse_args()
    if not 1<=args.seconds<=86400 or not 1<=args.threads<=128 or not 0.5<=args.poll<=60 or not 0<=args.batch<=1<<26 or args.max_txs<1:raise SafetyError('时长/线程/轮询/批次/笔数参数越界')
    if args.address:
        if not Web3.is_address(args.address):raise SafetyError('钱包地址格式无效')
        args.address=Web3.to_checksum_address(args.address)
    if args.command in ('dry','live') and not args.address:raise SafetyError('必须指定 --address 公开钱包地址')
    if args.command=='live' and (not args.budget or not args.max_gas_cost):raise SafetyError('正式模式必须设置 --budget 与 --max-gas-cost（单位 USDC）')
    chain=None if args.command=='bench' else Chain(args.rpc)
    if args.command=='check':chain.show(chain.job(),args.address);return
    if args.command!='live':mine(args,chain);return
    with contextlib.closing(Ledger(args.address)) as ledger:
        records=ledger.records()
        if len(records)>=args.max_txs or sum(r['reserved'] for r in records)>=args.budget:raise SafetyError('历史预留已用尽预算或签名次数')
        chain.show(chain.job(),args.address)
        print(f'正式模式：value=0，sponsor=0；单笔Gas≤{fmt(args.max_gas_cost)} USDC；累计≤{fmt(args.budget)} USDC。')
        print('最多提交一笔后退出。不同服务器的预算/nonce不共享；请勿多机使用同一私钥。')
        if input('允许真实支付 Gas，请输入 START: ').strip()!='START':raise SafetyError('已取消')
        with warnings.catch_warnings():
            warnings.simplefilter('error',getpass.GetPassWarning)
            key=getpass.getpass('独立钱包私钥（隐藏输入，不保存）: ').strip()
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
