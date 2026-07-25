#!/bin/bash
# self-track 每日入口（launchd 调用）
# 1) 先用一次 kimi CLI 刷新 OAuth token（LLM 整理需要新鲜 token；失败不阻塞统计）
# 2) 跑完整流程：增量扫描 → LLM 整理 → 日统计 → 前端
cd /Users/jingquanhu/sideProject/self-track || exit 1
echo ok | /Users/jingquanhu/.kimi-code/bin/kimi -p "ok" >/dev/null 2>&1
export LIFELOG_LLM_BACKEND=kimi-code
exec /opt/homebrew/bin/python3 -m lifelog run
