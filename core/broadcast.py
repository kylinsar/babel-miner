"""Sign-once broadcast. Never retry send_raw_transaction."""
import time
from Crypto.Hash import keccak
from web3 import Web3
from web3.exceptions import TransactionNotFound
from core.safety import SafetyError, log, fmt


def keccak256(data):
    return keccak.new(digest_bits=256, data=data).digest()


def broadcast_once(w3, signed, *, deadline, symbol, explorer_base, reserved_cost, fail_noun='资产', cost_label='最高 Gas'):
    raw = signed.raw_transaction
    local_hash = Web3.to_hex(keccak256(bytes(raw)))
    log(f'签名交易 {local_hash}；{cost_label}={fmt(reserved_cost)} {symbol}；预留不会自动释放')
    if time.monotonic() >= deadline:
        raise SafetyError('签名后到期，未广播；预留保留')
    try:
        returned = w3.eth.send_raw_transaction(raw)
    except Exception:
        raise SafetyError(f'广播结果未知，请查询 {local_hash}；不会自动重试或释放预算') from None
    if Web3.to_hex(returned).lower() != local_hash.lower():
        raise SafetyError('RPC 返回的交易哈希不匹配')
    log(f'已广播 {explorer_base.rstrip("/")}/{local_hash}')
    until = min(deadline, time.monotonic() + 120)
    while time.monotonic() < until:
        try:
            receipt = w3.eth.get_transaction_receipt(returned)
        except TransactionNotFound:
            time.sleep(1)
            continue
        if receipt['status'] != 1:
            raise SafetyError(f'交易失败：Gas 已消耗，未获得{fail_noun}；程序停止')
        log(f'铸造成功 block={receipt["blockNumber"]} gasUsed={receipt["gasUsed"]}；继续挖矿直到时长或预算用尽')
        return True
    raise SafetyError(f'交易仍待确认：{local_hash}；停止，不重发')
