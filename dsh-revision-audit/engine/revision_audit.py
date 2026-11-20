# -*- coding: utf-8 -*-
"""revision_audit 引擎：旧稿 + 新稿 (+ 申报单) → 统一 evidence JSON（stdout）。

设计约束（批次 1 + 申报单对账）：
  · 只复用 tools/tex_change_audit.py 的 read/hunks/render/cmd_stats/cmd_names，
    **不修改那个文件**，也不 import 它作为包（按路径加载，避免污染 sys.path）。
  · 只读输入；唯一可写的文件是 --report 指定的 Markdown，且先删旧件（幂等）。
  · 对账口径按 research/2026-11-16-改动留痕与归口审.md §6.1/§6.4：
    机器只判「块能不能落到申报条目的锚点上」，不判语义是否相称，也不拿总数对账。
    - 无申报单 → verdict unknown
    - 有申报单且块落不到条目、且申报单里没有无锚点条目 → 未申报 → fail
    - 申报单含无锚点条目（文件级/说明性）→ 落不到的块只能是「覆盖不确定」→ unknown
    - 可锚定条目找不到对应块 → 申报未改 → fail

用法：
    python revision_audit.py --old OLD.tex --new NEW.tex [--declaration 申报单.md]
                             [--report 明细.md] [--expect N] [--todo N] [--ctx 1]
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

# 不写 .pyc：本引擎被反复调用，不能往 engine/ 和共享的 tools/ 里丢 __pycache__。
# 必须在 import common 之前设置，否则 common 已经写好了缓存。
sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402  （同目录模块，须先补 sys.path）
import declaration  # noqa: E402


def load_audit():
    """按路径加载工作区的 tools/tex_change_audit.py。"""
    vendored = Path(__file__).resolve().parent / "vendor" / "tex_change_audit.py"
    if vendored.is_file():
        target = vendored
    else:
        root = common.workspace_root(Path(__file__))
        if root is None:
            raise SystemExit(
                "找不到 tex_change_audit.py：包内 vendor/ 缺失，且不在工作区的 tools/ 下"
            )
        target = root / "tools" / "tex_change_audit.py"
    spec = importlib.util.spec_from_file_location("tex_change_audit", target)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载审计模块：{target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def finding(severity: str, code: str, where: str, detail: str, evidence: str = "") -> dict:
    return {
        "severity": severity,
        "code": code,
        "where": where,
        "detail": detail,
        "evidence": evidence,
    }


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def block_findings(hunks: list[dict], audit) -> list[dict]:
    """逐块报机器能判的客观异常；不判改动性质、不判是否越界。"""
    out: list[dict] = []
    for index, hunk in enumerate(hunks, 1):
        tags = hunk["tags"]
        where = f"第 {index} 块（旧 L{hunk['old_range'][0]}）"
        if "PURE_ADD" in tags and "COMMENT" not in tags:
            added = [line for line in hunk["new"].splitlines() if line.strip()]
            out.append(
                finding(
                    "warn",
                    "PURE_ADD",
                    where,
                    f"纯插入 {len(added)} 行正文，没有对应旧文本",
                    common.clip(added[0] if added else ""),
                )
            )
        elif "PURE_DEL" in tags:
            removed = [line for line in hunk["old"].splitlines() if line.strip()]
            out.append(
                finding(
                    "warn",
                    "PURE_DEL",
                    where,
                    f"纯删除 {len(removed)} 行，没有对应新文本",
                    common.clip(removed[0] if removed else ""),
                )
            )
        if "CMD" in tags:
            before = audit.cmd_names(hunk["old"])
            after = audit.cmd_names(hunk["new"])
            added_cmds = [name for name in after if name not in before]
            gone_cmds = [name for name in before if name not in after]
            if added_cmds or gone_cmds:
                parts = []
                if added_cmds:
                    parts.append("新增 " + ", ".join(added_cmds[:5]))
                if gone_cmds:
                    parts.append("移除 " + ", ".join(gone_cmds[:5]))
                out.append(
                    finding(
                        "warn",
                        "CMD_CHANGE",
                        where,
                        "块内 LaTeX 命令有种类增删：" + "；".join(parts),
                    )
                )
    return out


def analyze_declaration(text: str, path: str, hunks: list[dict]) -> dict:
    """解析申报单并逐块对账。

    @returns {{findings, payload, status, parsed, per_hunk, item_matched}}
      status 为 pass / fail / unknown；无申报单时由调用方处理。
    """
    parsed = declaration.parse(text)
    items = parsed["items"]
    findings: list[dict] = []

    if not items:
        for note in parsed["notes"] or ["申报单里没有可识别的条目"]:
            findings.append(finding("warn", "DECLARATION_UNPARSED", path, note))
        return {
            "findings": findings,
            "status": "unknown",
            "parsed": parsed,
            "per_hunk": {},
            "item_matched": {},
            "payload": {
                "path": path,
                "sha256": "",
                "items": 0,
                "anchored": 0,
                "matched": 0,
                "undeclared": [],
                "undeclaredTotal": 0,
                "uncertain": [],
                "missing": [],
            },
        }

    per_hunk, item_matched = declaration.match(hunks, items)
    has_coarse = any(not item["anchors"] for item in items)
    undeclared: list[dict] = []
    uncertain: list[int] = []
    missing: list[str] = []

    for index, hunk in enumerate(hunks, 1):
        if per_hunk.get(index):
            continue
        where = f"第 {index} 块（旧 L{hunk['old_range'][0]}）"
        preview = common.clip(first_line(hunk["new"] or hunk["old"]))
        if has_coarse:
            uncertain.append(index)
            findings.append(
                finding(
                    "warn",
                    "COVERAGE_UNCERTAIN",
                    where,
                    "该块落不到任何锚点，而申报单里有无锚点条目，无法机器判它是否已申报",
                    preview,
                )
            )
        else:
            undeclared.append({"hunk": index, "where": where, "preview": preview})
            findings.append(
                finding(
                    "error",
                    "UNDECLARED_CHANGE",
                    where,
                    "该块落不到申报单任何一条，属未申报改动",
                    preview,
                )
            )

    for item in items:
        if item["anchors"] and not item_matched.get(item["id"], False):
            missing.append(item["id"])
            place = f"申报条目 {item['id']}"
            if item["where"]:
                place += f"（{item['where']}）"
            findings.append(
                finding(
                    "error",
                    "DECLARED_NOT_CHANGED",
                    place,
                    "申报了改动，但新旧稿里都找不到该锚点对应的块（申报未改）",
                    common.clip(item["raw"]),
                )
            )

    for note in parsed["notes"]:
        findings.append(finding("warn", "DECLARATION_NOTE", path, note))

    if any(f["severity"] == "error" for f in findings):
        status = "fail"
    elif uncertain:
        status = "unknown"
    else:
        status = "pass"

    return {
        "findings": findings,
        "status": status,
        "parsed": parsed,
        "per_hunk": per_hunk,
        "item_matched": item_matched,
        "payload": {
            "path": path,
            "sha256": "",
            "items": len(items),
            "anchored": sum(1 for item in items if item["anchors"]),
            "matched": sum(1 for item in items if item_matched.get(item["id"], False)),
            "undeclared": undeclared[: common.MAX_FINDINGS],
            "undeclaredTotal": len(undeclared),
            "uncertain": uncertain[: common.MAX_FINDINGS],
            "missing": missing[: common.MAX_FINDINGS],
        },
    }


def render_declaration_section(analyzed: dict, hunks: list[dict]) -> str:
    """对账结果追加在审计报告末尾；不改 tex_change_audit 的正文。"""
    parsed = analyzed["parsed"]
    per_hunk = analyzed["per_hunk"]
    item_matched = analyzed["item_matched"]
    payload = analyzed["payload"]
    lines = [
        "",
        "## 六、机器对账（申报单）",
        "",
        f"- 申报单：`{payload['path']}`（{payload['items']} 条，"
        f"{payload['anchored']} 条可锚定，命中 {payload['matched']} 条）",
        f"- 结论：**{analyzed['status']}**"
        f"（未申报 {payload['undeclaredTotal']} 块，覆盖不确定 {len(payload['uncertain'])} 块，"
        f"申报未改 {len(payload['missing'])} 条）",
        "",
        "> 锚点 = 申报条目「原文/改法」列里的反引号片段；去掉全部空白后与该块文本比对。"
        "命中即已申报；落不到任何条目的块才是未申报。语义是否相称不在本表判定。",
        "",
        "| 块 | 位置 | 结论 | 申报条目 |",
        "|---|---|---|---|",
    ]
    for index, hunk in enumerate(hunks, 1):
        ids = per_hunk.get(index, [])
        if ids:
            verdict = "已申报"
            items_text = ", ".join(ids)
        elif index in payload["uncertain"]:
            verdict = "覆盖不确定"
            items_text = "—"
        else:
            verdict = "**未申报**"
            items_text = "—"
        lines.append(f"| {index} | 旧 L{hunk['old_range'][0]} | {verdict} | {items_text} |")

    if parsed["items"]:
        lines.extend([
            "",
            "| 条目 | 位置 | 锚点 | 结论 |",
            "|---|---|---|---|",
        ])
        for item in parsed["items"]:
            if not item["anchors"]:
                conclusion = "无锚点（不能机器归块）"
                anchor_text = "—"
            else:
                anchor_label = item["anchors"][0]["text"].replace("|", "\\|")
                anchor_text = "`" + (anchor_label[:40] + "…" if len(anchor_label) > 40 else anchor_label) + "`"
                conclusion = "命中" if item_matched.get(item["id"]) else "**申报未改**"
            lines.append(f"| {item['id']} | {item['where'] or '—'} | {anchor_text} | {conclusion} |")

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--declaration", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--expect", type=int, default=None)
    parser.add_argument("--todo", type=int, default=None)
    parser.add_argument("--ctx", type=int, default=1)
    args = parser.parse_args()

    old_path, new_path = Path(args.old), Path(args.new)
    for candidate in (old_path, new_path):
        if not candidate.is_file():
            print(f"找不到文件：{candidate}", file=sys.stderr)
            return 2
        if candidate.suffix.lower() not in (".tex", ".ltx"):
            print(f"只支持 LaTeX 源文件（.tex / .ltx），收到：{candidate}", file=sys.stderr)
            return 2
    declaration_path = Path(args.declaration) if args.declaration else None
    if declaration_path is not None and not declaration_path.is_file():
        print(f"找不到申报单：{declaration_path}", file=sys.stderr)
        return 2

    audit = load_audit()
    old_lines = audit.read(old_path)
    new_lines = audit.read(new_path)
    hunks = audit.hunks(old_lines, new_lines, args.ctx)

    findings = block_findings(hunks, audit)
    declaration_payload = {
        "path": "",
        "sha256": "",
        "items": 0,
        "anchored": 0,
        "matched": 0,
        "undeclared": [],
        "undeclaredTotal": 0,
        "uncertain": [],
        "missing": [],
    }
    declaration_section = ""
    analyzed: dict | None = None

    if declaration_path is None:
        if not hunks:
            verdict = "pass"
        else:
            verdict = "unknown"
            findings.append(
                finding(
                    "warn",
                    "NO_DECLARATION",
                    "-",
                    "未提供申报单：本引擎只报客观改动块，不判「已申报/未申报/申报未改」。",
                )
            )
    else:
        analyzed = analyze_declaration(
            declaration_path.read_text(encoding="utf-8", errors="replace"),
            str(declaration_path),
            hunks,
        )
        findings.extend(analyzed["findings"])
        verdict = analyzed["status"]
        declaration_payload = analyzed["payload"]
        declaration_payload["sha256"] = common.sha256_file(declaration_path)
        declaration_section = render_declaration_section(analyzed, hunks)

    if args.expect is not None:
        findings.append(
            finding(
                "warn",
                "DECLARATION_COUNT",
                "-",
                f"申报直改 {args.expect} 处、实际 {len(hunks)} 块；两者口径不同，不可直接比较。",
            )
        )

    tag_counts: dict[str, int] = {}
    for hunk in hunks:
        for tag in hunk["tags"]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    report_path = ""
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        body = audit.render(old_path, new_path, old_lines, new_lines, hunks, args.expect, args.todo)
        target.write_text(body.rstrip("\n") + "\n" + declaration_section, encoding="utf-8")
        report_path = str(target)

    total = len(findings)
    payload = {
        "tool": "revision_audit",
        "target": str(new_path),
        "hash": common.sha256_file(new_path),
        "verdict": verdict,
        "findings": findings[: common.MAX_FINDINGS],
        "findingsTotal": total,
        "declaration": declaration_payload,
        "stats": {
            "oldPath": str(old_path),
            "newPath": str(new_path),
            "oldHash": common.sha256_file(old_path),
            "newHash": common.sha256_file(new_path),
            "oldLines": len(old_lines),
            "newLines": len(new_lines),
            "hunks": len(hunks),
            "tagCounts": tag_counts,
            "commandsOld": audit.cmd_stats(old_lines),
            "commandsNew": audit.cmd_stats(new_lines),
            "expect": args.expect if args.expect is not None else -1,
            "todo": args.todo if args.todo is not None else -1,
        },
        "reportPath": report_path,
        "receipt": common.receipt(),
    }
    return common.emit(payload)


if __name__ == "__main__":
    raise SystemExit(main())
