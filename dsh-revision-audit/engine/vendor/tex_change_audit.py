# -*- coding: utf-8 -*-
"""审计一对 LaTeX 源码：把 old → new 的改动聚成可判定的条目。

用途：改稿后核对"改了哪几处、是否有没申报的改动、改动强度是否超过申报的性质"。
只读两个 .tex，不改任何文件；报告另存。

用法:
    & "<python>" tools/tex_change_audit.py OLD.tex NEW.tex -o 报告.md
    & "<python>" tools/tex_change_audit.py OLD.tex NEW.tex -o 报告.md --expect 11 --todo 3

--expect  申报的直改处数（reviews/ 里写的"11 处直改"）
--todo    申报的 % TODO 处数

设计说明：
  · 行级 difflib 会把"改一个词"报成整行删+整行增，天然高估改动量；
    故每个 hunk 内部再做字符级对比，用改动占比判定强度。
  · 不引入 LaTeX 解析器，只做文本与命令计数的静态对比。
  · 分类是提示，不是判据——最终由人看"旧→新"片段决定。
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

# ---------- 基础 ----------

def read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def squash(s: str) -> str:
    """去掉全部空白，用于判"是否只是排版差异"。"""
    return re.sub(r"\s+", "", s)


def char_ops(a: str, b: str) -> list[tuple[str, str, str]]:
    """字符级 opcodes，去掉 equal。大块直接放弃（防 O(n^2) 卡死）。"""
    if len(a) + len(b) > 40000:
        return [("~", a[:200], b[:200])]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    # get_opcodes() 给的是下标，必须就地切片成文本，否则调用方拿到的是 int
    return [(tag, a[i1:i2], b[j1:j2])
            for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def ratio(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if len(a) + len(b) > 60000:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def clip(s: str, n: int = 90) -> str:
    s = s.replace("\r", "").replace("\n", "⏎")
    return (s[:n] + "…") if len(s) > n else s


def md(s: str, n: int = 90) -> str:
    """塞进 Markdown 表格的代码片段：去竖线、去反引号。"""
    return "`" + clip(s, n).replace("|", "\\|").replace("`", "'") + "`"


# ---------- 命令计数 ----------

PATTERNS = {
    r"\ref": re.compile(r"\\ref\{[^}]*\}"),
    r"\label": re.compile(r"\\label\{[^}]*\}"),
    r"\cite": re.compile(r"\\cite[a-z]*\{[^}]*\}"),
    r"\begin": re.compile(r"\\begin\{[^}]*\}"),
    r"\end": re.compile(r"\\end\{[^}]*\}"),
    r"\input": re.compile(r"\\input\{[^}]*\}"),
    r"%TODO": re.compile(r"%\s*TODO", re.IGNORECASE),
}


def cmd_names(s: str) -> dict[str, int]:
    """块内所有 LaTeX 命令名的多重集，用于判"结构是否变了"。"""
    out: dict[str, int] = {}
    for m in re.findall(r"\\[a-zA-Z@]+\*?", s):
        out[m] = out.get(m, 0) + 1
    return out


def cmd_stats(lines: list[str]) -> dict[str, int]:
    text = "\n".join(lines)
    return {k: len(p.findall(text)) for k, p in PATTERNS.items()}


def word_count(lines: list[str]) -> int:
    """去掉整行注释与命令后的正文词数，用于看"实际改了多少内容"。"""
    out = []
    for ln in lines:
        s = re.sub(r"(?<!\\)%.*$", "", ln)
        s = re.sub(r"\\[a-zA-Z@]+\*?(\[[^\]]*\])?(\{[^{}]*\})*", " ", s)
        out.append(s)
    return len(re.findall(r"[A-Za-z][A-Za-z'-]*", " ".join(out)))


# ---------- hunk 分类 ----------

def classify(old_block: str, new_block: str, ops) -> list[str]:
    """只给**客观可判**的标签。性质（勘误/润色/重写）由人判，机器不越界。

    实测教训：字符相似度无法区分"机械替换"与"实质重写"——
    摘要三句全改（ratio 仍高）与加一条 \ref（ratio 低）会被同一规则判反。
    故本函数不再输出 REWRITE/LOCAL 这类性质判断。
    """
    tags: list[str] = []
    a, b = squash(old_block), squash(new_block)

    if a == b:
        tags.append("WS")                      # 纯空白/换行
    elif not old_block.strip():
        tags.append("PURE_ADD")                # 纯插入，强度必然 100%，不等于超强度
    elif not new_block.strip():
        tags.append("PURE_DEL")
    else:
        non_ws_a = re.sub(r"\s+", "", old_block)
        non_ws_b = re.sub(r"\s+", "", new_block)
        if non_ws_a == non_ws_b:
            tags.append("WS")
        else:
            changed = sum(max(len(x[1]), len(x[2])) for x in ops)
            if changed <= 40 and ratio(old_block, new_block) >= 0.85:
                tags.append("TYPO")            # 小改动：≤40 字符且高度相似

    all_lines = [x for x in (old_block + "\n" + new_block).splitlines() if x.strip()]
    if all_lines and all(x.lstrip().startswith("%") for x in all_lines):
        tags.append("COMMENT")

    # 命令增减：不写死白名单，自动收集两侧命令名的多重集
    ca, cb = cmd_names(old_block), cmd_names(new_block)
    if ca != cb:
        tags.append("CMD")
    return tags


def hunks(old: list[str], new: list[str], ctx: int) -> list[dict]:
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        ob = "\n".join(old[i1:i2])
        nb = "\n".join(new[j1:j2])
        ops = char_ops(ob, nb)
        changed = sum(max(len(x[1]), len(x[2])) for x in ops)
        base = max(len(ob), len(nb), 1)
        out.append({
            "kind": tag,
            "old_range": (i1 + 1, i2),
            "new_range": (j1 + 1, j2),
            "old": ob,
            "new": nb,
            "tags": classify(ob, nb, ops),
            "ops": ops,
            "intensity": changed / base,
            "before": old[max(0, i1 - ctx):i1],
            "after": old[i2:i2 + ctx],
        })
    return out


# ---------- 报告 ----------

def render(old_p: Path, new_p: Path, old: list[str], new: list[str],
           hs: list[dict], expect: int | None, todo_expect: int | None) -> str:
    L: list[str] = []
    so, sn = cmd_stats(old), cmd_stats(new)
    wo, wn = word_count(old), word_count(new)

    L.append(f"# LaTeX 改动审计：{new_p.name}\n")
    L.append(f"- 旧：`{old_p.as_posix()}`（{len(old)} 行）")
    L.append(f"- 新：`{new_p.as_posix()}`（{len(new)} 行）")
    L.append(f"- 改动块（hunk）：**{len(hs)}**")
    if expect is not None:
        L.append(f"- 申报直改：**{expect}** 处；实际改动块 **{len(hs)}**"
                 f"（口径不同，不直接比较，见第四节）")
    if todo_expect is not None:
        L.append(f"- 申报 `% TODO`：**{todo_expect}** → 实际 {sn['%TODO']}，"
                 f"差 {sn['%TODO'] - todo_expect:+d}")
    L.append("")

    # 分类汇总
    counts: dict[str, int] = {}
    for h in hs:
        for t in h["tags"]:
            counts[t] = counts.get(t, 0) + 1
    L.append("## 一、改动分类\n")
    L.append("| 标签 | 含义 | 块数 |")
    L.append("|---|---|---|")
    LEG = {
        "WS": "纯空白/换行（排版级）",
        "TYPO": "小改动：≤40 字符且高度相似",
        "PURE_ADD": "纯插入（强度恒 100%，不据此判超强度）",
        "PURE_DEL": "纯删除",
        "COMMENT": "只在注释里",
        "CMD": "块内 LaTeX 命令有增删",
    }
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        L.append(f"| `{k}` | {LEG.get(k, k)} | {v} |")
    L.append("")

    # 命令计数
    L.append("## 二、命令与篇幅计数\n")
    L.append("| 项 | 旧 | 新 | 差 |")
    L.append("|---|---:|---:|---:|")
    for k in PATTERNS:
        d = sn[k] - so[k]
        flag = "" if d == 0 else ("  ⚠" if abs(d) > 4 else "")
        L.append(f"| `{k}` | {so[k]} | {sn[k]} | {d:+d}{flag} |")
    L.append(f"| 正文词数 | {wo} | {wn} | {wn - wo:+d} |")
    L.append("")

    # 明细
    L.append("## 三、逐块明细\n")
    for idx, h in enumerate(hs, 1):
        rng = (f"旧 L{h['old_range'][0]}–{h['old_range'][1]}"
               if h["old_range"][1] >= h["old_range"][0] else "—")
        nrng = (f"新 L{h['new_range'][0]}–{h['new_range'][1]}"
                if h["new_range"][1] >= h["new_range"][0] else "—")
        nold = len([x for x in h["old"].splitlines() if x.strip()])
        nnew = len([x for x in h["new"].splitlines() if x.strip()])
        L.append(f"### {idx}. {rng} → {nrng}　"
                 f"`{'|'.join(h['tags']) or '—'}`　"
                 f"删 {nold} 行 / 增 {nnew} 行 / 改动占比 {h['intensity']:.0%}\n")
        L.append("```diff")
        for line in h["before"][-1:]:
            L.append("  " + line)
        for line in h["old"].splitlines():
            L.append("- " + line)
        for line in h["new"].splitlines():
            L.append("+ " + line)
        for line in h["after"][:1]:
            L.append("  " + line)
        L.append("```\n")

        shown = [op for op in h["ops"] if len(op[1]) + len(op[2]) > 0][:6]
        if shown:
            L.append("| 旧片段 | → | 新片段 |")
            L.append("|---|---|---|")
            for _t, a, b in shown:
                L.append(f"| {md(a)} | → | {md(b)} |")
            L.append("")

    # 对账清单
    L.append("## 四、对账清单：拿申报单逐条划\n")
    L.append("> 「申报条目」一列**留空**，由人对账填写（如 A1 / C2 / E3）。"
             "**填不出对应条目的块，就是申报单里没有依据的改动。**\n")
    L.append("| # | 位置 | 删/增行 | 占比 | 开头片段 | 申报条目 |")
    L.append("|---|---|---:|---:|---|---|")
    for idx, h in enumerate(hs, 1):
        nold = len([x for x in h["old"].splitlines() if x.strip()])
        nnew = len([x for x in h["new"].splitlines() if x.strip()])
        src = h["new"] or h["old"]
        nonblank = [x for x in src.splitlines() if x.strip()]
        first = nonblank[0] if nonblank else ""
        L.append(f"| {idx} | 旧 L{h['old_range'][0]} | {nold}/{nnew} | "
                 f"{h['intensity']:.0%} | {md(first, 46)} |  |")
    L.append("")

    # 客观异常
    L.append("## 五、客观异常（只列机器能判的，不判性质）\n")
    flags = []
    for idx, h in enumerate(hs, 1):
        tags = h["tags"]
        where = f"第 {idx} 块（旧 L{h['old_range'][0]}）"
        if "PURE_ADD" in tags and "COMMENT" not in tags:
            nnew = len([x for x in h["new"].splitlines() if x.strip()])
            flags.append(f"- {where}：**纯插入 {nnew} 行正文**（无对应旧文本）。")
        elif "PURE_DEL" in tags:
            nold = len([x for x in h["old"].splitlines() if x.strip()])
            flags.append(f"- {where}：**纯删除 {nold} 行**（无对应新文本）。")
        if "CMD" in tags:
            ca, cb = cmd_names(h["old"]), cmd_names(h["new"])
            added = [c for c in cb if c not in ca]
            gone = [c for c in ca if c not in cb]
            if added or gone:
                parts = []
                if added:
                    parts.append("新增 " + ", ".join(f"`{c}`" for c in added[:5]))
                if gone:
                    parts.append("移除 " + ", ".join(f"`{c}`" for c in gone[:5]))
                flags.append(f"- {where}：命令" + "；".join(parts) + "——结构级。")
    if expect is not None:
        flags.append(
            f"- 申报 **{expect}** 处直改、实际 **{len(hs)}** 块："
            f"**两数不可直接比较**（一处改动可拆成多块，多条相邻改动会合成一块）。"
            f"本报告不据此判越界。")
    if not flags:
        flags.append("- 无纯插入/纯删除，块内命令无种类增删。")
    L.extend(flags)
    L.append("")
    L.append("---\n")
    L.append("> 机器只给客观量（行号、删增行数、改动占比、命令增删、原片段），"
             "**不判改动性质**。性质与「是否越界」由人按申报单对账、由归口按判据判定，"
             "且审者不应看到改动理由（盲审）。")
    return "\n".join(L)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--expect", type=int, default=None)
    ap.add_argument("--todo", type=int, default=None)
    ap.add_argument("--ctx", type=int, default=1)
    a = ap.parse_args()

    old_p, new_p = Path(a.old), Path(a.new)
    for p in (old_p, new_p):
        if not p.is_file():
            print(f"找不到文件：{p}", file=sys.stderr)
            return 2

    old, new = read(old_p), read(new_p)
    hs = hunks(old, new, a.ctx)
    report = render(old_p, new_p, old, new, hs, a.expect, a.todo)

    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()                      # 幂等：先清旧产物
        out.write_text(report, encoding="utf-8")
        print(f"报告：{out}")
    else:
        print(report)
    print(f"改动块 {len(hs)}；命令计数 "
          + ", ".join(f"{k}={v}" for k, v in cmd_stats(new).items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
