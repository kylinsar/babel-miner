#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
mkdir -p bin
mode="${1:-cpu}"
if [[ "$mode" == cpu ]]; then
  "${CXX:-c++}" -O3 -std=c++17 worker.cpp -o bin/babel-cpu
elif [[ "$mode" == cuda ]]; then
  command -v nvcc >/dev/null || { echo '未找到 nvcc，请安装 CUDA Toolkit devel 版本。' >&2; exit 1; }
  mapfile -t arches < <(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | tr -d '. ' | sort -u)
  ((${#arches[@]})) || { echo '未识别到 GPU 架构' >&2; exit 1; }
  supported="$(nvcc --list-gpu-arch)"
  flags=()
  for arch in "${arches[@]}"; do
    [[ "$arch" =~ ^[0-9]+$ ]] || { echo "无效架构: $arch" >&2; exit 1; }
    grep -qx "compute_$arch" <<< "$supported" || { echo "当前 nvcc 不支持 compute_$arch；请升级 Toolkit（sm_103 至少 12.9）。" >&2; exit 1; }
    flags+=(-gencode "arch=compute_$arch,code=sm_$arch")
  done
  nvcc -O3 -std=c++17 -x cu -DBABEL_CUDA "${flags[@]}" worker.cpp -o bin/babel-cuda
else
  echo '用法: bash build.sh cpu|cuda' >&2; exit 1
fi
