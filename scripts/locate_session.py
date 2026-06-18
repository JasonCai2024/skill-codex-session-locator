#!/usr/bin/env python3
"""
locate_session.py — 在 Codex 本地索引(state_*.sqlite)中按工作目录 + 时间窗定位会话。

用法:
    python locate_session.py --cwd "E:\\vnpy-master" --time "yesterday afternoon"
    python locate_session.py --codex-home "C:\\Users\\pc\\.codex" --cwd "E:\\vnpy-master" \
        --after 2026-06-17 --before 2026-06-17T23:59:59
    python locate_session.py --cwd "vnpy" --time "last week" --limit 20 --json

输出:
    默认人类可读表格;--json 时输出机器可读 JSON。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# ---------- 常量 ----------

DEFAULT_CODEX_HOME_SUFFIX = ".codex"
STATE_DB_GLOB = "state_*.sqlite"
DEFAULT_LIMIT = 10
MAX_WINDOW_DAYS = 30  # 防止误匹配全表的安全上限

# ---------- 数据结构 ----------


@dataclass
class ThreadRow:
    id: str
    title: str
    cwd: str
    created_at: int
    updated_at: int
    rollout_path: str
    model: Optional[str]
    source: Optional[str]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "created_at": self._fmt(self.created_at),
            "updated_at": self._fmt(self.updated_at),
            "created_at_ts": self.created_at,
            "updated_at_ts": self.updated_at,
            "rollout_path": self.rollout_path,
            "model": self.model,
            "source": self.source,
        }

    @staticmethod
    def _fmt(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- CODEX_HOME 解析 ----------


def resolve_codex_home(explicit: Optional[str]) -> Path:
    """解析 codex_home 路径。优先级: --codex-home 参数 > CODEX_HOME 环境变量 > ~/.codex"""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / DEFAULT_CODEX_HOME_SUFFIX


def find_state_db(codex_home: Path) -> Path:
    """在 codex_home 下找最新版本的 state_*.sqlite;不存在则抛错。"""
    if not codex_home.exists():
        raise FileNotFoundError(f"CODEX_HOME 不存在: {codex_home}")
    candidates = sorted(codex_home.glob(STATE_DB_GLOB))
    if not candidates:
        raise FileNotFoundError(f"在 {codex_home} 下未找到 {STATE_DB_GLOB}")
    # 选择文件名中数字最大的版本(如 state_5.sqlite > state_4.sqlite)
    def version_key(p: Path) -> int:
        m = re.search(r"state_(\d+)\.sqlite", p.name)
        return int(m.group(1)) if m else -1

    return max(candidates, key=version_key)


# ---------- 时间窗解析 ----------


@dataclass
class TimeWindow:
    start_utc: datetime
    end_utc: datetime
    parsed_from: str  # 原始输入

    def width_days(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds() / 86400


# 中英文时间关键词(注意:英文按"完整词"匹配,避免 afternoon 误匹配 morning)
_CN_TOD = [
    ("凌晨", (0, 6)),
    ("上午", (6, 12)),
    ("中午", (11, 14)),
    ("下午", (12, 18)),
    ("傍晚", (17, 20)),
    ("晚上", (18, 24)),
    ("夜里", (20, 24)),
    ("深夜", (22, 24)),
]
# 英文关键词必须在 "morning" 前于 "afternoon" 中匹配;因此用 word boundary 优先长词
_EN_TOD = [
    ("early morning", (0, 6)),
    ("late night", (22, 24)),
    ("morning", (6, 12)),
    ("noon", (11, 14)),
    ("afternoon", (12, 18)),
    ("evening", (17, 20)),
    ("night", (18, 24)),
]


def parse_time_hint(hint: str, now: Optional[datetime] = None) -> TimeWindow:
    """
    解析自然语言或 ISO 时间提示为 (start_utc, end_utc) 窗口。
    支持:
        - "today" / "今天"
        - "yesterday" / "昨天"
        - "前天" / "day before yesterday"
        - "N days ago" / "N天前"
        - "last week" / "上周"
        - "this morning" / "上午" / "下午" / "晚上" / "afternoon"
        - "around 3pm" / "around 15:00"
        - "2026-06-17" (整天)
        - "2026-06-17T15:00" (±2 小时)
    """
    if now is None:
        now = datetime.now().astimezone()

    hint_norm = hint.strip().lower()
    if not hint_norm:
        raise ValueError("时间提示为空")

    # 优先匹配 ISO 格式
    iso = _try_parse_iso(hint_norm, now)
    if iso:
        return iso

    # 解析日期部分(中文 / 英文);找不到时回退到 today(允许"around 3pm"等仅含时间)
    day_anchor, consumed = _parse_day_anchor(hint_norm, now)
    if day_anchor is None:
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_anchor, consumed = today, ""

    # 剩余部分找时段关键词
    remainder = hint_norm.replace(consumed, "").strip()
    start_t, end_t = _match_time_of_day(remainder)

    # 是否有具体小时("around 3pm" / "15:30" / "下午3点")
    hour_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", remainder)
    if hour_match:
        h = int(hour_match.group(1))
        m = int(hour_match.group(2) or 0)
        ampm = hour_match.group(3)
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        # 防御:h 越界就退回到时段
        if 0 <= h <= 23:
            # 具体小时 ±2 小时窄窗口
            start_dt = day_anchor.replace(hour=h, minute=m, second=0, microsecond=0)
            end_dt = start_dt + timedelta(hours=4)
            return TimeWindow(start_dt.astimezone(timezone.utc), end_dt.astimezone(timezone.utc), hint)

    # 否则用时段
    end_hour = 23 if end_t == 24 else end_t
    end_minute = 59 if end_t == 24 else 0
    end_second = 59 if end_t == 24 else 0
    start_dt = day_anchor.replace(hour=start_t, minute=0, second=0, microsecond=0)
    end_dt = day_anchor.replace(hour=end_hour, minute=end_minute, second=end_second, microsecond=0)
    return TimeWindow(start_dt.astimezone(timezone.utc), end_dt.astimezone(timezone.utc), hint)


def _try_parse_iso(hint: str, now: datetime) -> Optional[TimeWindow]:
    """尝试 ISO 日期 / 日期时间解析。"""
    # 完整 ISO 日期时间
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(hint, fmt)
            dt = dt.replace(tzinfo=now.tzinfo)
            return TimeWindow(
                (dt - timedelta(hours=2)).astimezone(timezone.utc),
                (dt + timedelta(hours=2)).astimezone(timezone.utc),
                hint,
            )
        except ValueError:
            pass
    # 仅日期
    try:
        d = datetime.strptime(hint, "%Y-%m-%d")
        d = d.replace(tzinfo=now.tzinfo)
        return TimeWindow(
            d.astimezone(timezone.utc),
            (d + timedelta(days=1) - timedelta(seconds=1)).astimezone(timezone.utc),
            hint,
        )
    except ValueError:
        return None


def _parse_day_anchor(hint: str, now: datetime) -> Tuple[Optional[datetime], str]:
    """从 hint 提取日期锚点。返回 (锚点 datetime, 消耗掉的子串)。"""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # "today" / "今天"
    if re.search(r"\btoday\b|今天", hint):
        return today, "today" if "today" in hint else "今天"

    # "yesterday" / "昨天"
    if re.search(r"\byesterday\b|昨天", hint):
        return today - timedelta(days=1), "yesterday" if "yesterday" in hint else "昨天"

    # "day before yesterday" / "前天"
    if re.search(r"day before yesterday|前天", hint):
        consumed = "day before yesterday" if "day before yesterday" in hint else "前天"
        return today - timedelta(days=2), consumed

    # "N days ago" / "N天前"
    m = re.search(r"(\d+)\s*days?\s*ago|(\d+)\s*天前", hint)
    if m:
        n = int(m.group(1) or m.group(2))
        consumed = m.group(0)
        return today - timedelta(days=n), consumed

    # "last week" / "上周" → 7 天前窗口
    if re.search(r"\blast\s+week\b|上周", hint):
        consumed = "last week" if "last week" in hint else "上周"
        return today - timedelta(days=7), consumed

    # ISO 日期
    m = re.search(r"\d{4}-\d{2}-\d{2}", hint)
    if m:
        try:
            d = datetime.strptime(m.group(0), "%Y-%m-%d")
            return d.replace(tzinfo=now.tzinfo), m.group(0)
        except ValueError:
            pass

    return None, ""


def _match_time_of_day(remainder: str) -> Tuple[int, int]:
    """从剩余子串匹配时段,返回 (start_hour, end_hour)。默认 0-24 全天。"""
    # 中文时段优先(可能多字)
    for kw, (s, e) in _CN_TOD:
        if kw in remainder:
            return s, e
    # 英文用 word boundary,避免 "afternoon" 被 "morning" 抢先匹配
    for kw, (s, e) in _EN_TOD:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, remainder):
            return s, e
    return 0, 24


# ---------- SQLite 查询 ----------


def normalize_cwd(cwd: str) -> str:
    """把 cwd 归一化为反斜杠形式,便于 LIKE 匹配。"""
    return cwd.replace("/", "\\").rstrip("\\")


def locate_threads(
    db_path: Path,
    cwd_keyword: str,
    after_utc: datetime,
    before_utc: datetime,
    limit: int = DEFAULT_LIMIT,
) -> List[ThreadRow]:
    """在 state_*.sqlite 的 threads 表里查 cwd LIKE + updated_at BETWEEN 的会话。"""
    after_ts = int(after_utc.timestamp())
    before_ts = int(before_utc.timestamp())

    norm = normalize_cwd(cwd_keyword)
    pattern = f"%{norm}%"

    conn = sqlite3.connect(str(db_path))
    try:
        # 健康检查
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"SQLite 完整性检查失败: {integrity}")

        rows = conn.execute(
            """
            SELECT id, title, cwd, created_at, updated_at, rollout_path, model, source
            FROM threads
            WHERE cwd LIKE ?
              AND updated_at >= ?
              AND updated_at <= ?
              AND rollout_path IS NOT NULL
              AND rollout_path != ''
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (pattern, after_ts, before_ts, limit),
        ).fetchall()
    finally:
        conn.close()

    return [
        ThreadRow(
            id=r[0],
            title=r[1] or "(无标题)",
            cwd=r[2] or "",
            created_at=r[3] or 0,
            updated_at=r[4] or 0,
            rollout_path=r[5] or "",
            model=r[6],
            source=r[7],
        )
        for r in rows
    ]


def list_distinct_cwds(db_path: Path, limit: int = 20) -> List[Tuple[str, int]]:
    """返回数据库中出现过的 cwd 列表(去重,带计数),用于 0 匹配时的诊断。"""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT cwd, COUNT(*) AS n
            FROM threads
            WHERE cwd IS NOT NULL AND cwd != ''
            GROUP BY cwd
            ORDER BY n DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        conn.close()


# ---------- 输出 ----------


def render_table(rows: List[ThreadRow]) -> str:
    if not rows:
        return "(无匹配)"
    lines = []
    lines.append(f"{'#':<3} {'updated_at':<20} {'title':<40} {'cwd':<30} {'id'}")
    lines.append("-" * 120)
    for i, r in enumerate(rows, 1):
        title = (r.title[:37] + "...") if len(r.title) > 40 else r.title
        cwd = (r.cwd[:27] + "...") if len(r.cwd) > 30 else r.cwd
        lines.append(f"{i:<3} {r.to_dict()['updated_at']:<20} {title:<40} {cwd:<30} {r.id}")
    return "\n".join(lines)


# ---------- CLI ----------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="locate_session.py",
        description="按工作目录 + 时间窗定位 Codex 会话",
    )
    p.add_argument("--cwd", required=True, help="会话启动时的工作目录(子串匹配)")
    p.add_argument("--time", help="自然语言时间提示,如 'yesterday afternoon' / '昨天下午'")
    p.add_argument("--after", help="窗口开始(ISO),与 --time 互斥")
    p.add_argument("--before", help="窗口结束(ISO),与 --time 互斥")
    p.add_argument("--codex-home", help="覆盖 CODEX_HOME 环境变量")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"返回候选上限,默认 {DEFAULT_LIMIT}")
    p.add_argument("--json", action="store_true", help="输出 JSON 格式")
    return p


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    # 解析时间窗
    try:
        if args.time:
            window = parse_time_hint(args.time)
        elif args.after or args.before:
            now = datetime.now().astimezone()
            after = (
                datetime.fromisoformat(args.after).replace(tzinfo=now.tzinfo)
                if args.after
                else now - timedelta(days=MAX_WINDOW_DAYS)
            )
            before = (
                datetime.fromisoformat(args.before).replace(tzinfo=now.tzinfo)
                if args.before
                else now
            )
            window = TimeWindow(
                after.astimezone(timezone.utc),
                before.astimezone(timezone.utc),
                f"{args.after or ''} ~ {args.before or ''}",
            )
        else:
            print("错误:必须指定 --time 或 (--after/--before)", file=sys.stderr)
            return 2
    except ValueError as e:
        print(f"错误:{e}", file=sys.stderr)
        return 2

    if window.width_days() > MAX_WINDOW_DAYS:
        print(
            f"错误:时间窗宽度 {window.width_days():.1f} 天超过 {MAX_WINDOW_DAYS} 天上限,请缩小窗口",
            file=sys.stderr,
        )
        return 2

    # 定位 codex_home 与 sqlite
    try:
        codex_home = resolve_codex_home(args.codex_home)
        db = find_state_db(codex_home)
    except FileNotFoundError as e:
        print(f"错误:{e}", file=sys.stderr)
        return 2

    # 查询
    matches = locate_threads(db, args.cwd, window.start_utc, window.end_utc, args.limit)

    if args.json:
        result = {
            "codex_home": str(codex_home),
            "state_db": db.name,
            "query": {
                "cwd_keyword": args.cwd,
                "time_window": {
                    "start_utc": window.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_utc": window.end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "parsed_from": window.parsed_from,
                },
            },
            "matches": [r.to_dict() for r in matches],
            "match_count": len(matches),
        }
        if not matches:
            # 0 匹配时附上 cwd 候选清单,便于诊断
            try:
                result["hint_distinct_cwds"] = [
                    {"cwd": c, "count": n} for c, n in list_distinct_cwds(db)
                ]
            except Exception:
                pass
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"CODEX_HOME: {codex_home}")
        print(f"state DB:   {db.name}")
        print(f"窗口:       {window.start_utc} ~ {window.end_utc} (UTC)")
        print(f"窗口来源:   {window.parsed_from!r}")
        print(f"cwd 匹配:   LIKE %{normalize_cwd(args.cwd)}%")
        print()
        print(f"匹配 {len(matches)} 条:")
        print(render_table(matches))
        if not matches:
            print()
            print("数据库中出现过的 cwd(前 20):")
            for c, n in list_distinct_cwds(db):
                print(f"  {n:>4}  {c}")

    return 0


if __name__ == "__main__":
    sys.exit(main())