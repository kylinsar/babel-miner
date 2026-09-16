#!/usr/bin/env bash
# Unified menu. Option 2 persists non-secret settings; private keys are never saved.
set -u
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
py="$PWD/.venv/bin/python"
state_dir="${POW_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/pow-control}"
config_file="$state_dir/menu.conf"
plugin=babel
backend=cpu
devices=0
threads=1
address=""
seconds=60
workdir="${HASHCATS_WORKDIR:-/workspace/adssdadas}"
rpc="https://towerofbabel.fly.dev/rpc/mainnet"

run() { [[ -x "$py" ]] || { echo '请先选择 3 安装'; return 1; }; HASHCATS_WORKDIR="$workdir" "$py" -I "$PWD/pow.py" "$plugin" "$@"; }

set_plugin_meta() {
  case "$plugin" in
    babel)
      title="Babel"
      live_hint='只支付网络 Gas；不会支付砖块铸造价。金额单位是 Arc 原生 USDC，不是 ETH。'
      cap_prompt='单笔 Gas 最高成本 USDC（必填）: '
      budget_prompt='本机钱包累计 Gas 预算 USDC（必填）: '
      rpc_hint="HTTPS RPC（回车保留；网站代理易429，可改 https://rpc.mainnet.arc.io）: "
      default_rpc="https://towerofbabel.fly.dev/rpc/mainnet"
      ;;
    parsec)
      title="Parsec"
      live_hint='只支付网络 Gas；不会支付发现铸造价。金额单位是 Arc 原生 USDC，不是 ETH。不实现 Buy / Combine。'
      cap_prompt='单笔 Gas 最高成本 USDC（必填）: '
      budget_prompt='本机钱包累计 Gas 预算 USDC（必填）: '
      rpc_hint="HTTPS RPC（回车保留）: "
      default_rpc="https://rpc.mainnet.arc.io"
      ;;
    hashcats)
      title="Hashcats"
      live_hint='正式模式会支付铸造价 + Gas，单位 ETH。时长内的费率/预算由上游监管器询问。'
      cap_prompt=''
      budget_prompt=''
      rpc_hint="HTTPS RPC（回车保留）: "
      default_rpc="https://rpc.mainnet.chain.robinhood.com"
      ;;
    *) echo "未知项目 $plugin"; return 1 ;;
  esac
}

plugin_defaults() {
  set_plugin_meta || return 1
  if [[ "$plugin" == hashcats ]]; then
    backend=cuda; devices=all; threads=1
  else
    backend=cpu; devices=0; threads=1
  fi
  rpc="$default_rpc"
}

save_config() {
  mkdir -p -m 700 "$state_dir" || return 1
  local tmp="$config_file.tmp.$$"
  umask 077
  printf '%s\n' \
    "plugin=$plugin" \
    "backend=$backend" \
    "devices=$devices" \
    "threads=$threads" \
    "rpc=$rpc" \
    "address=$address" \
    "seconds=$seconds" \
    "workdir=$workdir" > "$tmp" || return 1
  mv "$tmp" "$config_file"
  chmod 600 "$config_file" 2>/dev/null || true
  echo "已保存到 $config_file （不含私钥、不含正式预算）"
}

load_config() {
  [[ -f "$config_file" ]] || return 0
  local key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    case "$key" in
      plugin) case "$val" in babel|parsec|hashcats) plugin=$val ;; esac ;;
      backend) case "$val" in cpu|cuda) backend=$val ;; esac ;;
      devices)
        val="${val//，/,}"; val="${val//、/,}"; val="${val// /}"
        if [[ "$val" == all || "$val" =~ ^[0-9]+(,[0-9]+)*$ ]]; then devices=$val; fi
        ;;
      threads) [[ "$val" =~ ^[0-9]+$ ]] && (( val>=1 && val<=128 )) && threads=$val ;;
      rpc) case "$val" in https://*) rpc=$val ;; esac ;;
      address) [[ -z "$val" || "$val" =~ ^0x[0-9a-fA-F]{40}$ ]] && address=$val ;;
      seconds) [[ "$val" =~ ^[0-9]+$ ]] && (( val>=1 && val<=86400 )) && seconds=$val ;;
      workdir) [[ "$val" == /* ]] && workdir=$val ;;
    esac
  done < "$config_file"
}

edit_settings() {
  echo
  echo "设置长期保存在 $config_file"
  echo '不把私钥写入配置。正式预算/笔数上限每次手填。'
  echo '私钥可设环境变量 PRIVATE_KEY（或 POW_PRIVATE_KEY / BABEL_PRIVATE_KEY / PARSEC_PRIVATE_KEY / HASHCATS_PRIVATE_KEY）。'
  echo '1 Babel（Arc，只付 Gas）'
  echo '2 Hashcats（Robinhood，铸造价 + Gas）'
  echo '3 Parsec（Arc，Scan · PoW，只付 Gas）'
  local entered pick old=$plugin
  read -r -p "项目（回车保留 ${plugin}）: " pick || exit 0
  case "${pick:-}" in
    '' ) ;;
    1|babel|Babel) plugin=babel ;;
    2|hashcats|Hashcats) plugin=hashcats ;;
    3|parsec|Parsec|PARSEC) plugin=parsec ;;
    *) echo '无效项目'; return 1 ;;
  esac
  if [[ "$plugin" != "$old" ]]; then
    plugin_defaults || return 1
  else
    set_plugin_meta || return 1
  fi
  read -r -p "后端 cpu/cuda（回车保留 ${backend}）: " entered || exit 0
  case "${entered:-$backend}" in cpu|cuda) backend="${entered:-$backend}" ;; *) echo '无效后端'; return 1 ;; esac
  if [[ "$plugin" == hashcats && "$backend" == cpu ]]; then
    echo 'Hashcats 上游矿工只支持 cuda'; backend=cuda
  fi
  if [[ "$backend" == cuda ]]; then
    if [[ "$plugin" == hashcats ]]; then
      read -r -p "CUDA GPU（all 或英文逗号索引，回车保留 ${devices}）: " entered || exit 0
    else
      read -r -p "CUDA GPU索引，英文逗号分隔（回车保留 ${devices}）: " entered || exit 0
    fi
    if [[ -n "${entered:-}" ]]; then
      entered="${entered//，/,}"; entered="${entered//、/,}"; entered="${entered// /}"
      devices="$entered"
    fi
  else
    read -r -p "CPU进程数（回车保留 ${threads}）: " entered || exit 0
    threads="${entered:-$threads}"
  fi
  read -r -p "$rpc_hint" entered || exit 0
  if [[ -n "${entered:-}" ]]; then
    case "$entered" in
      https://*) rpc="$entered" ;;
      *) echo 'RPC 必须是不含凭据的 https:// 地址'; return 1 ;;
    esac
  fi
  read -r -p "钱包公开地址（回车保留 ${address:-空}）: " entered || exit 0
  if [[ -n "${entered:-}" ]]; then
    if [[ "$entered" =~ ^0x[0-9a-fA-F]{40}$ ]]; then address="$entered"
    else echo '地址必须是 0x 加 40 位十六进制'; return 1
    fi
  fi
  read -r -p "默认运行秒数（回车保留 ${seconds}）: " entered || exit 0
  if [[ -n "${entered:-}" ]]; then
    if [[ "$entered" =~ ^[0-9]+$ ]] && (( entered>=1 && entered<=86400 )); then seconds=$entered
    else echo '秒数必须是 1～86400'; return 1
    fi
  fi
  if [[ "$plugin" == hashcats ]]; then
    read -r -p "Hashcats 源码目录（回车保留 ${workdir}）: " entered || exit 0
    if [[ -n "${entered:-}" ]]; then
      if [[ "$entered" == /* ]]; then workdir="$entered"
      else echo '需要绝对路径'; return 1
      fi
    fi
  fi
  save_config
}

load_config
if [[ -n "${POW_PLUGIN:-}" ]]; then plugin="$POW_PLUGIN"; fi
if [[ -f "$config_file" ]]; then
  set_plugin_meta || exit 1
else
  plugin_defaults || exit 1
fi

while true; do
  echo
  if [[ "$backend" == cuda ]]; then echo "$title | CUDA GPU=$devices | ${seconds}s"; else echo "$title | CPU进程=$threads | ${seconds}s"; fi
  echo "RPC $rpc"
  [[ -n "$address" ]] && echo "地址 $address"
  echo '1 系统信息  2 设置  3 安装  4 编译'
  echo '5 测速  6 链上检查  7 不付费试运行  8 正式挖矿  0 退出'
  read -r -p '选择: ' choice || exit 0
  case "$choice" in
    1)
      echo "主机 $(uname -n)  $(uname -srm)"
      echo "配置 ${config_file}"
      python3 --version
      command -v c++
      command -v nvcc >/dev/null && nvcc --version
      command -v nvidia-smi >/dev/null && nvidia-smi -L
      ;;
    2) edit_settings || true ;;
    3)
      python3 -m venv .venv && "$py" -m pip install -r requirements.txt
      if [[ "$plugin" == hashcats ]]; then run install; fi
      ;;
    4)
      if [[ "$plugin" == hashcats ]]; then run --build "$backend"
      else bash build.sh "$backend"
      fi
      ;;
    5|6|7|8)
      mode=bench
      [[ "$choice" == 6 ]] && mode=check
      [[ "$choice" == 7 ]] && mode=dry
      [[ "$choice" == 8 ]] && mode=live
      if [[ "$plugin" == hashcats ]]; then
        run "$mode" --rpc "$rpc" --devices "$devices"
        continue
      fi
      if [[ "$mode" == check ]]; then
        if [[ -n "$address" ]]; then run check --rpc "$rpc" --address "$address"; else run check --rpc "$rpc"; fi
        continue
      fi
      read -r -p "运行秒数（回车保留 ${seconds}）: " entered || exit 0
      if [[ -n "${entered:-}" ]]; then seconds=$entered; save_config >/dev/null; fi
      args=("$mode" --backend "$backend" --devices "$devices" --threads "$threads" --seconds "$seconds" --rpc "$rpc")
      if [[ "$mode" != bench ]]; then
        read -r -p "钱包公开地址${address:+（回车保留 ${address}）}: " entered || exit 0
        if [[ -n "${entered:-}" ]]; then address=$entered; save_config >/dev/null; fi
        args+=(--address "$address")
      fi
      if [[ "$mode" == live ]]; then
        echo "$live_hint"
        echo '按设定秒数持续挖矿；找到解不会结束本次运行。签名受累计预算和笔数上限约束。'
        read -r -p "$cap_prompt" cap || exit 0
        read -r -p "$budget_prompt" budget || exit 0
        read -r -p '该钱包累计签名笔数上限（必填）: ' maxtxs || exit 0
        args+=(--max-gas-cost "$cap" --budget "$budget" --max-txs "$maxtxs")
      fi
      run "${args[@]}"
      ;;
    0) exit 0 ;;
    *) echo '无效选项' ;;
  esac
done
