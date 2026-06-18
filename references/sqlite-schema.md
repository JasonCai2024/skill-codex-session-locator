# `state_*.sqlite` Schema 参考

Codex 会话索引数据库的核心表与字段说明。本文件是 `locate_session.py` 的查询依据。

## 1. 文件定位

```
${CODEX_HOME}/state_*.sqlite
```

最新版本文件名中数字最大，例如 `state_5.sqlite`。`locate_session.py` 通过 `max(glob, key=version)` 选取。

## 2. 表清单

| 表名 | 用途 |
|------|------|
| `threads` | **核心表**：所有会话的元数据 |
| `thread_dynamic_tools` | 线程动态工具配置 |
| `stage1_outputs` | Agent 记忆摘要 |
| `jobs` | 后台任务队列 |
| `agent_jobs` | Agent 任务配置 |
| `agent_job_items` | Agent 任务项 |
| `thread_spawn_edges` | 线程父子关系（树状结构） |
| `remote_control_enrollments` | 远程控制配置 |

本技能只读 `threads` 表。

## 3. `threads` 表结构

```sql
CREATE TABLE threads (
  id TEXT PRIMARY KEY,              -- 会话 UUID
  rollout_path TEXT,                -- 完整会话文件绝对路径
  created_at INTEGER,               -- 创建时间(Unix 秒)
  updated_at INTEGER,               -- 更新时间(Unix 秒)
  source TEXT,                      -- cli / web / ...
  model_provider TEXT,              -- 模型提供商
  cwd TEXT,                         -- 会话启动时的工作目录
  title TEXT,                       -- /resume 显示的标题
  first_user_message TEXT,          -- 首条用户消息
  sandbox_policy TEXT,              -- 沙箱策略
  approval_mode TEXT,               -- 审批模式
  tokens_used INTEGER,              -- token 使用量
  has_user_event INTEGER,           -- 是否有用户事件(0/1)
  archived INTEGER,                 -- 是否归档(0/1)
  archived_at INTEGER,              -- 归档时间(Unix 秒)
  cli_version TEXT,                 -- CLI 版本
  model TEXT                        -- 模型名称
)
```

### 3.1 关键字段

| 字段 | 类型 | 关键性 | 说明 |
|------|------|--------|------|
| `id` | TEXT PK | ★★★ | 与 `history.jsonl.session_id`、`sessions/*.jsonl` 文件名后缀完全一致 |
| `rollout_path` | TEXT | ★★★ | 完整 JSONL 文件绝对路径，调用 `read_rollout.py --path` |
| `cwd` | TEXT | ★★★ | **会话启动时**的工作目录（不是当前目录） |
| `updated_at` | INTEGER | ★★★ | `/resume` 列表的排序键 |
| `created_at` | INTEGER | ★★ | 会话启动时间 |
| `title` | TEXT | ★★ | `/resume` 标题；创建后可以独立修改 |
| `first_user_message` | TEXT | ★ | 首条用户消息，title 创建时的来源 |
| `model` / `model_provider` | TEXT | ★ | 过滤用 |
| `archived` | INTEGER | ★ | 1=已归档；当前查询默认不过滤 |

### 3.2 `title` vs `first_user_message`

- 会话刚创建：`title` = `first_user_message`
- 之后：两者独立，可单独修改
- `/resume` 列表只读 `title`

> 含义：用户改 `title` 后，旧的 `first_user_message` 不会跟着变；做全文搜索时记得同时查两个字段。

## 4. 索引

Codex 默认在以下字段建索引：

- `id`（主键）
- `updated_at`（用于 `/resume` 排序）

本技能的 `WHERE cwd LIKE ? AND updated_at BETWEEN ? AND ?` 在数据量不大（< 10 万条）时性能足够。如果未来数据膨胀，可以在 `cwd` 上加额外索引：

```sql
CREATE INDEX IF NOT EXISTS idx_threads_cwd ON threads(cwd);
```

## 5. 健康检查

`locate_session.py` 在连接后立刻执行 `PRAGMA integrity_check`，返回 `ok` 才继续。损坏的 SQLite 必须从备份恢复或重建。

## 6. 时间字段处理

- `created_at` / `updated_at` / `archived_at` 都是 **Unix 秒**（INTEGER）
- 转换示例：
  ```python
  from datetime import datetime, timezone
  datetime.fromtimestamp(updated_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
  ```
- `session_meta` JSONL 行的 `timestamp` 字段是 **ISO 8601 UTC 字符串**，与 SQLite INTEGER 不互通；做 join 时务必转换

## 7. 常用查询模板

### 7.1 按 cwd + 时间窗定位

```sql
SELECT id, title, cwd, created_at, updated_at, rollout_path, model, source
FROM threads
WHERE cwd LIKE '%E:\\vnpy-master%'
  AND updated_at >= ?
  AND updated_at <= ?
  AND rollout_path IS NOT NULL
  AND rollout_path != ''
ORDER BY updated_at DESC
LIMIT 10;
```

### 7.2 列出全部 distinct cwd（用于 0 匹配时的诊断）

```sql
SELECT cwd, COUNT(*) AS n
FROM threads
WHERE cwd IS NOT NULL AND cwd != ''
GROUP BY cwd
ORDER BY n DESC
LIMIT 20;
```

### 7.3 按模型过滤

```sql
SELECT id, title, cwd, updated_at
FROM threads
WHERE model = 'gpt-5'
ORDER BY updated_at DESC
LIMIT 20;
```

### 7.4 修改 title

```sql
UPDATE threads
SET title = '新标题'
WHERE id = '019d14be-e6d2-7c90-8b53-8d9f4882966d';
```

> ⚠️ 修改前建议先 SELECT 确认 id 与预期一致；批量修改建议先在事务中备份。

### 7.5 归档会话

```sql
UPDATE threads
SET archived = 1, archived_at = strftime('%s', 'now')
WHERE id IN (?, ?, ?);
```

## 8. 与 `history.jsonl` 的对齐

`state_*.sqlite.threads.id` 应与 `history.jsonl` 的 `session_id` 完全一致。可做交叉校验：

```python
import sqlite3, json
conn = sqlite3.connect(r'C:\Users\pc\.codex\state_5.sqlite')
sqlite_ids = {r[0] for r in conn.execute("SELECT id FROM threads").fetchall()}
with open(r'C:\Users\pc\.codex\history.jsonl', encoding='utf-8') as f:
    history_ids = {json.loads(line)['session_id'] for line in f if line.strip()}
missing_in_sqlite = history_ids - sqlite_ids
missing_in_history = sqlite_ids - history_ids
print(f"history 中存在但 sqlite 缺失: {len(missing_in_sqlite)}")
print(f"sqlite 中存在但 history 缺失: {len(missing_in_history)}")
```

正常情况下两个集合应该完全一致或非常接近；若差异大，可能是 Codex 异常退出或迁移未完成。

## 9. 参考来源

- 用户文档 `E:\BaiduSyncdisk\WorkSpace\ForAgent\工具专题\Codex\Codex对话历史管理指南.md`
- `references/codex-storage.md`（本仓库）