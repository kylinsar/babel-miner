# Babel Linux CPU / NVIDIA CUDA miner

针对 https://towerofbabel.fly.dev/mine 的独立客户端。2026-09-16 根据网站公开部署配置、ABI、CPU/WebGPU算法实现。

**这是功能初版，不是已完成 GPU 性能优化或完整合约审计的版本。** 不沿用 Hashcats 的合约、RPC、私钥配置、付费规则或算力预测。

## 网络与费用

- Arc 主网，chain ID `5042`。
- 固定 tower 合约：`0x07b5AB324fFD5f2CcCfd178B8f225E5419C5736c`。
- 固定部署字节码 SHA-256 见 `protocol.json`；不从网页自动更新合约。
- 算法：`keccak256(seed[32] || sender[20] || nonce_be[32]) < targetAt(laid, 0)`。不是标准 SHA3-256。
- 只实现纯挖矿：`lay(0, nonce)`，`value=0`，无 sponsor。不实现付费、混合铸造、批量购买、burn、claim 或代币批准。
- 正式模式需要 **Arc 主网原生 USDC 支付 Gas**。RPC原生余额和交易费用使用18位精度；不是ETH，也不是ERC20 transfer的6位精度。
- 支付Gas不保证铸造成功；找到解后其他人先铸造可能使交易失败。
- 官方页面显示纯挖矿砖块分享独立的7%挖矿奖励池；这不是年化收益或保证收益。资产可交易性未在本脚本中验证。

## Linux 安装（Ubuntu / Debian）

从 GitHub 下载，然后安装依赖：

```bash
mkdir -p /workspace
cd /workspace
git clone https://github.com/kylinsar/babel-miner.git
cd babel-miner
apt-get update
apt-get install -y python3 python3-venv python3-pip build-essential
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
bash build.sh cpu
```

NVIDIA服务器需使用带 `nvcc` 的 CUDA **devel** 环境。驱动正常不代表装了编译器。

```bash
nvcc --version
nvidia-smi -L
bash build.sh cuda
```

构建会查询实际显卡架构并检查 nvcc 支持情况。若出现 compute_103 不支持，使用 CUDA Toolkit 12.9 或更新的兼容版本。不要修改目标架构来掩盖不支持。

## 交互菜单

```bash
bash babel-menu.sh
```

新GPU服务器顺序：1环境检查 → 2安装依赖 → 8设置后端cuda和GPU编号 → 3编译 → 4测速 → 5链上检查 → 6不付费试运行 → 7正式。

GPU编号默认0，多卡必须填 `0,1,2,3` 等实际CUDA索引；MIG需核对CUDA可见设备，不能用显卡商品名称推断数量。
菜单不保存私钥或设置；预算账本单独永久保留。

## 命令行：测试

```bash
# 完全离线，不使用钱包、不访问RPC、不支付任何链上费用
.venv/bin/python -I babel.py bench --backend cpu --threads 4 --seconds 60
.venv/bin/python -I babel.py bench --backend cuda --devices 0 --seconds 60
.venv/bin/python -I babel.py bench --backend cuda --devices 0,1,2,3 --seconds 300

# 只读检查网络、固定合约、seed和精确target，显示余额
.venv/bin/python -I babel.py check --address 0x你的公开钱包地址

# 实际链上任务；找到结果后使用eth_call模拟，成功后退出，不签名、不广播
.venv/bin/python -I babel.py dry --backend cuda --devices 0 --address 0x你的公开钱包地址 --seconds 300
```

`dry`时间内未找到解不算失败，只表示未验证到具体的铸造模拟；`check`和算力正常也不代表已经验证真实交易。

## 命令行：正式纯挖矿

下面的 **0.1 USDC单笔上限、1 USDC累计预算只是填写示例，不是当前Gas报价**。若超过上限程序会停，不会自行提高预算。

```bash
.venv/bin/python -I babel.py live \
  --backend cuda --devices 0 \
  --address 0x你的公开钱包地址 \
  --seconds 86400 \
  --max-gas-cost 0.1 \
  --budget 1 \
  --max-txs 1
```

输入START后，在隐藏提示中输入独立钱包私钥。脚本核对私钥派生地址必须等于 `--address`。私钥不通过命令参数或环境变量传入，不写入日志。不要把私钥发给别人。

- 每次运行最多广播一笔交易，确认后退出；失败／等待超时／广播结果未知也停止。
- `--max-txs` 是本机钱包累计签名次数，含历史；增加它意味着额外授权。
- Gas由RPC估算，用120% gas limit及`2×baseFee+priorityFee`作为预留；签名前同时检查单笔限额、累计预算和余额。
- `~/.local/state/babel-miner/` 中的钱包账本在签名前fsync保存预留。失败、未广播和pending不自动释放；不要通过删除账本绕过未确认交易。
- 同一机器同一用户的钱包锁阻止重复正式进程；**不同服务器、不同容器或不同用户不共享锁和预算**。当前未实现集中签名服务。
- 不要让同一GPU同时运行Hashcats和Babel；本脚本不自动停止其他程序，也不提供跨程序GPU锁。
- SIGINT/SIGTERM/SIGHUP会清理本次计算进程；断线可能终止前台程序。需长跑时使用tmux并从会话分离，不要关闭云实例后假设任务仍在。
- 计时从worker自测完成后开始；网络请求有10秒超时，退出可能略晚于设定时间；重试等待计入运行时长，到期不再重试。没有到期后自动提高预算或重新签名。
- 停止挖矿不等于停止云平台计费。

## 调参与错误

`--batch`：单个worker每次计算的hash数，CPU默认4096、GPU默认1048576。批次太大意味着对新任务反应慢；超过60秒没有结果会停止。GPU性能尚未优化，先做60秒和300秒实测，不承诺Hashcats相同算力。

`--poll`：链上更新间隔，默认2秒，最低0.5秒。RPC任务读取遇到连接错误、超时或 HTTP 408/425/429/500/502/503/504 时，暂停下发新计算任务（当前批次完成后等待），按2、4、8秒间隔最多重试3次；恢复后重新读取完整快照，旧解提交前再次校验。其他HTTP错误或重试耗尽则停止并清理worker。签名和交易广播不自动重试；Web3底层隐式重试已关闭。

`HIT`只是找到PoW，`DRY RUN`只是模拟；只有正式模式的成功交易回执才报告铸造成功。广播前会打印已签名交易的本地哈希，若广播响应丢失，按这个哈希检查浏览器，不自动重发。

## 验证与限制

```bash
bash build.sh cpu
.venv/bin/python -m unittest discover -s tests -v
```

已经做了：CPU原生编译与测速、与PyCryptodome独立Keccak实现对照、与网站JavaScript实现的向量对照、只读Arc主网state/targetAt验证、模拟签名/广播的预算保护测试。

开发机没有NVIDIA CUDA设备：**未完成Linux GPU实机编译/测速，也没有使用用户私钥或发送真实付费交易。** 每个worker启动时都会先跑包含高位nonce和计数进位的自测；但这不能代替Linux GPU实机验证。

依赖顶层版本固定，传递依赖由pip解析；Python环境需从可信PyPI安装。代码字节码固定不是对项目经济模型、管理员权限或协议资产安全的完整审计。
