#!/usr/bin/env bash
set -u
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
backend=cpu
devices=0
threads=1
address=""
rpc="https://towerofbabel.fly.dev/rpc/mainnet"
py="$PWD/.venv/bin/python"
run() { [[ -x "$py" ]] || { echo '请先选择 2 安装依赖'; return 1; }; "$py" -I "$PWD/babel.py" "$@"; }
while true; do
  echo
  if [[ "$backend" == cuda ]]; then echo "Babel | CUDA GPU=$devices"; else echo "Babel | CPU进程=$threads"; fi
  echo "RPC $rpc"
  echo '1 环境检查  2 安装依赖  3 编译  4 测速  5 链上检查'
  echo '6 不付费试运行  7 正式纯挖矿  8 设置  0 退出'
  read -r -p '选择: ' choice || exit 0
  case "$choice" in
    1) python3 --version; command -v c++; command -v nvcc >/dev/null && nvcc --version; command -v nvidia-smi >/dev/null && nvidia-smi -L ;;
    2) python3 -m venv .venv && "$py" -m pip install -r requirements.txt ;;
    3) bash build.sh "$backend" ;;
    4|6|7)
      mode=bench
      [[ "$choice" == 6 ]] && mode=dry
      [[ "$choice" == 7 ]] && mode=live
      read -r -p '运行秒数（默认60，最高86400）: ' seconds || exit 0
      args=("$mode" --backend "$backend" --devices "$devices" --threads "$threads" --seconds "${seconds:-60}" --rpc "$rpc")
      if [[ "$mode" != bench ]]; then
        read -r -p "钱包公开地址${address:+（回车保留 $address）}: " entered || exit 0
        address="${entered:-$address}";args+=(--address "$address")
      fi
      if [[ "$mode" == live ]]; then
        echo '只支付网络 Gas；不会支付砖块铸造价。金额单位是 Arc 原生 USDC，不是 ETH。'
        read -r -p '单笔 Gas 最高成本 USDC（必填）: ' cap || exit 0
        read -r -p '本机钱包累计 Gas 预算 USDC（必填）: ' budget || exit 0
        read -r -p '累计签名次数上限（默认1，含历史）: ' maxtxs || exit 0
        args+=(--max-gas-cost "$cap" --budget "$budget" --max-txs "${maxtxs:-1}")
      fi
      run "${args[@]}" ;;
    5) if [[ -n "$address" ]]; then run check --rpc "$rpc" --address "$address"; else run check --rpc "$rpc"; fi ;;
    8)
      read -r -p '后端 cpu/cuda（默认cuda）: ' entered || exit 0
      case "${entered:-cuda}" in cpu|cuda) backend="${entered:-cuda}";; *) echo '无效后端';continue;; esac
      if [[ "$backend" == cuda ]]; then
      read -r -p 'CUDA GPU索引，逗号分隔（默认0，例如0,1,2,3）: ' devices || exit 0
      devices="${devices:-0}"
      else
      read -r -p 'CPU进程数（默认1）: ' threads || exit 0
      threads="${threads:-1}"
      fi
      read -r -p "HTTPS RPC（回车保留；网站代理易429，可改 https://rpc.mainnet.arc.io）: " entered || exit 0
      if [[ -n "${entered:-}" ]]; then
        case "$entered" in
          https://*) rpc="$entered" ;;
          *) echo 'RPC 必须是不含凭据的 https:// 地址' ;;
        esac
      fi ;;
    0) exit 0 ;;
    *) echo '无效选项' ;;
  esac
done
