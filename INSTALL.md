# 安装与手动调用说明

本文件从主仓库 README 中剥离，专门说明本地安装与手动调用方式。

主仓库 README 关注「技能做什么、为何这样做」；本文件关注「怎样放到本地调试与使用」。

---

## 一、安装位置

### Claude Code

#### 项目级（仅当前项目可见）

```text
.claude/skills/skill-codex-session-locator/
```

把整个仓库目录复制到此路径即可。

#### 全局（所有项目可见）

```text
~/.claude/skills/skill-codex-session-locator/
```

### OpenCode

```text
.opencode/skills/skill-codex-session-locator/
# 或全局
~/.config/opencode/skills/skill-codex-session-locator/
```

OpenCode 同时也识别 `.claude/skills/` 与 `~/.claude/skills/`，所以同一份目录可以同时服务 Claude Code 与 OpenCode。

---

## 二、验证发现

### Claude Code

1. 启动或重启 Claude Code
2. 输入 `/`，应该能看到 `skill-codex-session-locator`
3. 如果没出现，检查：
   - 目录是否完整（包含 `SKILL.md`、`scripts/`、`references/`）
   - `SKILL.md` 顶部 frontmatter 是否合法

### OpenCode

1. 启动 OpenCode
2. 输入 `/` 同理
3. 若未发现，重启或新开会话

---

## 三、手动调用

### Claude Code 中

```text
/skill-codex-session-locator 帮我找昨天在 E:\vnpy-master 启动的 Codex 会话
```

带参数时：

```text
/skill-codex-session-locator E:\vnpy-master yesterday afternoon
```

### 兼容命令（可选）

如希望更稳定地在 `/` 补全中看到，可额外安装兼容命令：

```text
~/.claude/commands/skill-codex-session-locator.md
```

兼容命令模板：

```markdown
---
description: 按工作目录 + 时间窗定位 Codex 会话并读取内容
argument-hint: [cwd] [time-hint]
---

按用户输入的工作目录和时间提示定位 Codex 会话。

Prefer using:

`~/.claude/skills/skill-codex-session-locator/scripts/locate_session.py`
`~/.claude/skills/skill-codex-session-locator/scripts/read_rollout.py`

If no input is provided in `$ARGUMENTS`, ask for cwd and time-hint then stop.
```

---

## 四、直接调用脚本（无需 Claude Code）

```powershell
# 1. 定位候选会话(JSON 输出便于二次处理)
python scripts/locate_session.py --cwd "E:\vnpy-master" --time "yesterday afternoon" --json

# 2. 拿到候选的 rollout_path 后,读取会话
python scripts/read_rollout.py --path "<rollout_path>" --summary

# 3. 看某一条 turn 的完整内容
python scripts/read_rollout.py --path "<rollout_path>" --turn 3
```

---

## 五、Python 环境要求

- Python 3.8+
- 仅使用标准库，**无需 pip install**

如系统默认 `python` 不可用，可换：

```powershell
py -3 scripts/locate_session.py --cwd "E:\vnpy-master" --time "today"
```

---

## 六、常见问题

### Q1：`/resume` 列表里看到的标题和 `state_*.sqlite.threads.title` 不一致？

`/resume` 标题来自 `threads.title`，与 `history.jsonl` 和 `sessions/*.jsonl` 的首条用户消息是**独立**的两条信息。修改 `title` 后，`first_user_message` 不会同步更新。

### Q2：时间窗 30 天限制挡住了我的查询怎么办？

这是安全上限，防止扫描全表。请缩窄时间窗，例如：

```powershell
python scripts/locate_session.py --cwd "E:\vnpy-master" --after 2026-05-01 --before 2026-05-31
```

### Q3：找不到会话，但确实跑过？

可能原因：

1. `CODEX_HOME` 不是默认路径 → 用 `--codex-home` 显式传入
2. 路径记错了 → 用 `--json` 输出，0 匹配时脚本会自动列出数据库中实际出现过的 cwd 前 20 个
3. JSONL 文件被删除 → 检查 `sessions/<YYYY>/<MM>/<DD>/` 下是否还有 rollout 文件
4. Codex 升级导致 schema 变更 → 检查 `state_*.sqlite` 版本号

### Q4：如何修改某个会话的标题？

```python
import sqlite3
conn = sqlite3.connect(r'C:\Users\pc\.codex\state_5.sqlite')
conn.execute(
    "UPDATE threads SET title = ? WHERE id = ?",
    ("新标题", "019d14be-e6d2-7c90-8b53-8d9f4882966d"),
)
conn.commit()
```

详见 `references/sqlite-schema.md` 第 7.4 节。

---

## 七、本文件维护说明

本文件随仓库根目录发布；如安装位置或调用方式有调整，请同步修改本文件与 `README.md`。