# -*- coding: utf-8 -*-
"""office_lint 引擎：docx/pptx 只报客观项 → 统一 evidence JSON（stdout）。

分工：页面 PNG 由 Host 侧渲染（Node；docx 优先用 Microsoft Word 只读导出 PDF，
失败回退 officeKit/LibreOffice），本引擎负责
  1. 空白页：逐页算墨迹占比（<0.001% 视为空白，页面上的页码不会误报）
  2. 图片有效分辨率：OOXML 里显示尺寸（EMU）× 内嵌图片像素数 → DPI
  3. 字体回退：officeKit 返回的 missingFonts
  4. pptx：PowerPoint 记录下来的自动缩小（normAutofit fontScale < 100%）
  5. pptx：图表引用范围行数 vs 缓存点数（c:f 的 $B$2:$B$5 对 c:ptCount）
  6. docx：Word 自报页数（docProps/app.xml 的 <Pages>）vs 渲染页数；
     不一致时页号不可搬用，空白页降级为「未验证」而不是 fail

不判美观、不判版式好坏；查不了的项如实标 unknown。
渲染结果经 stdin 传入（base64 PNG），不落临时文件。

用法：
    python office_lint.py --file X.docx [--report 报告.md] < pages.json
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

EMU_PER_INCH = 914400
INK_THRESHOLD = 200        # 灰度 < 200 算墨迹
BLANK_RATIO = 1e-5         # 墨迹占比低于此视为空白页
DPI_WARN = 150
DPI_ERROR = 100
MIN_DISPLAY_INCH = 0.5     # 小于半英寸的图（图标、公式）不判分辨率

RANGE = re.compile(r"\$?[A-Za-z]+\$?(\d+)(?::\$?[A-Za-z]+\$?(\d+))?")


def finding(severity: str, code: str, where: str, detail: str, evidence: str = "") -> dict:
    return {"severity": severity, "code": code, "where": where, "detail": detail, "evidence": evidence}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def iter_local(root, name: str):
    for element in root.iter():
        if local(element.tag) == name:
            yield element


def attr(element, name: str) -> str | None:
    """按本地名读属性（OOXML 各部件命名空间不同）。"""
    if element is None:
        return None
    for key, value in element.attrib.items():
        if key == name or key.endswith("}" + name):
            return value
    return None


def theme_families(zf: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    """theme1.xml 的 major/minor 字体方案；ea/cs 为空时退回 script 字体。"""
    try:
        root = ElementTree.fromstring(zf.read("word/theme/theme1.xml"))
    except (KeyError, ElementTree.ParseError):
        return {}
    themes: dict[str, dict[str, str]] = {}
    for scheme, node_name in (("major", "majorFont"), ("minor", "minorFont")):
        node = next(iter_local(root, node_name), None)
        if node is None:
            continue
        slots = {"latin": "", "ea": "", "cs": ""}
        scripts: dict[str, str] = {}
        for child in node:
            tag = local(child.tag)
            typeface = child.get("typeface") or ""
            if tag in slots:
                slots[tag] = typeface
            elif tag == "font" and child.get("script"):
                scripts[child.get("script")] = typeface
        slots["ea"] = slots["ea"] or scripts.get("Hans", "") or scripts.get("Hant", "")
        slots["cs"] = slots["cs"] or scripts.get("Arab", "")
        themes[scheme] = slots
    return themes


def resolve_theme(value: str, themes: dict[str, dict[str, str]]) -> list[str]:
    """minorHAnsi / majorEastAsia / minorBidi → 具体字体族。"""
    scheme = "major" if value.startswith("major") else "minor" if value.startswith("minor") else ""
    if scheme == "" or scheme not in themes:
        return []
    slots = themes[scheme]
    slot = value[len(scheme):].lower()
    if slot in ("ascii", "hansi"):
        return [slots["latin"]] if slots["latin"] else []
    if slot == "eastasia":
        return [slots["ea"]] if slots["ea"] else []
    if slot in ("bidi", "cs"):
        return [slots["cs"]] if slots["cs"] else []
    return []


def rfonts_families(element, themes: dict[str, dict[str, str]]) -> set[str]:
    families: set[str] = set()
    for name in ("ascii", "hAnsi", "eastAsia", "cs"):
        value = attr(element, name)
        if value:
            families.add(value)
    for name in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme", "csTheme"):
        value = attr(element, name)
        if value:
            families.update(resolve_theme(value, themes))
    return families


def used_font_families(zf: zipfile.ZipFile) -> set[str] | None:
    """docx 实际引用到的字体族（小写）。

    只算 document.xml 里的 run/段落直接格式、被 pStyle/rStyle/tblStyle 引用的样式
    及其 basedOn 链、默认样式与 docDefaults；不扫 styles.xml 里没人引用的样式定义。
    主题字体按 theme1.xml 解析。解析失败返回 None，调用方不做过滤。
    """
    try:
        document = ElementTree.fromstring(zf.read("word/document.xml"))
        styles_root = ElementTree.fromstring(zf.read("word/styles.xml"))
    except (KeyError, ElementTree.ParseError):
        return None
    themes = theme_families(zf)

    styles: dict[str, dict] = {}
    for element in iter_local(styles_root, "style"):
        style_id = attr(element, "styleId")
        if not style_id:
            continue
        fonts: set[str] = set()
        for rfonts in iter_local(element, "rFonts"):
            fonts |= rfonts_families(rfonts, themes)
        based = next(iter_local(element, "basedOn"), None)
        styles[style_id] = {
            "basedOn": attr(based, "val") or "",
            "fonts": fonts,
            "default": (attr(element, "default") or "").lower() in ("1", "true"),
        }

    used: set[str] = set()
    for defaults in iter_local(styles_root, "docDefaults"):
        for rfonts in iter_local(defaults, "rFonts"):
            used |= rfonts_families(rfonts, themes)

    for rfonts in iter_local(document, "rFonts"):
        used |= rfonts_families(rfonts, themes)

    referenced: set[str] = {
        value
        for tag in ("pStyle", "rStyle", "tblStyle")
        for node in iter_local(document, tag)
        for value in [attr(node, "val")]
        if value
    }
    referenced.update(style_id for style_id, info in styles.items() if info["default"])

    pending = list(referenced)
    seen: set[str] = set()
    while pending:
        style_id = pending.pop()
        if style_id in seen:
            continue
        seen.add(style_id)
        info = styles.get(style_id)
        if info is None:
            continue
        used |= info["fonts"]
        if info["basedOn"]:
            pending.append(info["basedOn"])

    return {name.strip().lower() for name in used if name and name.strip()}


def embedded_rel(blip) -> str:
    for key, value in blip.attrib.items():
        if key.endswith("}embed"):
            return value
    return ""


def word_page_count(zf: zipfile.ZipFile) -> int:
    """docx 的 docProps/app.xml 里 Word 自报的页数；没有记录或非正数返回 0。

    Word 保存时会写入这个值。它不代表当前 Word 版本重新排版后的页数，
    但它至少是「交付方当时的页数」，可用于判断渲染分页是否可比。
    """
    try:
        root = ElementTree.fromstring(zf.read("docProps/app.xml"))
    except (KeyError, ElementTree.ParseError):
        return 0
    for element in root.iter():
        if local(element.tag) != "Pages" or not element.text:
            continue
        try:
            value = int(element.text.strip())
        except ValueError:
            return 0
        return value if value > 0 else 0
    return 0


def core_modified(zf: zipfile.ZipFile) -> datetime | None:
    """docProps/core.xml 的 dcterms:modified（UTC）；缺失或解析失败返回 None。"""
    try:
        root = ElementTree.fromstring(zf.read("docProps/core.xml"))
    except (KeyError, ElementTree.ParseError):
        return None
    for element in root.iter():
        if local(element.tag) != "modified" or not element.text:
            continue
        try:
            return datetime.fromisoformat(element.text.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def docprops_stale(modified: datetime | None, path: Path) -> bool:
    """modified 与文件 mtime 相差超过 1 天 → docProps 可能是旧值。"""
    if modified is None:
        return False
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    return abs((mtime - modified).total_seconds()) > 86400


def rel_targets(zf: zipfile.ZipFile, rels_path: str) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(zf.read(rels_path))
    except (KeyError, ElementTree.ParseError):
        return {}
    targets = {}
    for relationship in root.iter():
        if local(relationship.tag) != "Relationship":
            continue
        rid = relationship.get("Id")
        target = relationship.get("Target")
        if rid and target:
            targets[rid] = target
    return targets


def media_path(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    normalized = Path(base, target).as_posix()
    parts = []
    for piece in normalized.split("/"):
        if piece == "..":
            if parts:
                parts.pop()
        elif piece not in ("", "."):
            parts.append(piece)
    return "/".join(parts)


def image_size(zf: zipfile.ZipFile, media: str) -> tuple[int, int] | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(zf.read(media))) as image:
            return image.size
    except (KeyError, OSError, ValueError):
        return None


def first_extent(element):
    """docx 用 wp:extent，pptx 用 a:ext；都带 cx/cy（EMU）。"""
    for candidate in element.iter():
        if local(candidate.tag) in ("extent", "ext") and candidate.get("cx") and candidate.get("cy"):
            return candidate
    return None


def check_images(zf: zipfile.ZipFile, part: str, rels: str, base: str, findings: list[dict], kind: str) -> tuple[int, float]:
    try:
        root = ElementTree.fromstring(zf.read(part))
    except (KeyError, ElementTree.ParseError) as error:
        findings.append(finding("warn", "PART_UNREADABLE", part, "XML 解析失败", f"{type(error).__name__}"))
        return 0, 0.0
    targets = rel_targets(zf, rels)
    checked = 0
    worst = 0.0
    for container in iter_local(root, "pic" if kind == "powerpoint" else "drawing"):
        blip = next(iter_local(container, "blip"), None)
        extent = first_extent(container)
        if blip is None or extent is None:
            continue
        rid = embedded_rel(blip)
        target = targets.get(rid)
        if not target:
            continue
        try:
            width_inches = int(extent.get("cx")) / EMU_PER_INCH
        except (TypeError, ValueError):
            continue
        if width_inches < MIN_DISPLAY_INCH:
            continue
        size = image_size(zf, media_path(base, target))
        if size is None:
            findings.append(finding("warn", "IMAGE_UNREADABLE", f"{part}:{target}", "读不出内嵌图片尺寸"))
            continue
        pixel_width, _pixel_height = size
        dpi = pixel_width / width_inches if width_inches > 0 else 0.0
        checked += 1
        worst = dpi if worst == 0.0 else min(worst, dpi)
        if dpi < DPI_ERROR:
            findings.append(finding("error", "LOW_DPI_IMAGE", f"{part}:{target}",
                                    f"有效分辨率 {dpi:.0f} DPI（{pixel_width}px 铺 {width_inches:.2f} 英寸）",
                                    "低于 100 DPI，打印会糊"))
        elif dpi < DPI_WARN:
            findings.append(finding("warn", "LOW_DPI_IMAGE", f"{part}:{target}",
                                    f"有效分辨率 {dpi:.0f} DPI（{pixel_width}px 铺 {width_inches:.2f} 英寸）",
                                    "低于 150 DPI"))
    return checked, worst


def check_docx(zf: zipfile.ZipFile, findings: list[dict]) -> dict:
    checked, worst = check_images(zf, "word/document.xml", "word/_rels/document.xml.rels", "word", findings, "word")
    return {"imagesChecked": checked, "minImageDpi": worst}


def check_pptx(zf: zipfile.ZipFile, findings: list[dict]) -> dict:
    slides = sorted(name for name in zf.namelist()
                    if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
    checked = 0
    worst = 0.0
    for slide in slides:
        rels = slide.replace("ppt/slides/", "ppt/slides/_rels/") + ".rels"
        part_checked, part_worst = check_images(zf, slide, rels, "ppt/slides", findings, "powerpoint")
        checked += part_checked
        if part_worst and (worst == 0.0 or part_worst < worst):
            worst = part_worst

    shrunk = 0
    for slide in slides:
        try:
            root = ElementTree.fromstring(zf.read(slide))
        except (KeyError, ElementTree.ParseError):
            continue
        for element in iter_local(root, "normAutofit"):
            scale = element.get("fontScale")
            if not scale:
                continue
            try:
                value = int(scale)
            except ValueError:
                continue
            if value < 100000:
                shrunk += 1
                findings.append(finding("warn", "TEXT_AUTOFIT_SHRUNK", slide,
                                        f"PowerPoint 记录了自动缩字：fontScale={value / 1000:.0f}%",
                                        "说明文本框在原字号下放不下"))

    charts = sorted(name for name in zf.namelist()
                    if name.startswith("ppt/charts/chart") and name.endswith(".xml"))
    mismatches = 0
    for chart in charts:
        try:
            root = ElementTree.fromstring(zf.read(chart))
        except (KeyError, ElementTree.ParseError):
            continue
        for series in iter_local(root, "ser"):
            for value in series:
                if local(value.tag) not in ("val", "yVal"):
                    continue
                num_ref = next(iter_local(value, "numRef"), None)
                if num_ref is None:
                    continue
                formula = next(iter_local(num_ref, "f"), None)
                point_count = next(iter_local(num_ref, "ptCount"), None)
                if formula is None or point_count is None or not formula.text:
                    continue
                match = RANGE.search(formula.text.split("!")[-1])
                if match is None:
                    continue
                low = int(match.group(1))
                high = int(match.group(2)) if match.group(2) else low
                declared = int(point_count.get("val") or 0)
                rows = high - low + 1
                if rows != declared:
                    mismatches += 1
                    findings.append(finding(
                        "warn", "CHART_REF_MISMATCH", chart,
                        f"图表引用 {formula.text}（{rows} 行）与缓存点数 {declared} 不一致",
                        "引用范围与数据行号错位时，图上的点会整体偏移",
                    ))
    return {"imagesChecked": checked, "minImageDpi": worst, "slides": len(slides),
            "autofitShrunk": shrunk, "charts": len(charts), "chartRefMismatches": mismatches}


def check_pages(pages: list[dict], findings: list[dict]) -> dict:
    """逐页算墨迹占比；返回统计与空白页列表。"""
    try:
        from PIL import Image
    except ImportError:
        findings.append(finding("warn", "PIL_MISSING", "-", "环境里没有 Pillow，空白页检查未做"))
        return {"minInkRatio": 0.0, "blankPages": [], "checked": 0}
    ratios = []
    blanks = []
    for page in pages:
        try:
            payload = base64.b64decode(page.get("png", ""))
            with Image.open(io.BytesIO(payload)) as image:
                gray = image.convert("L")
                histogram = gray.histogram()
                total = gray.width * gray.height
        except Exception as error:  # noqa: BLE001 - 单页坏了不该中断整份
            findings.append(finding("warn", "PAGE_UNREADABLE", f"第 {page.get('page')} 页",
                                    "页面 PNG 无法解析", f"{type(error).__name__}"))
            continue
        if total == 0:
            continue
        dark = sum(histogram[:INK_THRESHOLD])
        ratio = dark / total
        ratios.append((page.get("page"), ratio))
        if ratio < BLANK_RATIO:
            blanks.append(page.get("page"))
            findings.append(finding("error", "BLANK_PAGE", f"第 {page.get('page')} 页",
                                    "整页没有墨迹：渲染后是空白页",
                                    f"墨迹占比 {ratio:.2e}"))
    minimum = min((ratio for _, ratio in ratios), default=0.0)
    return {"minInkRatio": minimum, "blankPages": [page for page in blanks if page is not None],
            "checked": len(ratios), "ratios": ratios}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        # Node 写进来的是 UTF-8；Windows 默认按本机代码页解码，中文字体名会炸。
        sys.stdin.reconfigure(encoding="utf-8")
    started = time.monotonic()

    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    target = Path(args.file)
    if not target.is_file():
        print(f"找不到文件：{target}", file=sys.stderr)
        return 2
    extension = target.suffix.lower().lstrip(".")
    if extension not in ("docx", "pptx"):
        print(f"只支持 docx / pptx，收到 .{extension}", file=sys.stderr)
        return 2

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        print(f"stdin 不是 JSON：{error}", file=sys.stderr)
        return 2
    pages = payload.get("pages") or []
    page_count = int(payload.get("pageCount") or 0)
    dpi = int(payload.get("dpi") or 0)
    missing_fonts = [str(name) for name in (payload.get("missingFonts") or [])]
    render_ms = int(payload.get("renderMs") or 0)
    render_error = str(payload.get("renderError") or "")
    renderer = str(payload.get("renderer") or "libreoffice")
    renderer_note = str(payload.get("rendererNote") or "")

    findings: list[dict] = []
    declared_pages = 0
    stale_docprops = False
    stats = {
        "file": str(target),
        "extension": extension,
        "kind": "powerpoint" if extension == "pptx" else "word",
        "pages": page_count,
        "renderedPages": len(pages),
        "dpi": dpi,
        "minInkRatio": 0.0,
        "blankPages": [],
        "missingFonts": missing_fonts,
        "imagesChecked": 0,
        "lowDpiImages": 0,
        "minImageDpi": 0.0,
        "slides": 0,
        "autofitShrunk": 0,
        "charts": 0,
        "chartRefMismatches": 0,
        "renderMs": render_ms,
        "renderError": render_error,
        "renderer": renderer,
        "rendererNote": renderer_note,
        "durationMs": 0,
    }

    if render_error:
        findings.append(finding("warn", "RENDER_FAILED", "-",
                                "Host 渲染失败，空白页与字体检查未做；结构检查仍然完成",
                                common.clip(render_error, 200)))
    elif not pages:
        findings.append(finding("warn", "NO_PAGES", "-",
                                "没有拿到渲染页面：空白页检查未做", f"pageCount={page_count}"))
    else:
        page_stats = check_pages(pages, findings)
        stats["minInkRatio"] = round(page_stats["minInkRatio"], 8)
        stats["blankPages"] = page_stats["blankPages"]

    try:
        with zipfile.ZipFile(target) as archive:
            if extension == "docx":
                used_fonts = used_font_families(archive)
                if used_fonts:
                    missing_fonts = [name for name in missing_fonts if name.strip().lower() in used_fonts]
                declared_pages = word_page_count(archive)
                stale_docprops = docprops_stale(core_modified(archive), target)
                structure = check_docx(archive, findings)
            else:
                structure = check_pptx(archive, findings)
            stats.update(structure)
    except zipfile.BadZipFile as error:
        findings.append(finding("error", "CORRUPT_CONTAINER", str(target),
                                "不是有效的 OOXML 包", f"{type(error).__name__}: {error}"))

    for family in missing_fonts:
        findings.append(finding("warn", "FONT_MISSING", family,
                                "字体未安装，渲染时发生了回退", family))
    stats["missingFonts"] = missing_fonts

    # 页数不可比时，渲染器的页号不能搬到 Word：空白页只报 info/warn，绝不判 fail。
    # 只有 render == word 时 BLANK_PAGE 才保留 error；没有 Word 页数记录同样不算相等。
    if extension == "docx" and page_count > 0 and declared_pages != page_count \
            and (declared_pages > 0 or stats["blankPages"]):
        blank_pages = list(stats["blankPages"])
        stats["blankPages"] = []
        findings[:] = [item for item in findings if item["code"] != "BLANK_PAGE"]
        if declared_pages > 0:
            ratio = abs(page_count - declared_pages) / declared_pages
            cause = "docProps 可能过时" if stale_docprops else "渲染器分页不可信"
            findings.append(finding(
                "warn", "PAGE_COUNT_MISMATCH", "-",
                f"Word 记录 {declared_pages} 页，渲染 {page_count} 页（差 {ratio:.0%}；{cause}）",
                "页号不可搬用；只有渲染页数与 Word 记录相等时才判 fail",
            ))
            severity = "info" if ratio > 0.15 else "warn"
            detail = ("渲染多出的页无墨迹，可能是分页差异，不代表文档有空白页"
                      if severity == "info"
                      else f"渲染未见墨迹，但页数与 Word 记录不一致（{declared_pages} vs {page_count}）")
        else:
            findings.append(finding(
                "warn", "PAGE_COUNT_UNKNOWN", "-",
                "docProps/app.xml 没有 Word 页数记录，渲染页号不可搬用",
                "页号只属于渲染器；空白页检查降级为未验证",
            ))
            severity = "info"
            detail = "渲染页面无墨迹，但无法与 Word 页数对照，不代表文档有空白页"
        for page in blank_pages:
            findings.append(finding(severity, "BLANK_PAGE_UNVERIFIED", f"第 {page} 页",
                                    detail, "无法确认是真实空白页"))

    stats["lowDpiImages"] = sum(1 for item in findings if item["code"] == "LOW_DPI_IMAGE")
    stats["minImageDpi"] = round(float(stats["minImageDpi"]), 1)
    stats["durationMs"] = int((time.monotonic() - started) * 1000)

    has_error = any(item["severity"] == "error" for item in findings)
    incomplete = any(item["code"] in ("NO_PAGES", "PAGE_UNREADABLE", "PIL_MISSING", "PART_UNREADABLE",
                                      "RENDER_FAILED", "PAGE_COUNT_MISMATCH", "PAGE_COUNT_UNKNOWN")
                     for item in findings)
    if has_error:
        verdict = "fail"
    elif incomplete or stats["renderedPages"] < stats["pages"]:
        verdict = "unknown"
    else:
        verdict = "pass"

    report_path = ""
    if args.report:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        if report.exists():
            report.unlink()
        report.write_text(render_report(stats, findings, verdict, declared_pages), encoding="utf-8")
        report_path = str(report)

    result = {
        "tool": "office_lint",
        "target": str(target),
        "hash": common.sha256_file(target),
        "verdict": verdict,
        "findings": findings[: common.MAX_FINDINGS],
        "findingsTotal": len(findings),
        "stats": stats,
        "reportPath": report_path,
        "receipt": common.receipt(),
    }
    return common.emit(result)


def render_report(stats: dict, findings: list[dict], verdict: str, declared_pages: int = 0) -> str:
    if declared_pages > 0:
        pages_line = f"Word 记录 {declared_pages} 页，渲染 {stats['pages']} 页"
    else:
        pages_line = f"页数 {stats['pages']}（渲染 {stats['renderedPages']}）"
    renderer_label = "Word COM（PDF）" if stats["renderer"] == "word-com" else "LibreOffice"
    renderer_line = f"- 渲染器：{renderer_label}"
    if stats["rendererNote"]:
        renderer_line += f"（回退原因：{stats['rendererNote']}）"
    rendered_only = declared_pages > 0 and declared_pages != stats["pages"]
    blank_label = "空白页（渲染器，未验证）" if rendered_only else "空白页"
    ink_label = "最小墨迹占比（渲染器）" if rendered_only else "最小墨迹占比"
    lines = [
        f"# 排版质检报告：{Path(stats['file']).name}",
        "",
        f"- 文件：`{stats['file']}`（{stats['extension']}）",
        f"- 结论：**{verdict}**　{pages_line} @ {stats['dpi']} DPI",
        renderer_line,
        f"- 耗时：渲染 {stats['renderMs']} ms + 检查 {stats['durationMs']} ms",
        "",
        "## 一、客观量",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| Word 记录页数 | {declared_pages or '未记录'} |",
        f"| {blank_label} | {stats['blankPages'] or '无'} |",
        f"| {ink_label} | {stats['minInkRatio']:.2e} |",
        f"| 检查图片数 | {stats['imagesChecked']} |",
        f"| 低分辨率图片 | {stats['lowDpiImages']}（最低 {stats['minImageDpi']} DPI） |",
        f"| 缺失字体 | {', '.join(stats['missingFonts']) or '无'} |",
    ]
    if stats["slides"]:
        lines.append(f"| 幻灯片 | {stats['slides']}（自动缩字 {stats['autofitShrunk']} 处） |")
        lines.append(f"| 图表 | {stats['charts']}（引用与点数不一致 {stats['chartRefMismatches']} 处） |")
    lines.extend(["", "## 二、发现项", ""])
    if not findings:
        lines.append("无。")
    else:
        for item in findings:
            lines.append(f"- [{item['severity']}] `{item['code']}` {item['where']}：{item['detail']}"
                         + (f"（{item['evidence']}）" if item["evidence"] else ""))
    lines.extend([
        "",
        "---",
        "",
        "> 只报客观项：空白页、图片有效分辨率、字体回退、自动缩字记录、图表引用与数据行数。"
        "不判美观，不判版式好坏。docx 优先用 Microsoft Word 导出 PDF 再渲染，页数与 Word 一致；"
        "Word 不可用时回退 LibreOffice Kit 并在报告写明，分页可能与 Microsoft Office 有差异。",
        "",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
