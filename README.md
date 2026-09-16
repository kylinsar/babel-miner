# pow-control

统一 PoW 内核：同一套菜单、账本和签名闸门，Babel、Parsec 与 Hashcats 作为插件接入。内核拥有私钥与广播；插件只提供任务、PoW 和交易形状。签名前预留、不重试广播、HTTPS RPC 这些规则插件改不了。

生产环境里单独钉死的 Babel GPU 客户端仍是 https://github.com/kylinsar/babel-miner （例如 `d5dcbee`）。本仓库是带插件内核的新版本。

Babel 插件针对 https://towerofbabel.fly.dev/mine 。Parsec 插件针对 https://parsecxyz.fun/ 的 Scan · PoW。**这是功能初版，不是已完成 GPU 性能优化或完整合约审计的版本。** 各插件不共用合约、RPC、私钥账本或付费规则。

## 网络与费用

- Arc 主网，chain ID `5042`。
- 固定 tower 合约：`0x07b5AB324fFD5f2CcCfd178B8f225E5419C5736c`。
- 固定部署字节码 SHA-256 见 `protocol.json`；不从网页自动更新合约。
- 算法：`keccak256(seed[32] || sender[20] || nonce_be[32]) < targetAt(laid, 0)`。不是标准 SHA3-256。
- 只实现纯挖矿：`lay(0, nonce)`，`value=0`，无 sponsor。不实现付费、混合铸造、批量购买、burn、claim 或代币批准。
- 正式模式需要 **Arc 主网原生 USDC 支付 Gas**。RPC原生余额和交易费用使用18位精度；不是ETH，也不是ERC20 transfer的6位精度。
- 支付Gas不保证铸造成功；找到解后其他人先铸造可能使交易失败。
- 官方页面显示纯挖矿砖块分享独立的7%挖矿奖励池；这不是年化收益或保证收益。资产可交易性未在本脚本中验证。

## Parsec（Scan · PoW）

- 网站 https://parsecxyz.fun/ 。Arc 主网，chain ID `5042`。
- 固定合约：`0x631f96907126Ae23313Ec8DF489da0879AACfC98`（PARSEC Deep Field Survey，8800 个 ERC-721）。
- 固定部署字节码 SHA-256 见 `parsec-protocol.json`。
- 算法：`keccak256(entropy[32] || sender[20] || nonce_be[32]) <= proofTarget(requiredWorkFor(tokenId, 0))`。entropy 在部署时写死。复用 Babel 的 CPU/CUDA Keccak worker。
- 只实现纯扫描：`mint(nonce)`，`value=0`。不实现 Buy（付全价免 PoW）、Combine（部分付款 + PoW），也不实现 watch 奖池猎分。
- 正式模式同样用 Arc 原生 USDC 付 Gas；找到解后别人先 mint 仍可能失败。当前难度大约数万亿次哈希，以链上 `requiredWorkFor` 为准。

## Linux 安装（Ubuntu / Debian）

从 GitHub 下载，然后安装依赖：

```bash
mkdir -p /workspace
cd /workspace
git clone https://github.com/kylinsar/pow-control.git
cd pow-control
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
bash pow-menu.sh
# 也可 POW_PLUGIN=parsec 或 POW_PLUGIN=hashcats bash pow-menu.sh 覆盖已保存的项目
# 只跑 Babel 时也可以：bash babel-menu.sh
```

新GPU服务器顺序：1系统信息 → 2设置（项目、cuda、GPU、RPC、地址、秒数，长期保存） → 3安装 → 4编译 → 5测速 → 6链上检查 → 7不付费试运行 → 8正式。

GPU编号默认0，多卡必须填 `0,1,2,3` 等实际CUDA索引；MIG需核对CUDA可见设备，不能用显卡商品名称推断数量。
选项2把非机密设置写到 `~/.local/state/pow-control/menu.conf`。不保存私钥，也不保存正式模式的预算/签名上限。私钥可选用环境变量，未设置时仍隐藏输入：

```bash
export PRIVATE_KEY=0x你的64位十六进制私钥
# 或 POW_PRIVATE_KEY / BABEL_PRIVATE_KEY / PARSEC_PRIVATE_KEY / HASHCATS_PRIVATE_KEY
```

## 命令行：测试

```bash
# 完全离线，不使用钱包、不访问RPC、不支付任何链上费用
.venv/bin/python -I babel.py bench --backend cpu --threads 4 --seconds 60
.venv/bin/python -I babel.py bench --backend cuda --devices 0 --seconds 60
.venv/bin/python -I babel.py bench --backend cuda --devices 0,1,2,3 --seconds 300

# 只读检查网络、固定合约、seed和精确target，显示余额
.venv/bin/python -I babel.py check --address 0x你的公开钱包地址

# 实际链上任务；找到结果后使用eth_call模拟，不签名、不广播
.venv/bin/python -I babel.py dry --backend cuda --devices 0 --address 0x你的公开钱包地址 --seconds 300
.venv/bin/python -I parsec.py check --rpc https://rpc.mainnet.arc.io --address 0x你的公开钱包地址
.venv/bin/python -I parsec.py dry --backend cuda --devices 0 --address 0x你的公开钱包地址 --seconds 300
```

`dry`时间内未找到解不算失败，只表示未验证到具体的铸造模拟；`check`和算力正常也不代表已经验证真实交易。

## 命令行：正式纯挖矿

下面的 **0.1 USDC单笔上限、1 USDC累计预算只是填写示例，不是当前Gas报价**。若超过上限程序会停，不会自行提高预算。

```bash
.venv/bin/python -I babel.py live \
  --backend cuda --devices 0 \
  --rpc https://rpc.mainnet.arc.io \
  --address 0x你的公开钱包地址 \
  --seconds 86400 \
  --max-gas-cost 0.1 \
  --budget 1 \
  --max-txs 1
```

输入START后读取私钥：已设置 `PRIVATE_KEY`（或 `POW_PRIVATE_KEY` / `BABEL_PRIVATE_KEY` / `PARSEC_PRIVATE_KEY`）则用环境变量，否则隐藏输入。脚本核对私钥派生地址必须等于 `--address`。私钥不写入配置和日志，也不放进命令参数。不要把私钥发给别人。

- 按 `--seconds` 持续挖矿；找到解、铸造成功都不会结束本次运行。广播失败／等待超时／结果未知会停止，且不自动重发。
- `--max-txs` 是本机钱包累计签名次数，含历史；增加到账本上限才停止新签名，不是“本次一笔”。
- Gas由RPC估算，用120% gas limit及`2×baseFee+priorityFee`作为预留；签名前同时检查单笔限额、累计预算和余额。
- `~/.local/state/babel-miner/` 或 `~/.local/state/parsec-miner/` 中的钱包账本在签名前fsync保存预留。失败、未广播和pending不自动释放；不要通过删除账本绕过未确认交易。
- 同一机器同一用户的钱包锁阻止重复正式进程；**不同服务器、不同容器或不同用户不共享锁和预算**。当前未实现集中签名服务。
- 不要让同一GPU同时运行多个矿工；本脚本不自动停止其他程序，也不提供跨程序GPU锁。
- SIGINT/SIGTERM/SIGHUP会清理本次计算进程；断线可能终止前台程序。需长跑时使用tmux并从会话分离，不要关闭云实例后假设任务仍在。
- 计时从worker自测完成后开始；网络请求有10秒超时，退出可能略晚于设定时间；重试等待计入运行时长，到期不再重试。没有到期后自动提高预算或重新签名。
- 停止挖矿不等于停止云平台计费。

## 调参与错误

`--batch`：单个worker每次计算的hash数，CPU默认4096、GPU默认1048576。批次太大意味着对新任务反应慢；超过60秒没有结果会停止。GPU性能尚未优化，先做60秒和300秒实测，不承诺Hashcats相同算力。

`--poll`：链上更新间隔，默认8秒，最低0.5秒。Babel 常规轮询每次只读 `state()`；Parsec 轮询 `paused` / `nextTokenId` / `requiredWorkFor`。HTTP 429 等瞬时错误不停止挖矿，GPU继续当前任务，并按至少15秒退避后再查。连续180秒都读不到链上状态才停止。找到解或签名前仍做完整校验；有运行截止时间时瞬时读错误会退避重试到截止，没有截止时间时最多3次。签名和交易广播不自动重试。Babel 网站默认 RPC `https://towerofbabel.fly.dev/rpc/mainnet` 是共享代理，容易429；可改官方 `https://rpc.mainnet.arc.io`。Parsec 默认用官方 RPC。

`HIT`只是找到PoW，`DRY RUN`只是模拟；只有正式模式的成功交易回执才报告铸造成功。广播前会打印已签名交易的本地哈希，若广播响应丢失，按这个哈希检查浏览器，不自动重发。

## 插件内核（pow.py）

同一套菜单、账本和签名闸门，项目差异放在 `plugins/`：

| 插件 | 引擎 | 说明 |
|---|---|---|
| `babel` | native | Arc Tower of Babel 纯挖矿 |
| `parsec` | native | Arc PARSEC Scan · PoW，复用同一套 Keccak worker |
| `hashcats` | upstream | 调用旁边的 `hashcats-control` 监管器，不共用合约/RPC/私钥 |

```bash
bash pow-menu.sh
# 项目、GPU、RPC 等在菜单 2 设置里长期保存
python3 -I pow.py babel check --rpc https://rpc.mainnet.arc.io
python3 -I pow.py parsec check --rpc https://rpc.mainnet.arc.io
python3 -I pow.py hashcats install
python3 -I pow.py hashcats bench --rpc https://rpc.mainnet.chain.robinhood.com --devices all
python3 -I pow.py hashcats live --rpc https://rpc.mainnet.chain.robinhood.com --devices 0,1
```

上游矿工程序目录默认 `/workspace/adssdadas`，可用 `HASHCATS_WORKDIR` 覆盖。

内核拥有时长循环、私钥与广播；插件只提供任务、PoW 和交易形状。找到解不会结束本次运行。签名前预留、累计预算/笔数、不重试广播、HTTPS RPC 这些规则插件改不了。Babel 的 native 循环在 `core/engine.py`，广播只走 `core/broadcast.py` 的一次 `send_raw_transaction`。

## 验证与限制

```bash
bash build.sh cpu
.venv/bin/python -m unittest discover -s tests -v
```

已经做了：CPU原生编译与测速、与PyCryptodome独立Keccak实现对照、与网站JavaScript实现的向量对照、只读Arc主网state/targetAt验证、模拟签名/广播的预算保护测试。

开发机没有NVIDIA CUDA设备：**未完成Linux GPU实机编译/测速，也没有使用用户私钥或发送真实付费交易。** 每个worker启动时都会先跑包含高位nonce和计数进位的自测；但这不能代替Linux GPU实机验证。

依赖顶层版本固定，传递依赖由pip解析；Python环境需从可信PyPI安装。代码字节码固定不是对项目经济模型、管理员权限或协议资产安全的完整审计。
