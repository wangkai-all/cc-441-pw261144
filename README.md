# logcollector — 韧性日志采集器

把多行异常堆栈、嵌套/格式化 JSON、截断行和时区混用的日志拼接成事件，
保留原始行号与字节偏移；中断时保存未闭合事件，恢复后继续拼接；
解析失败进入可回放的死信队列，一条坏日志不会丢掉后续事件。

纯 Python 3.11 标准库实现，无第三方依赖。

## 快速开始

```bash
# 生成分阶段演示日志（跨批次 JSON、坏记录、20KB 截断行、轮转、未闭合堆栈）
python3 scripts/make_demo_logs.py --dir /tmp/lc-demo/logs --stage 1

# 启动采集器 + 控制台（默认每秒轮询，状态目录可放任意位置）
python3 -m logcollector --state-dir /tmp/lc-demo/state --log /tmp/lc-demo/logs \
    serve --port 8410

# 继续写入后续阶段，观察跨批次拼接与轮转
python3 scripts/make_demo_logs.py --dir /tmp/lc-demo/logs --stage 2
python3 scripts/make_demo_logs.py --dir /tmp/lc-demo/logs --stage 3   # 轮转 app.log
```

打开 http://127.0.0.1:8410 查看控制台：文件偏移、事件时间线、解析错误、重放差异。

```bash
# 命令行一次性操作
python3 -m logcollector --state-dir S --log DIR poll     # 采集一轮
python3 -m logcollector --state-dir S status             # 偏移/未闭合事件
python3 -m logcollector --state-dir S flush              # 强制闭合未闭合事件
python3 -m logcollector --state-dir S replay --no-strict --apply   # 回放死信

# 验证（21 个用例）
python3 -m unittest discover -s tests -v
```

## 拼接规则（assembler）

| 输入 | 行为 |
|---|---|
| 带时间戳前缀的行 | 开启新事件；前一个未闭合事件随之完成 |
| 堆栈行（`Traceback`、`at com.x(...)`、`Caused by:`、`File "...", line N` 等） | 并入当前事件，`has_stack=true` |
| `{` 起始，或 `[` 后接 JSON 值起始符 | 进入 JSON 累积，直到顶层括号平衡（忽略字符串内的括号）后解析 |
| JSON 未平衡时遇到新的时间戳行 | 已累积部分隔离为 `json-unclosed` 死信，新行正常开事件——坏数据不吞噬后续事件 |
| 无时间戳又无归属的行 | 成为 `unstructured` 单行事件，绝不静默丢弃 |
| 超过 `max_line_bytes` 的物理行 | 截断保留头部，`truncated=true` + `orig_len` 记录原始长度 |
| 文件末尾无换行的残行 | 留在读取缓冲，下一批数据到达后拼接（跨批次） |
| 事件行数超 `max_event_lines` | 强制完成并标记 `overflow=true`，防止失控堆栈撑爆内存 |

时间戳支持 ISO-8601（`Z` / `±08:00` / `±0800`）、logback 逗号毫秒、
syslog（无年份，按当前年推断）、Apache 格式；全部归一化为 UTC 毫秒，
无时区的标记 `tz_assumed=true`（默认时区可用 `--tz-offset` 配置）。

## 中断恢复与幂等

- 每轮 poll 后原子写入 `checkpoint.json`（临时文件 + `os.replace`）：
  每个文件的 inode、代（generation）、读取偏移、行号、残行缓冲，
  以及**组装器的未闭合事件**。进程被杀后重启，事件跨重启继续拼接。
- 事件 ID = `sha1(来源|代|起始偏移|起始行号|首行前64字节)`，内容寻址：
  重复读取、重跑采集器、回放应用，全部幂等。
- 文件轮转：inode 变化或文件缩小（copytruncate）→ 旧代的未闭合事件
  以 `incomplete=true` 冲刷保存（不丢失），新代从偏移 0、行号 1 重新开始。

## 死信与回放

解析失败（`json-parse` / `json-unclosed` / `json-overflow`）连同原始行
（行号、偏移、原文）存入 `errors.jsonl`。回放用**当前**解析器配置重跑
这些原始行——修复解析器或放宽配置（`json_strict=false` 可修复尾随逗号）
后，死信可以恢复成事件；恢复的事件 ID 与"当初本应生成"的相同，应用
回放结果天然幂等。回放差异（恢复/仍失败、逐条对比）在控制台展示。

## HTTP API

| 路由 | 说明 |
|---|---|
| `GET /api/status` | 文件偏移、代、未闭合事件、计数 |
| `GET /api/events?source=&level=&limit=` | 事件时间线（UTC 排序） |
| `GET /api/errors` | 死信队列（含原始行） |
| `POST /api/poll` / `POST /api/flush` | 立即采集 / 闭合未闭合事件 |
| `POST /api/replay` | `{"apply": bool, "overrides": {"json_strict": false, ...}}` |
| `GET /api/replay` | 最近一次回放差异 |

## 控制台设计说明

- 时间线散点：x = UTC 时间，y = 来源文件泳道，颜色 = 级别
  （INFO 蓝 = 类别色板 1 号位；WARN/ERROR = 固定状态色，配 ✕/⚠ 图标，
  不只依赖颜色；无级别 = 中性灰）。r=4 圆点 + 2px 表面色圆环，
  24px 最近点悬停命中 + 十字线，键盘 focus 同样显示提示。
- 每个图表都有表格视图（可切换）；筛选行在图表上方，作用于全部内容；
  刷新时旧内容降透明度保持布局不跳动。
- 明暗双主题（媒体查询 + 手动切换），色板经 `validate_palette.js` 验证：
  类别/状态色 CVD ΔE ≥ 8、正常视力 ΔE ≥ 15 均通过；warning 黄在亮面
  对比度 1.79 属状态色设计值，以图标+文字补偿（状态色固定规范）。

## 目录结构

```
logcollector/
  timestamps.py   混合时区解析 → UTC 归一化
  assembler.py    事件拼接状态机（可序列化，跨重启续拼）
  collector.py    文件读取、偏移、轮转检测、原子检查点
  store.py        事件/死信 JSONL 存储（按 ID 幂等）
  replay.py       死信回放与差异
  server.py       HTTP API + 静态页
  web/index.html  控制台（无依赖单页）
tests/test_logcollector.py   21 个验证用例
scripts/make_demo_logs.py    分阶段演示日志生成器
```
