---
name: skill-codex-session-locator
description: Locates Codex CLI sessions by cwd + update time, reads rollout JSONL. Use when user mentions a Codex session by project path and timestamp (e.g., 'yesterday's Codex in E:\vnpy-master') and wants to find, summarize, or resume it. Triggers on: 找Codex会话, 定位Codex历史, resume Codex by cwd.
disable-model-invocation: true
user-invocable: true
argument-hint: [cwd-path] [time-hint]
---

# Skill Codex Session Locator

## Goal

根据用户提供的工作目录（cwd）和大致更新时间，定位 Codex CLI 本地会话索引中匹配的会话记录，并按需读取对应的完整 `rollout-*.jsonl` 解析出会话内容，供后续总结、引用或恢复使用。

## Required Inputs

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `cwd` | 路径字符串 | 是 | 会话启动时所在的工作目录，可以是项目根目录或子目录；脚本会做大小写不敏感、子串匹配 |
| `time_hint` | 时间字符串 | 是 | 用户给出的会话更新时间提示，支持自然语言（"昨天下午" / "yesterday afternoon" / "around 3pm"）或 ISO 格式（"2026-06-17" / "2026-06-17T15:00"）|
| `codex_home` | 路径 | 否 | Codex 主目录，覆盖 `CODEX_HOME` 环境变量；默认 `~/.codex/` |
| `limit` | 整数 | 否 | 候选返回数量上限，默认 `10` |

**路径与目录约定**：
- 默认 `CODEX_HOME` = `~/.codex/`（Windows 上为 `C:\Users\<user>\.codex\`）
- 用户若有多个配置目录（如 `~/.codex/` 与 `~/.codex/zero-taobao/`），必须显式传入 `codex_home` 参数
- `cwd` 在数据库中可能带 `\\?\` 扩展长度路径前缀（如 `"\\?\E:\BaiduSyncdisk\WorkSpace"`），脚本用 LIKE 子串匹配兼容；用户输入只需给项目关键字（如 `BaiduSyncdisk`）即可

## Workflow

### Step 1：解析 CODEX_HOME 并定位 SQLite 索引

1. 读取 `CODEX_HOME` 环境变量；若未设置则使用 `Path.home() / ".codex"`
2. 在 `${CODEX_HOME}/` 下查找 `state_*.sqlite` 文件（最新版本优先，例如 `state_5.sqlite`）
3. 若不存在，立即中止并报告 `CODEX_HOME` 路径错误

### Step 2：解析时间提示为窗口

1. 调用 `scripts/locate_session.py --time "<hint>"` 解析为 `(start_utc, end_utc)` 时间窗
2. 解析失败时不要静默回退，必须返回错误提示用户改用 ISO 格式

### Step 3：查询候选会话

1. 打开 `state_*.sqlite` 的 `threads` 表
2. WHERE 条件：`cwd LIKE '%<cwd_normalized>%' AND updated_at BETWEEN start AND end`
3. cwd 路径需做归一化：用户输入既可能是 `"E:\\vnpy-master"` 也可能是 `"E:/vnpy-master"`，脚本内部统一为反斜杠并用 `LIKE %...%` 子串匹配
4. ORDER BY `updated_at DESC`
5. LIMIT `limit`（默认 10）

### Step 4：选择目标会话

1. **1 个候选** → 直接进入 Step 5
2. **多个候选** → 必须把候选列表（含 title、cwd、updated_at、rollout_path）展示给用户，请用户明确选择；不得擅自挑选
3. **0 个候选** → 报告 0 匹配；建议扩大时间窗（如 `--before now`）或检查 cwd 路径是否正确

### Step 5：读取并解析 rollout JSONL

1. 读取候选的 `rollout_path` 指向的 JSONL 文件
2. 调用 `scripts/read_rollout.py --path <rollout_path>` 解析：
   - 提取 `session_meta`（cwd、启动时间、originator、source）
   - 提取每个 turn 的 `user` / `assistant` 消息、工具调用
   - 跳过加密的 `reasoning` 字段（无法解读）
3. 默认返回结构化摘要：会话时长、用户提问数、助手回复数、关键转折点
4. 用户如要全文，逐 turn 渲染 Markdown

### Step 6：交付结果

返回结构化 Markdown，包含：
- 会话标识（id、title、cwd、起止时间）
- `rollout_path` 绝对路径
- 用户/助手消息条数
- 会话内容摘要（或指定 turn 的原文）

## Decision Rules

1. **路径不匹配时**：先用 `LIKE %...%` 子串匹配；若仍 0 结果，反馈用户「找不到该 cwd 的会话，请确认路径正确性」，并列出 `state_*.sqlite` 中实际出现过的 cwd 候选（前 20 个）
2. **时间窗歧义时**：
   - 仅有日期（"2026-06-17"）→ 当天 00:00:00 到 23:59:59（用户本地时区）
   - 仅有「昨天/今天」→ 当天完整窗口
   - 带有「上午/下午/晚上」→ 6:00-12:00 / 12:00-18:00 / 18:00-24:00
   - 带有具体小时（"around 3pm"）→ ±2 小时窄窗口
3. **多个 CODEX_HOME 目录并存时**：必须显式 `--codex-home` 参数；脚本默认只查 `CODEX_HOME` 一个目录
4. **`/resume` 标题与首条消息不一致**：以 `threads.title` 为准（这是 `/resume` 列表实际显示的字段）
5. **跨天会话**：整个会话存放在会话启动当天目录下；脚本不需特殊处理，按 `updated_at` 即可定位
6. **JSONL 文件被截断或损坏**：报告错误位置（行号），不要试图部分恢复；提示用户改用 Codex CLI 自身的 `/resume`

## Output Requirements

调用 `scripts/locate_session.py --json` 时返回 JSON，字段如下：

```json
{
  "codex_home": "C:\\Users\\pc\\.codex",
  "state_db": "state_5.sqlite",
  "query": {
    "cwd_keyword": "E:\\vnpy-master",
    "time_window": {
      "start_utc": "2026-06-17T00:00:00Z",
      "end_utc": "2026-06-17T23:59:59Z"
    }
  },
  "matches": [
    {
      "id": "019d14be-e6d2-7c90-8b53-8d9f4882966d",
      "title": "研究 vnpy 项目结构",
      "cwd": "E:\\vnpy-master",
      "created_at": "2026-06-17T09:45:22Z",
      "updated_at": "2026-06-17T15:32:18Z",
      "rollout_path": "C:\\Users\\pc\\.codex\\sessions\\2026\\06\\17\\rollout-2026-06-17T09-45-22-019d14be-...jsonl",
      "model": "gpt-5",
      "source": "cli"
    }
  ],
  "match_count": 1
}
```

调用 `scripts/read_rollout.py --path <rollout_path> --summary` 时返回结构化摘要：

```json
{
  "session_id": "019d14be-...",
  "session_meta": {
    "cwd": "E:\\vnpy-master",
    "started_at": "2026-06-17T09:45:22Z",
    "originator": "codex_cli_rs",
    "source": "cli"
  },
  "stats": {
    "user_messages": 8,
    "assistant_messages": 12,
    "tool_calls": 23,
    "duration_minutes": 350
  },
  "turns": [
    {"role": "user", "timestamp": "...", "content_preview": "..."},
    {"role": "assistant", "timestamp": "...", "content_preview": "..."}
  ]
}
```

## Validation

执行过程中必须验证：

1. **路径存在性**：`CODEX_HOME/state_*.sqlite` 必须存在，否则返回明确错误
2. **SQLite 完整性**：连接后立刻执行 `PRAGMA integrity_check`；失败则报告损坏并停止
3. **时间窗合法性**：`start_utc <= end_utc` 且窗口宽度不超过 30 天（防止误匹配全表）
4. **JSONL 可解析性**：每行必须是合法 JSON；解析失败时报告具体行号
5. **字段完整性**：`session_meta` 必须包含 `id`、`cwd`、`timestamp`；缺失则标记该会话为「元数据不完整」并跳过

## Fallback

| 失败场景 | 回退方案 |
|---------|---------|
| `state_*.sqlite` 不存在 | 检查 `CODEX_HOME` 路径；提示用户是否设置了非默认目录 |
| 时间窗内 0 匹配 | 列出同 cwd 下最近 5 个会话，让用户选择 |
| cwd 完全无匹配 | 列出数据库中实际出现过的 cwd（前 20 个）供用户比对 |
| JSONL 文件已被截断/损坏 | 报告错误位置；建议改用 `/resume` 或从备份恢复 |
| 时间提示无法解析 | 回退到「最近 7 天 + 该 cwd」作为默认窗口，并在结果中标注 |
| `CODEX_HOME` 同时存在多个子配置目录（如 `~/.codex/` 与 `~/.codex/zero-taobao/`） | 默认只查主目录；如需查其他目录，必须显式 `--codex-home` 参数 |

## Examples

### 触发示例

- `帮我找到昨天在 E:\vnpy-master 启动的那个 Codex 会话`
- `Locate the Codex session I ran around 3pm last Friday in E:\BaiduSyncdisk\WorkSpace`
- `查找 2026-06-17 在 C:\Users\pc\projects\foo 启动的 Codex 会话`
- `我那个 vnpy 会话后来怎么样了？`
- `resume 那个 ~3pm 的 Codex 会话`

### 调用示例

```powershell
# 仅定位（返回 JSON 候选列表）
python scripts/locate_session.py --cwd "E:\vnpy-master" --time "yesterday afternoon"

# 读取并解析指定会话
python scripts/read_rollout.py --path "C:\Users\pc\.codex\sessions\2026\06\17\rollout-2026-06-17T09-45-22-<id>.jsonl" --summary

# 自定义 CODEX_HOME
python scripts/locate_session.py --codex-home "C:\Users\pc\.codex\zero-taobao" --cwd "E:\vnpy-master" --time "2026-06-17"
```

### 端到端示例

用户：「找一下 2026-06-17 在 E:\vnpy-master 跑的那个 Codex 会话，告诉我它最后做了什么」

执行流程：
1. 解析时间窗 → `2026-06-17T00:00:00 ~ 2026-06-17T23:59:59`（用户本地时区 +08:00）
2. 查 `state_5.sqlite` → 匹配到 1 个候选（id=`019d14be-...`，title=`研究 vnpy 项目结构`）
3. 读取对应 rollout JSONL → 提取最后 2 轮对话
4. 返回：「该会话在 2026-06-17 09:45 启动于 E:\vnpy-master，持续约 5 小时 50 分钟。最后一次操作是让 Codex 把 vnpy 的 `cta_strategy` 模块改成异步加载；助手回复：『已完成改动，PR #12 已创建』。」

## Reference

详细资料见 `references/`：

| 文件 | 内容 |
|------|------|
| `references/codex-storage.md` | Codex 三种存储介质完整说明、目录组织规则、文件名格式 |
| `references/sqlite-schema.md` | `state_*.sqlite` 的所有表结构与字段含义 |

判定规则边界与查询性能建议详见 `references/sqlite-schema.md` 末尾章节。