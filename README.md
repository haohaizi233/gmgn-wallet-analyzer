# GMGN 钱包分析工具 V5

Windows 桌面程序：输入一个或多个 Solana 钱包地址，通过 **Helius Free + DEX Screener + 链上 RPC** 做可验证分析，GMGN / Moralis 仅为可选交叉验证。

核心原则：**准确、完整、可解释、可恢复、Excel 0 空字段**。Token 主键永远是 `(chain, token_address)`。

## 1. 项目用途

Solana Wallet Multi-Source Verifiable Analyzer：

- 报告周期内涉及哪些 Token
- 每个 Token 的**完整历史 First Buy**（链上 blockTime / token delta 裁决）
- 无 Buy 时识别 TransferIn / Bridge / Wrapped，禁止把转入写成 Buy
- 本地 FIFO 为主；GMGN 利润仅作对照
- 异常/缺失字段可审计

## 2. Python 版本

Python **3.12+**（当前开发环境可用 3.13）。

## 3. 安装依赖

```bat
pip install -r requirements.txt
```

## 4. 申请 API Key

最低配置（默认免费路径）：

1. [Helius](https://www.helius.dev/) Free API Key
2. DEX Screener 无需 Key
3. GMGN Key **可选**
4. Moralis Key **可选，默认关闭**

不要填写区块链钱包私钥。

## 5. .env 配置

```bat
copy .env.example .env
```

```
HELIUS_API_KEY=你的helius_key
HELIUS_TARGET_RPS=8
HELIUS_MONTHLY_CREDIT_BUDGET=1000000

GMGN_API_KEYS=
ENABLE_GMGN=true
ENABLE_GMGN_DEEP_HISTORY_FALLBACK=false

ENABLE_MORALIS=false
MORALIS_API_KEY=
MORALIS_API_KEYS=

VERIFICATION_MODE=BALANCED
SOLANA_RPC_URL=
```

有 `HELIUS_API_KEY` 时程序按官方格式生成 `https://mainnet.helius-rpc.com/?api-key=`，不必手填 RPC URL。`SOLANA_RPC_URL` 仅在你想覆盖时填写。

Moralis 多把 Key 可在 GUI「系统设置」一行一把增删，并轮换。`.env` 已加入 `.gitignore`，禁止提交真实 Key。

## 6. 启动

```bat
python main.py
```


窗口约 1500×920，最小 1200×760。

打包成独立程序（不装 Python 也能跑）：

```bat
scripts\build_exe.bat
```

产物在 `dist\GMGNWallet\`。把整个文件夹一起拷贝。exe 旁边的目录才是给用户改的：

| 路径 | 用途 |
|---|---|
| `.env` | 现有配置作为默认（API Key）。已有文件不会被覆盖 |
| `input\地址.txt` | 钱包地址，一行一个 |
| `output\` | Excel 导出 |
| `logs\` | 运行日志 |

不要只拷贝 exe，`_internal` 必须同级。

## 7. GUI 功能

| 页面 | 说明 |
|---|---|
| 钱包分析 | 多行地址；1 个=单钱包，>1=批量。同一 JobEngine |
| 任务进度 | 同一套任务的另一入口，按 job_id 路由，不串台 |
| 历史记录 | Job + 单钱包报告，打开 Excel/JSON |
| 数据导出 | 打开 output 目录 |
| 系统设置 | 多 API Key、目标利用率 60/80/90%、Workers、测试连接 |
| 关于 | 版本与数据原则 |

主线程不发 HTTP。点「停止」设置 Event，限流等待可立即退出。不同 Token 可并行，同一 Token 的 cursor 链保持串行。

## 8. Excel 字段说明

至少 5 个 Sheet：代币分析、交易明细、异常与补全、采集范围、原始概要。

详见 `docs/data-fields.md`。列冻结、筛选、自动列宽、盈亏红绿字体。地址可缩写展示，完整值在单元格或 JSON 中。

## 9. 数据来源

见 `docs/api-mapping.md`。官方字段以 [GMGN OpenAPI](https://github.com/GMGNAI/gmgn-skills) 为准。

## 10. 特殊值含义

| 文案 | 含义 |
|---|---|
| 不适用（转入获得） | 没有 Buy，首次获得是转入 |
| 非 Launchpad | 平台链路上没有任何 launchpad/pool exchange |
| 无法验证历史成本 | Sell 匹配不到完整 Buy 成本 |
| GMGN 未提供 | 接口该字段为 null |
| 接口暂不可用 | 请求失败 |
| $xx（估） | 用当前供给估算入场市值 |
| 接口不支持该周期 | 90d/自定义没有官方 profits 周期 |

## 11. Free API 限流

全局加权令牌桶，默认服务端 `rate=5, capacity=5`，目标利用率 80% → 有效约 4 weighted units/s。`wallet_activity/stats/profits` weight=3，`token_info/pool` weight=1。有限并发（默认 4 workers）只重叠网络等待，不叠加额度。429 触发全局冷却，禁止切备用 Key 绕过。状态栏显示实际使用率，不是写死的 80%。

## 12. 常见错误

- **401**：API Key 无效，或 timestamp 偏差超过约 ±5 秒。
- **403**：权限 / IP / 本机走了 IPv6。请改用 IPv4。
- **地址红色**：不是 Solana Base58（32–44）。
- Token 失败只标记该 Token，其它继续。

## 13. 429 说明

读取 `X-RateLimit-Reset` 或 JSON `reset_at`，等到该时刻再重试，同一请求最多 2 次。冷却期间继续打点会把 ban 每次延长约 5 秒，最长约 5 分钟。日志会出现 `RATE_LIMIT 等待 GMGN API 限频恢复`。

## 14. 数据准确性说明

- First Buy 按 **token 完整 cursor 历史** 的最小 timestamp，不是报告窗口内第一笔。
- 「每钱包交易上限」只限制报告窗口流水，不限制 First Buy 追溯。
- GMGN `wallet_profits` 只支持 1d/7d/30d/all。90 天和自定义周期不会伪造官方 PnL。
- 官方利润与本地 FIFO 分列，避免混成一个数。
- Token 创建时间、开盘时间、池子创建时间分列，不用池子时间冒充创建时间。
- 同名币（例如多个 ZEC mint）按合约隔离。

## 测试

```bat
python -m unittest discover -s tests -v
```
