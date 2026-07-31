#!/bin/bash
# self-track 每日入口（launchd 调用）
# 1) 先用一次 kimi CLI 刷新 OAuth token（LLM 整理需要新鲜 token；失败不阻塞统计）
# 2) 跑完整流程：增量扫描 → LLM 整理 → 日统计 → 前端
cd /Users/jingquanhu/sideProject/self-track || exit 1
echo ok | /Users/jingquanhu/.kimi-code/bin/kimi -p "ok" >/dev/null 2>&1
export LIFELOG_LLM_BACKEND=kimi-code
# python3 不写死单一路径：本机无 /opt/homebrew（曾因此每日任务静默失败），
# 按候选顺序取第一个可用的
for py in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  [ -x "$py" ] && break
done
[ -x "$py" ] || { echo "找不到可用的 python3" >&2; exit 127; }
exec "$py" -m lifelog run
