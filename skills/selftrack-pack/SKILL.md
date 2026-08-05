---
name: selftrack-pack
description: "把当前会话打包归档到 ~/Deliverables/（会话详情页 + 原始记录 + 会话产出的文件）。增量语义：重复打包只更新变化。当用户说「打包这个会话」「归档当前会话」「pack 一下」时使用。需要本机 self-track serve 在运行（127.0.0.1:8791）。"
---

# selftrack-pack — 打包当前会话

在**正在进行的**会话里调用本 skill，把这个会话连同它的产物（写过的文档/图片/
视频、manifest 里记录的 commit）打包到 `~/Deliverables/<目录名>/`。

打包是**增量**的：同一会话重复打包会更新同一个目录——`session.html`（详情页）
和 `raw/`（原始记录）每次刷新为最新，产物文件内容没变的跳过，只拷贝新增/变化的。
首次打包目录名默认用会话标题；用户指定了名字就用用户的（增量时忽略，目录已稳定）。

## 用法

用你**当前工作目录**作为 `cwd`，POST 到本机 self-track serve：

```bash
curl -s -X POST http://127.0.0.1:8791/api/pack-current \
  -H 'Content-Type: application/json' \
  -d "{\"cwd\": \"$PWD\"}"
```

用户给了名字就加上 `"name": "用户给的名字"`。

## 返回与汇报

成功（200）：

```json
{"ok": true, "dir": "/Users/.../Deliverables/<目录>", "incremental": false,
 "added": 2, "updated": 0, "unchanged": 0, "skipped": 0, "raw_files": [...],
 "session": {"source": "...", "session_id": "...", "title": "..."}}
```

向用户汇报：打包目录、`session.title`（确认没打错会话）、产物统计
（首次=added 个新文件；增量=新增/更新/未变各多少；skipped>0 说明有产物原件
已不在磁盘，包里保留的是上次的副本）。

## 失败处理

- **curl 连接失败**（serve 没起）：提示用户先运行 `selftrack` 启动本地服务，不要自己起。
- **409 busy**：每日批处理正在跑，稍等一两分钟重试一次；还忙就告诉用户稍后再试。
- **404**：库里没有任何会话（极少见），如实转告。

## 注意

- 只会读本机 127.0.0.1 接口和写 `~/Deliverables/`，不改任何项目文件。
- 打包是**复制**语义，原文件不动。
- 如果用户想逐条挑选产物打包、或补录被移动文件的路径，引导他去
  `http://127.0.0.1:8791/` 的「产物」tab 操作。
