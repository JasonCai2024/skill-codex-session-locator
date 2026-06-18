# Codex 会话存储结构参考

本文档汇总 Codex CLI 的本地会话存储结构、目录组织规则与文件命名约定。`SKILL.md` 中的工作流以本文档的发现为依据。

## 1. 总览

Codex 会话相关数据全部存放在 `CODEX_HOME` 目录下。`CODEX_HOME` 优先级：

1. 命令行 `--codex-home` 参数
2. `CODEX_HOME` 环境变量
3. 默认 `~/.codex/`（Windows 上为 `C:\Users\<user>\.codex\`）

> **多配置目录情况**：用户可能同时维护多个 `CODEX_HOME`（例如 `~/.codex/` 用 OAuth，`~/.codex/zero-taobao/` 用第三方 API Key）。脚本默认只查 `CODEX_HOME` 主目录；查询其他目录必须显式传入 `--codex-home`。

## 2. 三种存储介质

| 文件 / 目录 | 类型 | 用途 | 关键字段 |
|-------------|------|------|----------|
| `sessions/` | JSONL 文件树 | **完整对话历史**（含工具调用、思考过程） | 每行一个 JSON 对象 |
| `history.jsonl` | 单个 JSONL | **简略历史**，只有用户提问 | `session_id`、`ts`、`text` |
| `state_*.sqlite` | SQLite 数据库 | **会话索引数据库**，`/resume` 标题来源 | `threads.title`、`threads.cwd`、`threads.rollout_path` |

三者的关联键是 `session_id` / `threads.id`：

```
state_*.sqlite.threads.id
        ↓ rollout_path
sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<id>.jsonl
        ↓ session_id 后缀匹配
history.jsonl
```

## 3. 目录与文件名规则

### 3.1 目录分层

`sessions/` 按 **会话启动日期** 分层，跨天会话不会被分割：

```
sessions/
└── <YYYY>/
    └── <MM>/
        └── <DD>/
            └── rollout-<timestamp>-<uuid>.jsonl
```

> 规则：会话无论持续多久，**整段都存在启动当天**的目录下。

### 3.2 文件名格式

```
rollout-YYYY-MM-DDTHH-MM-SS-<uuid>.jsonl
         ↑ 会话启动的本地时间戳(年月日时分秒)
                       ↑ session_id,与 threads.id 完全一致
```

UUID 段通常为 `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`（8-4-4-4-12）。

### 3.3 启动日期示例

| 会话场景 | 存放位置 |
|---------|---------|
| 4月15日启动，4月17日结束 | `sessions/2026/04/15/` |
| 4月16日启动，4月16日结束 | `sessions/2026/04/16/` |
| 4月1日启动，4月30日结束 | `sessions/2026/04/01/` |

## 4. `state_*.sqlite` 结构

详见 `references/sqlite-schema.md`。核心要点：

- 关键表是 `threads`，包含会话元数据与 `/resume` 标题
- `threads.rollout_path` 指向完整会话文件
- `threads.cwd` 记录会话启动时的工作目录
- `threads.updated_at`（Unix 秒）按 `ORDER BY DESC` 即为 `/resume` 列表顺序

## 5. `sessions/*.jsonl` 行类型

每个 JSONL 文件由若干行组成，每行是一个独立 JSON 对象。常见类型：

| `type` | 说明 | 是否参与 turn 计数 |
|--------|------|--------------------|
| `session_meta` | 第 1 行的会话元数据 | 否 |
| `turn_context` | 每轮的上下文快照（cwd、当前日期） | 否 |
| `response_item`（payload.type=`message`） | 用户或助手消息 | 是 |
| `response_item`（payload.type=`function_call`） | 工具调用 | 单独计数 |
| `response_item`（payload.type=`function_call_output`） | 工具返回结果 | 单独计数 |
| `response_item`（payload.type=`reasoning`） | 思考过程（**加密**，无法解读） | 跳过 |

### 5.1 `session_meta` 示例

```json
{
  "timestamp": "2026-04-16T01:45:22.504Z",
  "type": "session_meta",
  "payload": {
    "id": "019d93f6-dd3f-7b80-a6bf-cc000b740e16",
    "cwd": "E:\\vnpy-master",
    "originator": "codex_cli_rs",
    "source": "cli"
  }
}
```

字段：

| 字段 | 含义 |
|------|------|
| `cwd` | **会话启动时的工作目录**（关键查询字段） |
| `timestamp` | UTC ISO 8601 时间戳 |
| `id` | 会话 UUID，与 `threads.id` 一致 |
| `originator` | 通常 `codex_cli_rs` |
| `source` | `cli` / `web` / ... |

### 5.2 message payload

```json
{
  "type": "response_item",
  "timestamp": "2026-04-16T01:50:00Z",
  "payload": {
    "type": "message",
    "role": "user",
    "content": [
      {"type": "text", "text": "帮我看看这个项目结构"}
    ]
  }
}
```

`content` 可能是字符串，也可能是结构化数组。`read_rollout.py` 兼容两种形态。

### 5.3 function_call payload

```json
{
  "type": "response_item",
  "payload": {
    "type": "function_call",
    "name": "shell_command",
    "arguments": "{\"command\":\"Get-ChildItem\",\"workdir\":\"E:\\\\vnpy-master\"}"
  }
}
```

`arguments` 是 JSON 字符串，需要二次解析。

## 6. `history.jsonl` 格式

```json
{"session_id":"019d14be-e6d2-7c90-8b53-8d9f4882966d","ts":1774169764,"text":"为我研究下这个项目..."}
```

字段：

| 字段 | 含义 |
|------|------|
| `session_id` | 会话 UUID，与 `threads.id` 和 rollout 文件名后缀一致 |
| `ts` | Unix 时间戳（秒） |
| `text` | 首条用户消息（不是 `/resume` 标题） |

> ⚠️ `history.jsonl` **不**是 `/resume` 标题来源；标题来自 `state_*.sqlite.threads.title`。

## 7. `/resume` 工作流（推测）

1. 读 `state_*.sqlite.threads`
2. 按 `updated_at DESC` 排序
3. 显示 `title` 列
4. 用户选择后，用 `rollout_path` 加载完整 JSONL

## 8. 定位会话的查询路径

要按「cwd + 时间」定位会话，正确顺序是：

```
state_*.sqlite.threads
    ↓ WHERE cwd LIKE '%<keyword>%' AND updated_at BETWEEN <start> AND <end>
    ↓ ORDER BY updated_at DESC
候选列表(每条含 rollout_path)
    ↓ 读取 rollout_path 指向的 JSONL
完整对话内容
```

## 9. 跨平台与编码注意

- Windows 上 `cwd` 字段通常带 `\\?\` 扩展长度路径前缀，例如 `"\\?\E:\BaiduSyncdisk\WorkSpace"`，而非简单的 `"E:\\..."`
- `cwd` 反斜杠转义在 JSON 中是双重 `\\`，即 `"\\?\E:\\..."`
- 文件读取务必 UTF-8（PowerShell 5.1 默认 ANSI 会损坏中文）
- 时间戳全部按 UTC 处理；用户输入按其本地时区解释

### 9.1 路径前缀对匹配的影响

`\\?\` 前缀并不影响 `LIKE %<keyword>%` 子串匹配，因为用户的输入本身通常不含 `\\?\`。如果用户输入的是带前缀的精确路径，反而比不带的更具体：

| 用户输入 | 匹配数据库的 `\\?\E:\BaiduSyncdisk\WorkSpace` |
|---------|------------------------------------------------|
| `BaiduSyncdisk` | ✓ |
| `E:\BaiduSyncdisk\WorkSpace` | ✓ |
| `\\?\E:\BaiduSyncdisk\WorkSpace` | ✓ |
| `WorkSpace` | ✓（但可能匹配多个，如 `E:\WorkSpace` vs `E:\BaiduSyncdisk\WorkSpace`） |

建议默认就用项目名（如 `vnpy`、`BaiduSyncdisk`）做关键字，避免路径变体带来的歧义。

## 10. 参考来源

- 用户文档 `E:\BaiduSyncdisk\WorkSpace\ForAgent\工具专题\Codex\Codex对话历史管理指南.md`
- 用户配置 `E:\BaiduSyncdisk\WorkSpace\ForAgent\SKILLS-编程开发\Codex配置切换指南.md`