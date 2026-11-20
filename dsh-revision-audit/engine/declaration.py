# -*- coding: utf-8 -*-
"""申报单（直改清单）解析与锚点匹配。

只做**确定性**的机械对账，不判语义：

  · 细粒度申报单（`# | 位置 | 原文 → 改法 | 理由`）：从「原文」列的反引号片段取锚点，
    逐个改动块检查该片段是否出现在块文本中（去掉全部空白后比较）。
  · 文件级申报单（`条目 | 文件 | 块数 | 申报内容`）：没有锚点可比，只能判"覆盖不确定"，
    绝不报"未申报"——这是 research/2026-11-16-改动留痕与归口审.md §6.1 的口径：
    总数与块数不可直接对账，申报单自己就会数错。

已知限制（写在这里，不要偷偷放宽）：
  · 只读 Markdown 表格；纯列表/自由段落里的申报行不解析。
  · 反引号片段太短（<8 个非空白字符）或像路径（含 / \\ 或 .tex 等后缀）的，不当锚点。
  · 表格单元格里转义的 `\\|` 会被当列分隔，暂不处理。
"""
from __future__ import annotations

import re

MIN_ANCHOR = 8

_SEP_CELL = re.compile(r"^:?-{3,}:?$")
_ID_CELL = re.compile(r"^[A-Za-z]{0,3}\d+$")
_BACKTICK = re.compile(r"`([^`]+)`")
_ARROW = re.compile(r"→|->|=>")
_ELLIPSIS = re.compile(r"…|\.\.\.")
_PATH_LIKE = re.compile(r"[\\/]|\.(?:tex|pdf|docx|pptx|xlsx|md|bib|png|jpe?g|svg)$", re.IGNORECASE)

# 表头里出现这些列名，才认成申报表；`| 项 | 内容 |` 这类键值表不算。
_COL_WHERE = "位置"
_COL_ANCHOR = ("原文", "改法", "申报内容")
_COL_ID = "条目"
_COL_FILE = "文件"
_COL_COUNT = "块数"


def normalize(text: str) -> str:
    """去掉全部空白——LaTeX 换行/缩进不影响"是不是同一段文字"。"""
    return re.sub(r"\s+", "", text or "")


def split_anchors(cell: str) -> list[dict]:
    """从一个单元格里取出可用的锚点。

    @returns [{{text, norm}}]：text 保留原样供报告显示，norm 去掉全部空白供匹配。
    """
    anchors: list[dict] = []
    for span in _BACKTICK.findall(cell or ""):
        head = _ARROW.split(span)[0]
        for piece in _ELLIPSIS.split(head):
            if _PATH_LIKE.search(piece.strip()):
                continue
            anchor = normalize(piece)
            if len(anchor) < MIN_ANCHOR:
                continue
            if any(existing["norm"] == anchor for existing in anchors):
                continue
            anchors.append({"text": piece.strip(), "norm": anchor})
    return anchors


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _column(header: list[str], name: str) -> int | None:
    for index, cell in enumerate(header):
        if cell == name:
            return index
    return None


def _anchor_column(header: list[str]) -> int | None:
    for index, cell in enumerate(header):
        for keyword in _COL_ANCHOR:
            if keyword in cell:
                return index
    return None


def parse(text: str) -> dict:
    """解析申报单文本。

    @returns {{items: list[dict], file_level: bool, notes: list[str]}}
    """
    items: list[dict] = []
    notes: list[str] = []
    file_level = False
    header: list[str] | None = None
    accepted = False
    tables = 0

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            header = None
            continue
        cells = _cells(stripped)
        if header is None:
            header = cells
            accepted = (
                _COL_WHERE in cells
                or _COL_ID in cells
                or _COL_FILE in cells
                or _COL_COUNT in cells
                or _anchor_column(cells) is not None
            )
            if accepted:
                tables += 1
                file_level = _COL_FILE in cells
            continue
        if not accepted:
            continue
        if cells and all(_SEP_CELL.match(cell) for cell in cells if cell != ""):
            continue

        id_index = _column(header, _COL_ID) or 0
        id_text = cells[id_index] if id_index < len(cells) else ""
        if not _ID_CELL.match(id_text):
            continue            # 合计行、键值行、脚注行：不认成条目
        anchor_index = _anchor_column(header)
        where_index = _column(header, _COL_WHERE)
        anchor_cell = cells[anchor_index] if anchor_index is not None and anchor_index < len(cells) else ""
        items.append({
            "id": id_text,
            "where": cells[where_index] if where_index is not None and where_index < len(cells) else "",
            "anchors": [] if file_level else split_anchors(anchor_cell),
            "raw": anchor_cell[:160],
        })

    if tables == 0:
        notes.append("未找到申报表的 Markdown 表格（要求含「位置」或「原文/改法/申报内容」列，或「条目/文件/块数」列）")
    if file_level:
        notes.append("申报单是文件级（有「文件」列）：本工具只比一对文件，无逐块锚点，只能判覆盖不确定")
    unanchored = [item for item in items if not item["anchors"]] if not file_level else items
    if items and unanchored:
        notes.append(f"{len(unanchored)}/{len(items)} 条没有可用锚点（反引号片段过短或像路径），不能机器归块")

    return {"items": items, "file_level": file_level, "notes": notes}


def match(hunks: list[dict], items: list[dict]) -> tuple[dict[int, list[str]], dict[str, bool]]:
    """把锚点落到改动块上。

    @returns (每块的条目 id 列表, 每条目是否命中) —— 多对多，允许一条覆盖多块、一块受多条覆盖。
    """
    per_hunk: dict[int, list[str]] = {index: [] for index in range(1, len(hunks) + 1)}
    item_matched: dict[str, bool] = {item["id"]: False for item in items}
    for index, hunk in enumerate(hunks, 1):
        old_norm = normalize(hunk["old"])
        new_norm = normalize(hunk["new"])
        for item in items:
            for anchor in item["anchors"]:
                if anchor["norm"] in old_norm or anchor["norm"] in new_norm:
                    per_hunk[index].append(item["id"])
                    item_matched[item["id"]] = True
                    break
    return per_hunk, item_matched
