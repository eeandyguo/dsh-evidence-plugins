# -*- coding: utf-8 -*-
"""backup_verify 引擎：备份包/目录 → 临时目录真还原 → 逐文件哈希 + 抽样打开 → 统一 evidence JSON。

验证的是"这份备份能不能被完整、无损地还原出来"，不是"它是否与某台机器上的原始数据一致"
（原始数据本工具看不到，报告里不冒充）。

流程：
  1. 列清单：逐个文件算 sha256（zip 按条目流式读，目录按文件读）
  2. 真还原：zip 解到临时目录（防 zip-slip、字节数上限），目录直接复制
  3. 对账：还原后的清单与原清单逐文件比哈希，多/少都报
  4. 抽样打开：docx/pptx/xlsx 解包并解析 [Content_Types].xml、sqlite 跑 integrity_check、zip 跑 testzip
  5. 清临时目录（finally，报告里记 tempRemoved）

用法：
    python backup_verify.py --source 备份.zip [--report 报告.md]
    python backup_verify.py --source D:\backup\目录 [--max-files 20000] [--max-bytes 4294967296]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DEFAULT_MAX_FILES = 20000
DEFAULT_MAX_BYTES = 4 * 1024 ** 3
DEFAULT_SAMPLE = 25

OOXML = {".docx", ".pptx", ".xlsx"}
SQLITE = {".sqlite", ".sqlite3", ".db"}
ZIPS = {".zip", ".jar", ".whl"}

# 办公软件/系统生成的临时或锁文件：保留哈希对账，但不参与抽样打开。
TEMP_BASENAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


def is_temp_file(name: str) -> bool:
    """`~$*`（Word/Excel Owner 锁）、`.~lock.*`（LibreOffice 锁）与系统临时文件。"""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base.startswith("~$") or base.startswith(".~lock."):
        return True
    if base in TEMP_BASENAMES:
        return True
    return base.endswith(".tmp")


def make_temp_dir(prefix: str = "dsh-backup-verify-") -> Path:
    """自建临时目录，**不用 tempfile.mkdtemp**。

    mkdtemp 用 0700 建目录，在 DSH 的 Windows 受限令牌（沙箱）下会得到一个不可写的目录
    （往里建子目录/写文件都报 WinError 5）；普通 mkdir 继承父目录 ACL，两种环境都正常。
    """
    base = Path(tempfile.gettempdir())
    for _ in range(50):
        candidate = base / f"{prefix}{os.getpid()}-{secrets.token_hex(4)}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"在 {base} 下创建临时目录失败")


class UnsafeEntry(Exception):
    """压缩包条目指向临时目录之外。"""


def clear_readonly_then_retry(function, target, _error) -> None:
    """Windows：只读文件会挡住 unlink，先清属性再重试一次。"""
    try:
        os.chmod(target, 0o666)
        function(target)
    except OSError:
        pass


def remove_tree(path: Path) -> bool:
    """删临时目录并返回是否真的删掉；只读残留先清属性。"""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear_readonly_then_retry)
    else:
        shutil.rmtree(path, onerror=clear_readonly_then_retry)
    return not path.exists()


def finding(severity: str, code: str, where: str, detail: str, evidence: str = "") -> dict:
    return {"severity": severity, "code": code, "where": where, "detail": detail, "evidence": evidence}


def sha256_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1 << 20), b""):
        digest.update(chunk)
    return digest.hexdigest()


def aggregate(files: list[tuple[str, int, str]]) -> str:
    digest = hashlib.sha256()
    for name, size, file_hash in sorted(files):
        digest.update(f"{name}\0{size}\0{file_hash}\n".encode("utf-8"))
    return digest.hexdigest()


def safe_target(root: Path, name: str) -> Path:
    normalized = name.replace("\\", "/").lstrip("/")
    if re.match(r"^[A-Za-z]:", normalized) or ".." in normalized.split("/"):
        raise UnsafeEntry(name)
    target = (root / normalized).resolve()
    base = root.resolve()
    if target != base and base not in target.parents:
        raise UnsafeEntry(name)
    return target


def manifest_dir(source: Path, max_files: int, max_bytes: int, findings: list[dict]) -> tuple[list, int]:
    files: list[tuple[str, int, str]] = []
    total_bytes = 0
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        if path.is_symlink():
            findings.append(finding("warn", "SYMLINK_SKIPPED", str(path.relative_to(source)),
                                    "符号链接不纳入备份验证，已跳过"))
            continue
        if len(files) >= max_files:
            raise OverflowError(f"文件数超过上限 {max_files}")
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > max_bytes:
            raise OverflowError(f"总字节数超过上限 {max_bytes}")
        with path.open("rb") as handle:
            files.append((str(path.relative_to(source)).replace("\\", "/"), size, sha256_stream(handle)))
    return files, total_bytes


def manifest_zip(archive: zipfile.ZipFile, max_files: int, max_bytes: int) -> tuple[list, int]:
    files: list[tuple[str, int, str]] = []
    total_bytes = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        if len(files) >= max_files:
            raise OverflowError(f"文件数超过上限 {max_files}")
        digest = hashlib.sha256()
        read_bytes = 0
        with archive.open(info) as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                read_bytes += len(chunk)
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise OverflowError(f"总字节数超过上限 {max_bytes}")
                digest.update(chunk)
        files.append((info.filename.replace("\\", "/"), read_bytes, digest.hexdigest()))
    return files, total_bytes


def restore_zip(archive: zipfile.ZipFile, root: Path, max_bytes: int, findings: list[dict]) -> int:
    total = 0
    root.mkdir(parents=True, exist_ok=True)
    for info in archive.infolist():
        if info.is_dir():
            continue
        target = safe_target(root, info.filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, target.open("wb") as sink:
            written = 0
            for chunk in iter(lambda: source.read(1 << 20), b""):
                written += len(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise OverflowError(f"解压总字节数超过上限 {max_bytes}")
                sink.write(chunk)
        if written != info.file_size:
            findings.append(finding("error", "SHORT_READ", info.filename,
                                    "解压得到的数据长度与压缩包记录不符",
                                    f"{written} != {info.file_size}"))
    return total


def verify_container(path: Path) -> str:
    """OOXML / zip：解包并解析 [Content_Types].xml。返回空串表示通过。"""
    try:
        with zipfile.ZipFile(path) as archive:
            broken = archive.testzip()
            if broken is not None:
                return f"zip 内条目损坏：{broken}"
            if "[Content_Types].xml" in archive.namelist():
                payload = archive.read("[Content_Types].xml")
                import xml.etree.ElementTree as element_tree
                element_tree.fromstring(payload)
            elif path.suffix.lower() in OOXML:
                return "缺少 [Content_Types].xml，不是有效的 Office 包"
        return ""
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        return f"{type(error).__name__}: {error}"


def verify_sqlite(path: Path) -> str:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        return f"{type(error).__name__}: {error}"
    messages = [row[0] for row in rows]
    if messages == ["ok"]:
        return ""
    return "；".join(str(message) for message in messages[:5])


def sample_files(manifest: list[tuple[str, int, str]], restored_root: Path, limit: int) -> list[Path]:
    """先每种已知类型取一个，再用其余文件补满。"""
    by_kind: dict[str, Path] = {}
    others: list[Path] = []
    for name, _size, _hash in sorted(manifest):
        path = restored_root / name
        suffix = path.suffix.lower()
        kind = "ooxml" if suffix in OOXML else "sqlite" if suffix in SQLITE else "zip" if suffix in ZIPS else ""
        if kind and kind not in by_kind:
            by_kind[kind] = path
        else:
            others.append(path)
    chosen = list(by_kind.values())
    for path in others:
        if len(chosen) >= limit:
            break
        chosen.append(path)
    return chosen[:limit]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--report", default=None)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    args = parser.parse_args()

    started = time.monotonic()
    source = Path(args.source)
    if not source.exists():
        print(f"找不到备份源：{source}", file=sys.stderr)
        return 2

    findings: list[dict] = []
    sampled_by_kind: dict[str, int] = {}
    stats = {
        "source": str(source),
        "kind": "unsupported",
        "files": 0,
        "bytes": 0,
        "restoredFiles": 0,
        "restoredBytes": 0,
        "mismatched": 0,
        "missing": 0,
        "extra": 0,
        "sampleRequested": args.sample,
        "sampleOpened": 0,
        "sampleSkipped": 0,
        "sampleFailed": 0,
        "sampledByKind": sampled_by_kind,
        "durationMs": 0,
        "tempRemoved": True,
    }
    verdict = "unknown"
    limit_hit = False
    temp_dir: Path | None = None

    try:
        if source.is_dir():
            stats["kind"] = "dir"
            source_files, total_bytes = manifest_dir(source, args.max_files, args.max_bytes, findings)
            restore_root = None
        elif zipfile.is_zipfile(source):
            stats["kind"] = "zip"
            with zipfile.ZipFile(source) as archive:
                source_files, total_bytes = manifest_zip(archive, args.max_files, args.max_bytes)
            restore_root = None
        else:
            if source.suffix.lower() in ZIPS:
                stats["kind"] = "zip"
                findings.append(finding("error", "CORRUPT_CONTAINER", str(source),
                                        "扩展名是压缩包，但内容不是有效的 zip，可能已损坏",
                                        "zipfile.is_zipfile 判定为假"))
            else:
                findings.append(finding("warn", "UNSUPPORTED_SOURCE", str(source),
                                        "既不是目录也不是 zip 包：本工具无法还原与核对"))
            source_files, total_bytes = [], 0
            restore_root = None
    except OverflowError as error:
        stats["kind"] = "dir" if source.is_dir() else "zip"
        findings.append(finding("warn", "LIMIT_EXCEEDED", str(source), f"超过验证上限，未完整核对：{error}"))
        source_files, total_bytes = [], 0
        restore_root = None
        limit_hit = True
    except (zipfile.BadZipFile, OSError) as error:
        findings.append(finding("error", "CORRUPT_CONTAINER", str(source),
                                "备份包无法读取", f"{type(error).__name__}: {error}"))
        source_files, total_bytes = [], 0
        restore_root = None

    stats["files"] = len(source_files)
    stats["bytes"] = total_bytes
    aggregate_hash = aggregate(source_files)

    if stats["kind"] != "unsupported" and source_files and not limit_hit:
        try:
            temp_dir = make_temp_dir()
            restored_root = temp_dir / "restore"
            restored_root.mkdir(parents=True, exist_ok=False)
            if stats["kind"] == "zip":
                try:
                    with zipfile.ZipFile(source) as archive:
                        restore_zip(archive, restored_root, args.max_bytes, findings)
                except UnsafeEntry as error:
                    findings.append(finding("error", "UNSAFE_ENTRY", str(error), "压缩包条目会逃出还原目录，已中止"))
            else:
                # copyfile 不复制权限：copy2 会把源文件的 ReadOnly 带进临时目录，导致删不掉。
                shutil.copytree(source, restored_root, dirs_exist_ok=True, symlinks=False,
                                copy_function=shutil.copyfile)

            restored: list[tuple[str, int, str]] = []
            restored_bytes = 0
            for path in sorted(p for p in restored_root.rglob("*") if p.is_file()):
                if path.is_symlink():
                    continue
                size = path.stat().st_size
                with path.open("rb") as handle:
                    restored.append((str(path.relative_to(restored_root)).replace("\\", "/"), size, sha256_stream(handle)))
                restored_bytes += size
            stats["restoredFiles"] = len(restored)
            stats["restoredBytes"] = restored_bytes

            original = {name: (size, file_hash) for name, size, file_hash in source_files}
            restored_map = {name: (size, file_hash) for name, size, file_hash in restored}
            for name in sorted(original):
                if name not in restored_map:
                    stats["missing"] += 1
                    findings.append(finding("error", "MISSING_AFTER_RESTORE", name, "还原后缺这个文件"))
                elif restored_map[name][1] != original[name][1]:
                    stats["mismatched"] += 1
                    findings.append(finding("error", "HASH_MISMATCH", name,
                                            "还原后哈希不同：备份不可无损还原",
                                            f"{original[name][1][:12]}… != {restored_map[name][1][:12]}…"))
            for name in sorted(restored_map):
                if name not in original:
                    stats["extra"] += 1
                    findings.append(finding("error", "EXTRA_AFTER_RESTORE", name, "还原后多出这个文件"))

            for path in sample_files(source_files, restored_root, args.sample):
                relative = str(path.relative_to(restored_root))
                if is_temp_file(relative):
                    stats["sampleSkipped"] += 1
                    continue
                suffix = path.suffix.lower()
                if path.stat().st_size == 0:
                    findings.append(finding("warn", "ZERO_BYTE", relative, "空文件（0 字节）"))
                    continue
                if suffix in OOXML or suffix in ZIPS:
                    problem = verify_container(path)
                    kind = "ooxml" if suffix in OOXML else "zip"
                    code = "CORRUPT_CONTAINER"
                elif suffix in SQLITE:
                    problem = verify_sqlite(path)
                    kind = "sqlite"
                    code = "CORRUPT_SQLITE"
                else:
                    continue
                stats["sampleOpened"] += 1
                sampled_by_kind[kind] = sampled_by_kind.get(kind, 0) + 1
                if problem:
                    stats["sampleFailed"] += 1
                    findings.append(finding("error", code, relative,
                                            "抽样打开失败", problem))
            if stats["sampleOpened"] == 0:
                findings.append(finding("warn", "NO_SAMPLE_OPENED", "-",
                                        "没有可抽样打开的文件（只认 docx/pptx/xlsx、sqlite、zip）"))

            if any(item["severity"] == "error" for item in findings):
                verdict = "fail"
            else:
                verdict = "pass"
        except OSError as error:
            findings.append(finding("error", "RESTORE_FAILED", str(source),
                                    "无法在临时目录还原备份，未能核对",
                                    f"{type(error).__name__}: {error}"))
            verdict = "unknown"
    elif stats["kind"] == "unsupported":
        verdict = "unknown"
    elif not source_files:
        if not any(item["code"] == "CORRUPT_CONTAINER" for item in findings):
            findings.append(finding("error", "EMPTY_BACKUP", str(source), "备份里没有任何文件"))
        verdict = "fail"
    else:
        verdict = "unknown"

    if temp_dir is not None:
        stats["tempRemoved"] = remove_tree(temp_dir)
        if not stats["tempRemoved"]:
            findings.append(finding("warn", "TEMP_NOT_REMOVED", str(temp_dir),
                                    "临时还原目录未能清理，已保留现场",
                                    "权限或占用导致；可人工删除"))

    stats["durationMs"] = int((time.monotonic() - started) * 1000)
    report_path = ""
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        target.write_text(render_report(stats, findings, verdict), encoding="utf-8")
        report_path = str(target)

    payload = {
        "tool": "backup_verify",
        "target": str(source),
        "hash": aggregate_hash,
        "verdict": verdict,
        "findings": findings[: common.MAX_FINDINGS],
        "findingsTotal": len(findings),
        "stats": stats,
        "reportPath": report_path,
        "receipt": common.receipt(),
    }
    return common.emit(payload)


def render_report(stats: dict, findings: list[dict], verdict: str) -> str:
    lines = [
        f"# 备份验证报告：{Path(stats['source']).name}",
        "",
        f"- 备份源：`{stats['source']}`（{stats['kind']}）",
        f"- 结论：**{verdict}**　耗时 {stats['durationMs']} ms",
        "",
        "## 一、清单与还原",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 原文件数 | {stats['files']} |",
        f"| 原字节数 | {stats['bytes']} |",
        f"| 还原文件数 | {stats['restoredFiles']} |",
        f"| 还原字节数 | {stats['restoredBytes']} |",
        f"| 哈希不符 | {stats['mismatched']} |",
        f"| 还原缺失 | {stats['missing']} |",
        f"| 还原多出 | {stats['extra']} |",
        f"| 抽样 | 请求 {stats['sampleRequested']} · 成功 {stats['sampleOpened']}"
        f" · 跳过（临时文件）{stats['sampleSkipped']} · 失败 {stats['sampleFailed']} |",
        f"| 临时目录 | {'已清理' if stats['tempRemoved'] else '保留现场（见发现项）'} |",
        "",
        "## 二、按类型抽样",
        "",
    ]
    if stats["sampledByKind"]:
        lines.extend(["| 类型 | 打开数 |", "|---|---:|"])
        for kind, count in sorted(stats["sampledByKind"].items()):
            lines.append(f"| {kind} | {count} |")
    else:
        lines.append("无（没有可抽样打开的文件）。")

    lines.extend(["", "## 三、发现项", ""])
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
        "> 本工具证明的是「这份备份能被完整还原」，不是「它等于某台机器的原始数据」——后者本工具看不到。",
        "",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
