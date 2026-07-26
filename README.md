# self-track · 零摩擦自我跟踪

每天扫一遍本机所有 AI agent（claude / tclaude / tcodex / kimi-code / workbuddy）的会话历史，
按 sessionId 增量落库，LLM 整理出「在做什么 / 关注什么 / 进展如何 / 热点是什么」，
生成一个简约静态页面——一条路上有个小人在走，路碑是每一个有记录的日子。

## 快速开始

```bash
cd /Users/jingquanhu/sideProject/self-track
python3 -m lifelog serve    # 本地应用 :8791，页面直触深度分析
# 或：LIFELOG_LLM_BACKEND=kimi-code python3 -m lifelog run && open web/index.html
```

- 中断、崩溃、一周没开机：重跑同一命令即可，自动补全缺失日期。
- 不用 LLM（纯统计、零外发）：`python3 -m lifelog run`（默认 backend=none）。
- 自动化：`cp scripts/com.selftrack.daily.plist ~/Library/LaunchAgents/ && launchctl load …`（详见 docs/05-usage.md）。

## 结构

```
lifelog/            代码（纯 Python 标准库，零依赖）
  adapters/         各 agent 会话格式适配器
  scan.py           增量扫描（(mtime,size) 水位 + 写入中不推进）
  db.py             SQLite 权威存储（+ dirty_days 持久化恢复）
  digest.py         LLM 整理（L1 会话卡 / L2 日叙事 / 预筛）
  aggregate.py      日统计投影（确定性计算）
  web.py            前端构建（单 HTML，数据内联）
data/               lifelog.sqlite + stats/daily/*.json（gitignore）
web/index.html      产物，双击即开
docs/               调研、schema、review 合并记录、决策记录
scripts/            launchd 模板
```

## 文档索引

- `docs/01-research-and-design.md` — 调研结论与总体设计
- `docs/02-schema.md` — 数据 schema（含修订）
- `docs/03-review-merge.md` — 三轮 tcodex/tclaude 背靠背 review 的合并记录
- `docs/05-usage.md` — 使用说明
- `docs/06-decisions.md` — 决策记录（每个决策点的选项/理由/代价）
