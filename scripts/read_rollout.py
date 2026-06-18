#!/usr/bin/env python3
"""
read_rollout.py — 读取并解析 Codex 会话 rollout JSONL 文件。

用法:
    python read_rollout.py --path <rollout_path>
    python read_rollout.py --path <rollout_path> --summary
    python read_rollout.py --path <rollout_path> --turn 3
    python read_rollout.py --path <rollout_path> --json

功能:
    - 解析 session_meta,提取 id/cwd/timestamp
    - 提取 user/assistant 消息(跳过加密的 reasoning)
    - --summary 时返回会话级统计与每个 turn 的预览
    - --turn N 时返回第 N 个 turn 的完整内容
    - --json 时全部以 JSON 输出
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Turn:
    index: int
    role: str  # user / assistant / tool / system
    timestamp: Optional[str]
    content: str
    tool_name: Optional[str] = None
    tool_args: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def preview(self, max_len: int = 120) -> str:
        s = self.content.replace("\n", " ").strip()
        return s if len(s) <= max_len else s[: max_len - 3] + "..."

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "role": self.role,
            "timestamp": self.timestamp,
            "content_preview": self.preview(),
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
        }


def parse_session_meta(line_obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """提取第一条 session_meta 类型的元数据。"""
    if line_obj.get("type") != "session_meta":
        return None
    payload = line_obj.get("payload", {})
    ts = line_obj.get("timestamp")
    return {
        "id": payload.get("id"),
        "cwd": payload.get("cwd"),
        "originator": payload.get("originator"),
        "source": payload.get("source"),
        "started_at": ts,
    }


def extract_content_from_message(msg: Dict[str, Any]) -> str:
    """从一条 message 的 content 字段提取纯文本(content 可能是字符串或数组)。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "input_text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "output_text":
                    parts.append(item.get("text", ""))
                # 跳过 image / tool_use 等非文本片段
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content) if content else ""


def parse_line_as_turn(line_obj: Dict[str, Any], turn_index: int) -> Optional[Turn]:
    """把一行 JSON 解析为 Turn(如果该行是对话 turn)。"""
    line_type = line_obj.get("type")

    if line_type == "session_meta":
        return None  # 元数据行单独处理

    timestamp = line_obj.get("timestamp")

    if line_type == "response_item":
        payload = line_obj.get("payload", {})
        ptype = payload.get("type")

        if ptype == "message":
            role = payload.get("role", "unknown")
            return Turn(
                index=turn_index,
                role=role,
                timestamp=timestamp,
                content=extract_content_from_message(payload),
                raw=payload,
            )

        if ptype == "function_call":
            name = payload.get("name", "unknown")
            args = payload.get("arguments", "")
            # arguments 可能是 JSON 字符串,尝试解析
            try:
                args_obj = json.loads(args)
                args_display = json.dumps(args_obj, ensure_ascii=False, indent=2)
            except (ValueError, TypeError):
                args_display = args
            return Turn(
                index=turn_index,
                role="tool",
                timestamp=timestamp,
                content=f"[tool_call] {name}({args_display})",
                tool_name=name,
                tool_args=args_display,
                raw=payload,
            )

        if ptype == "function_call_output":
            output = payload.get("output", "")
            return Turn(
                index=turn_index,
                role="tool_output",
                timestamp=timestamp,
                content=str(output),
                raw=payload,
            )

        if ptype == "reasoning":
            # reasoning 通常是加密的,跳过
            return Turn(
                index=turn_index,
                role="reasoning",
                timestamp=timestamp,
                content="[encrypted reasoning - skipped]",
                raw=payload,
            )

    # 兼容旧格式:直接是 {role, content}
    if "role" in line_obj and "content" in line_obj:
        return Turn(
            index=turn_index,
            role=line_obj.get("role", "unknown"),
            timestamp=timestamp,
            content=extract_content_from_message(line_obj),
            raw=line_obj,
        )

    return None


def read_rollout(path: Path) -> Dict[str, Any]:
    """读取 JSONL 文件,返回 {session_meta, turns, stats}。"""
    if not path.exists():
        raise FileNotFoundError(f"rollout 文件不存在: {path}")

    session_meta = None
    turns: List[Turn] = []
    parse_errors: List[Dict[str, Any]] = []

    with path.open("r", encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, 1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError as e:
                parse_errors.append({"line": lineno, "error": str(e), "raw": raw_line[:200]})
                continue

            if session_meta is None:
                sm = parse_session_meta(obj)
                if sm:
                    session_meta = sm
                    continue

            turn = parse_line_as_turn(obj, len(turns) + 1)
            if turn:
                turns.append(turn)

    # 统计
    role_counts: Dict[str, int] = {}
    tool_call_count = 0
    earliest_ts = None
    latest_ts = None

    for t in turns:
        role_counts[t.role] = role_counts.get(t.role, 0) + 1
        if t.role == "tool":
            tool_call_count += 1
        if t.timestamp:
            if earliest_ts is None or t.timestamp < earliest_ts:
                earliest_ts = t.timestamp
            if latest_ts is None or t.timestamp > latest_ts:
                latest_ts = t.timestamp

    duration_minutes = None
    if earliest_ts and latest_ts:
        try:
            e = datetime.fromisoformat(earliest_ts.replace("Z", "+00:00"))
            l = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
            duration_minutes = int((l - e).total_seconds() / 60)
        except (ValueError, AttributeError):
            pass

    stats = {
        "total_lines": sum(1 for _ in path.open("r", encoding="utf-8")),
        "total_turns": len(turns),
        "role_counts": role_counts,
        "tool_call_count": tool_call_count,
        "duration_minutes": duration_minutes,
    }

    return {
        "rollout_path": str(path),
        "session_meta": session_meta,
        "turns": turns,
        "stats": stats,
        "parse_errors": parse_errors,
    }


def render_summary(result: Dict[str, Any]) -> str:
    """渲染人类可读的会话摘要。"""
    meta = result.get("session_meta") or {}
    stats = result.get("stats", {})
    turns = result.get("turns", [])

    lines = []
    lines.append(f"会话 ID:    {meta.get('id', '?')}")
    lines.append(f"工作目录:   {meta.get('cwd', '?')}")
    lines.append(f"启动时间:   {meta.get('started_at', '?')}")
    lines.append(f"来源:       {meta.get('source', '?')}")
    lines.append(f"originator: {meta.get('originator', '?')}")
    lines.append("")
    lines.append("统计:")
    lines.append(f"  total_turns:      {stats.get('total_turns', 0)}")
    lines.append(f"  role_counts:      {stats.get('role_counts', {})}")
    lines.append(f"  tool_call_count:  {stats.get('tool_call_count', 0)}")
    lines.append(f"  duration_minutes: {stats.get('duration_minutes', '?')}")
    lines.append("")

    lines.append(f"Turn 列表(共 {len(turns)} 条):")
    lines.append(f"{'#':<4} {'role':<12} {'timestamp':<28} {'preview'}")
    lines.append("-" * 100)
    for t in turns:
        ts = (t.timestamp or "")[:25]
        prev = t.preview(80)
        lines.append(f"{t.index:<4} {t.role:<12} {ts:<28} {prev}")

    errs = result.get("parse_errors", [])
    if errs:
        lines.append("")
        lines.append(f"解析错误({len(errs)} 条):")
        for e in errs:
            lines.append(f"  line {e['line']}: {e['error']}")

    return "\n".join(lines)


def render_full_turn(turn: Turn) -> str:
    """渲染单个 turn 的完整内容。"""
    lines = []
    lines.append(f"Turn #{turn.index} [{turn.role}] {turn.timestamp or ''}")
    lines.append("=" * 80)
    lines.append(turn.content)
    lines.append("=" * 80)
    if turn.tool_name:
        lines.append(f"tool_name: {turn.tool_name}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="read_rollout.py",
        description="读取并解析 Codex 会话 rollout JSONL 文件",
    )
    p.add_argument("--path", required=True, help="rollout-*.jsonl 的绝对路径")
    p.add_argument("--summary", action="store_true", help="只输出摘要(默认)")
    p.add_argument("--turn", type=int, help="渲染指定 turn 的完整内容")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    args = p.parse_args(argv)

    path = Path(args.path)
    try:
        result = read_rollout(path)
    except FileNotFoundError as e:
        print(f"错误:{e}", file=sys.stderr)
        return 2

    if args.turn is not None:
        # 输出指定 turn 的全文
        idx = args.turn
        turns = result["turns"]
        if idx < 1 or idx > len(turns):
            print(f"错误:turn 索引 {idx} 超出范围(共 {len(turns)} 个 turn)", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(turns[idx - 1].__dict__, ensure_ascii=False, indent=2, default=str))
        else:
            print(render_full_turn(turns[idx - 1]))
        return 0

    # 默认摘要
    if args.json:
        out = {
            "rollout_path": result["rollout_path"],
            "session_meta": result["session_meta"],
            "stats": result["stats"],
            "turns": [t.to_dict() for t in result["turns"]],
            "parse_errors": result["parse_errors"],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render_summary(result))

    return 0


if __name__ == "__main__":
    sys.exit(main())