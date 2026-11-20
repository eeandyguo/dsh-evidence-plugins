# -*- coding: utf-8 -*-
"""共享的 evidence 契约：统一 JSON 输出、哈希、收据、工作区定位。

约定：引擎只往 stdout 写**一个** JSON 对象；诊断信息一律走 stderr。
退出码：0 = 跑完（含 verdict fail/unknown），2 = 用法或输入错误。

版本：与 package.json 的 0.1.0 同步手工维护。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ENGINE_VERSION = "0.1.0"

# 各引擎的发现项上限：工具结果进上下文，明细应落盘而不是全量回传。
MAX_FINDINGS = 50


def utc_now() -> str:
    """ISO 8601 UTC，秒级，形如 2026-11-18T13:20:00Z。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_root(start: Path) -> Path | None:
    """从 start 向上找含 tools/tex_change_audit.py 的目录。

    插件以 link 方式装在 <workspace>/plugins/<pkg>，所以上溯几级就是工作区根；
    换机器或移动工作区都不用改配置。找不到返回 None（由调用方报错）。
    """
    current = start.resolve()
    for _ in range(8):
        if (current / "tools" / "tex_change_audit.py").is_file():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def receipt(**extra: object) -> dict:
    """收据：时间戳 + 引擎版本，可再附加调用时的少量事实。"""
    data = {"createdAt": utc_now(), "engineVersion": ENGINE_VERSION}
    data.update(extra)
    return data


def clip(text: str, limit: int = 120) -> str:
    """单行化并截断，供 evidence 字段使用。"""
    flat = text.replace("\r", "").replace("\n", "⏎")
    return flat[:limit] + "…" if len(flat) > limit else flat


def emit(payload: dict) -> int:
    """把唯一的结果对象写到 stdout。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    sys.stdout.flush()
    return 0
