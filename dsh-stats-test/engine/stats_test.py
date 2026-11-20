# -*- coding: utf-8 -*-
"""stats_test 引擎：数据 → 描述统计 + 检验 + 效应量 + 前提检查 → 统一 evidence JSON。

范围（批次 2）：ttest（Welch 双样本）/ anova（单因素）/ chi2（列联表）
              / mannwhitney / kruskal。不做配对 t、不做多重比较校正。
p 值由 stats_math 自算（本机无 scipy / statsmodels）。
**不做 Shapiro-Wilk**：正态性只报组内偏度作粗提示，assumptions 里明写 status="na"，
绝不冒充"正态性检验通过"。

用法：
    python stats_test.py --data data.csv --test ttest --value y --group g
    python stats_test.py --data data.csv --test chi2 --group a --group2 b [--report 报告.md]
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import statistics
import sys
from pathlib import Path

sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import stats_math  # noqa: E402

TESTS = ("ttest", "anova", "chi2", "mannwhitney", "kruskal")
UNKNOWN_CODES = {"SMALL_GROUP", "SKEWED_GROUP", "UNEVEN_VARIANCE", "EXPECTED_COUNT_LOW"}


def finding(severity: str, code: str, where: str, detail: str, evidence: str = "") -> dict:
    return {"severity": severity, "code": code, "where": where, "detail": detail, "evidence": evidence}


def read_table(path: Path) -> tuple[list[dict], list[str]]:
    """读 CSV：先 utf-8-sig，再 gb18030；首行有制表符且无逗号时按 TSV 读。"""
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise SystemExit(f"无法解码 CSV：{path}（试过 utf-8-sig / gb18030）")
    lines = text.splitlines()
    first = lines[0] if lines else ""
    delimiter = "\t" if ("\t" in first and "," not in first) else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader]
    return rows, list(reader.fieldnames or [])


def describe(values: list[float]) -> dict:
    n = len(values)
    mean = sum(values) / n
    sd = 0.0
    if n > 1:
        sd = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1))
    item = {"n": n, "mean": mean, "sd": sd, "median": statistics.median(values)}
    if n >= 3 and sd > 0:
        m2 = sum((x - mean) ** 2 for x in values) / n
        m3 = sum((x - mean) ** 3 for x in values) / n
        if m2 > 0:
            item["skew"] = m3 / m2 ** 1.5
    return item


def ranked(values: list[float]) -> tuple[list[float], float]:
    """平均秩 + 结校正量 Σ(t³-t)。"""
    n = len(values)
    order = sorted(range(n), key=lambda index: values[index])
    ranks = [0.0] * n
    tie_sum = 0.0
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for index in range(i, j + 1):
            ranks[order[index]] = average
        size = j - i + 1
        tie_sum += size ** 3 - size
        i = j + 1
    return ranks, tie_sum


def levene_median(groups: list[list[float]]) -> tuple[float, float] | None:
    """Brown–Forsythe（以中位数为中心）方差齐性检验。"""
    shifted = [[abs(x - statistics.median(g)) for x in g] for g in groups]
    k = len(shifted)
    total = sum(len(g) for g in shifted)
    if k < 2 or total - k <= 0:
        return None
    grand = sum(sum(g) for g in shifted) / total
    numerator = sum(len(g) * (sum(g) / len(g) - grand) ** 2 for g in shifted)
    denominator = sum(sum((x - sum(g) / len(g)) ** 2 for x in g) for g in shifted)
    if denominator <= 0:
        return None
    df1, df2 = k - 1, total - k
    f_value = (numerator / df1) / (denominator / df2)
    return f_value, stats_math.f_upper_p(f_value, df1, df2)


def cohens_d(a: list[float], b: list[float]) -> float:
    n1, n2 = len(a), len(b)
    m1, m2 = sum(a) / n1, sum(b) / n2
    if n1 + n2 <= 2:
        return 0.0
    pooled = ((n1 - 1) * statistics.variance(a) if n1 > 1 else 0.0) \
        + ((n2 - 1) * statistics.variance(b) if n2 > 1 else 0.0)
    pooled = math.sqrt(pooled / (n1 + n2 - 2))
    if pooled == 0:
        return 0.0
    return (m1 - m2) / pooled


def welch_ttest(a: list[float], b: list[float]) -> tuple[float, float, float]:
    n1, n2 = len(a), len(b)
    m1, m2 = sum(a) / n1, sum(b) / n2
    v1 = statistics.variance(a) if n1 > 1 else 0.0
    v2 = statistics.variance(b) if n2 > 1 else 0.0
    denom = math.sqrt(v1 / n1 + v2 / n2)
    if denom == 0:
        raise ValueError("两组的方差都为 0，Welch t 无定义")
    t_value = (m1 - m2) / denom
    numerator = (v1 / n1 + v2 / n2) ** 2
    denominator = ((v1 / n1) ** 2) / (n1 - 1) + ((v2 / n2) ** 2) / (n2 - 1)
    df = numerator / denominator
    return t_value, df, stats_math.t_two_sided_p(t_value, df)


def one_way_anova(groups: list[list[float]]) -> tuple[float, float, float, float, float]:
    all_values = [x for group in groups for x in group]
    total = len(all_values)
    grand = sum(all_values) / total
    ss_between = sum(len(g) * (sum(g) / len(g) - grand) ** 2 for g in groups)
    ss_within = sum(sum((x - sum(g) / len(g)) ** 2 for x in g) for g in groups)
    df1, df2 = len(groups) - 1, total - len(groups)
    if df2 <= 0 or ss_within <= 0:
        raise ValueError("组内平方和为 0（各组取值恒定），F 无定义")
    f_value = (ss_between / df1) / (ss_within / df2)
    ss_total = ss_between + ss_within
    eta2 = ss_between / ss_total if ss_total > 0 else 0.0
    return f_value, df1, df2, stats_math.f_upper_p(f_value, df1, df2), eta2  # type: ignore[return-value]


def mann_whitney(a: list[float], b: list[float]) -> tuple[float, float, float, float]:
    combined = list(a) + list(b)
    ranks, tie_sum = ranked(combined)
    n1, n2 = len(a), len(b)
    total = n1 + n2
    rank_sum = sum(ranks[:n1])
    u1 = rank_sum - n1 * (n1 + 1) / 2.0
    mean_u = n1 * n2 / 2.0
    variance_u = n1 * n2 / 12.0 * ((total + 1) - tie_sum / (total * (total - 1)))
    if variance_u <= 0:
        raise ValueError("U 的方差为 0（全部取值相同），无法做正态近似")
    diff = u1 - mean_u
    correction = 0.5 if diff > 0 else (-0.5 if diff < 0 else 0.0)
    z_value = (diff - correction) / math.sqrt(variance_u)
    p_value = stats_math.normal_two_sided_p(z_value)
    rank_biserial = 2.0 * u1 / (n1 * n2) - 1.0
    return u1, z_value, p_value, rank_biserial


def kruskal_wallis(groups: list[list[float]]) -> tuple[float, float, float]:
    combined = [x for group in groups for x in group]
    ranks, tie_sum = ranked(combined)
    total = len(combined)
    h_value = 12.0 / (total * (total + 1)) * sum(
        sum(ranks[sum(len(g) for g in groups[:i]):sum(len(g) for g in groups[:i + 1])]) ** 2 / len(g)
        for i, g in enumerate(groups)
    ) - 3.0 * (total + 1)
    correction = 1.0 - tie_sum / (total ** 3 - total)
    if correction > 0:
        h_value /= correction
    k = len(groups)
    epsilon2 = (h_value - k + 1) / (total - k) if total > k else 0.0
    return h_value, stats_math.chi2_upper_p(h_value, k - 1), epsilon2


def chi_square(rows: list[str], cols: list[str]) -> tuple[float, int, float, float, list[list[float]]]:
    row_names = sorted(set(rows))
    col_names = sorted(set(cols))
    table = [[0] * len(col_names) for _ in row_names]
    for row_value, col_value in zip(rows, cols):
        table[row_names.index(row_value)][col_names.index(col_value)] += 1
    row_totals = [sum(row) for row in table]
    col_totals = [sum(table[i][j] for i in range(len(table))) for j in range(len(col_names))]
    total = sum(row_totals)
    expected = [[row_totals[i] * col_totals[j] / total for j in range(len(col_names))] for i in range(len(table))]
    chi2_value = 0.0
    for i in range(len(table)):
        for j in range(len(col_names)):
            if expected[i][j] > 0:
                chi2_value += (table[i][j] - expected[i][j]) ** 2 / expected[i][j]
    df = (len(row_names) - 1) * (len(col_names) - 1)
    p_value = stats_math.chi2_upper_p(chi2_value, df)
    cramers_v = math.sqrt(chi2_value / (total * min(len(row_names) - 1, len(col_names) - 1)))
    return chi2_value, df, p_value, cramers_v, expected


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--test", required=True, choices=TESTS)
    parser.add_argument("--value", default=None)
    parser.add_argument("--group", required=True)
    parser.add_argument("--group2", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    data_path = Path(args.data)
    if not data_path.is_file():
        print(f"找不到数据文件：{data_path}", file=sys.stderr)
        return 2

    rows, columns = read_table(data_path)
    missing = [column for column in (args.value, args.group, args.group2) if column and column not in columns]
    if missing:
        print(f"列不存在：{', '.join(missing)}；可用列：{', '.join(columns)}", file=sys.stderr)
        return 2

    findings: list[dict] = []
    assumptions: list[dict] = []
    total_rows = len(rows)

    if args.test == "chi2":
        if not args.group2:
            print("chi2 需要 --group2（第二个分类变量）", file=sys.stderr)
            return 2
        pairs = []
        dropped_group = 0
        for row in rows:
            left = (row.get(args.group) or "").strip()
            right = (row.get(args.group2) or "").strip()
            if left == "" or right == "":
                dropped_group += 1
                continue
            pairs.append((left, right))
        if dropped_group:
            findings.append(finding("warn", "MISSING_GROUP_DROPPED", "-", f"{dropped_group} 行缺分类标签，已剔除"))
        groups_payload = [{"name": name, "n": sum(1 for pair in pairs if pair[0] == name)}
                          for name in sorted({pair[0] for pair in pairs})]
        used = len(pairs)
        result = None
        if len({pair[0] for pair in pairs}) < 2 or len({pair[1] for pair in pairs}) < 2:
            findings.append(finding("error", "DEGENERATE_TABLE", "-", "列联表有一维只有 1 个类别，卡方无定义"))
        else:
            chi2_value, df, p_value, cramers_v, expected = chi_square(
                [pair[0] for pair in pairs], [pair[1] for pair in pairs]
            )
            flat_expected = [cell for row_cells in expected for cell in row_cells]
            small = sum(1 for cell in flat_expected if cell < 5)
            too_small = sum(1 for cell in flat_expected if cell < 1)
            min_expected = min(flat_expected)
            ratio = small / len(flat_expected)
            assumptions.append({
                "name": "期望频数",
                "status": "ok" if (too_small == 0 and ratio <= 0.2) else "warn",
                "detail": f"最小期望 {min_expected:.2f}；期望 <5 的格 {small}/{len(flat_expected)}",
            })
            if too_small:
                findings.append(finding("error", "EXPECTED_COUNT_TOO_LOW", "-",
                                        f"{too_small} 个格期望频数 <1，卡方近似不可用",
                                        f"最小期望 {min_expected:.2f}"))
            elif ratio > 0.2:
                findings.append(finding("warn", "EXPECTED_COUNT_LOW", "-",
                                        f"{small}/{len(flat_expected)} 个格期望频数 <5，卡方近似偏乐观",
                                        f"最小期望 {min_expected:.2f}"))
            result = {
                "method": "Pearson chi-square test of independence",
                "statistic": chi2_value,
                "df": str(df),
                "p": p_value,
                "effectName": "cramers_v",
                "effectValue": cramers_v,
            }
        stats_payload = {
            "data": str(data_path),
            "test": args.test,
            "value": "",
            "group": args.group,
            "group2": args.group2,
            "rows": total_rows,
            "used": used,
            "dropped": dropped_group,
            "groups": groups_payload,
            "assumptions": assumptions,
        }
    else:
        if not args.value:
            print(f"{args.test} 需要 --value（数值列）", file=sys.stderr)
            return 2
        grouped: dict[str, list[float]] = {}
        dropped_numeric = 0
        dropped_group = 0
        for row in rows:
            label = (row.get(args.group) or "").strip()
            if label == "":
                dropped_group += 1
                continue
            try:
                number = float((row.get(args.value) or "").strip())
            except (TypeError, ValueError):
                dropped_numeric += 1
                continue
            grouped.setdefault(label, []).append(number)
        if dropped_numeric:
            findings.append(finding("warn", "NON_NUMERIC_DROPPED", "-",
                                    f"{dropped_numeric} 行的 --value 不是数字，已剔除"))
        if dropped_group:
            findings.append(finding("warn", "MISSING_GROUP_DROPPED", "-", f"{dropped_group} 行缺分类标签，已剔除"))

        names = sorted(grouped)
        groups = [grouped[name] for name in names]
        used = sum(len(group) for group in groups)
        groups_payload = []
        for name, group in zip(names, groups):
            described = {"name": name}
            described.update(describe(group))
            groups_payload.append(described)

        too_small = [name for name, group in zip(names, groups) if len(group) < 5]
        if too_small:
            findings.append(finding("warn", "SMALL_GROUP", ", ".join(too_small),
                                    "有组样本量 <5，检验结果不稳定",
                                    "；".join(f"{name}: n={len(grouped[name])}" for name in too_small)))
        skewed = [
            (name, group) for name, group in zip(names, groups)
            if len(group) >= 3 and len(group) < 30
            and "skew" in describe(group) and abs(describe(group)["skew"]) > 1.0
        ]
        for name, group in skewed:
            findings.append(finding("warn", "SKEWED_GROUP", name,
                                    "组内偏度 |g1|>1 且 n<30：均值类检验可能不可靠（未做正态性检验）",
                                    f"skew={describe(group)['skew']:.2f}, n={len(group)}"))
        assumptions.append({
            "name": "正态性",
            "status": "na",
            "detail": "本机无 scipy，未做 Shapiro-Wilk；仅报组内偏度作粗提示",
        })
        if len(groups) >= 2:
            variance_check = levene_median(groups)
            if variance_check is None:
                assumptions.append({"name": "方差齐性", "status": "na", "detail": "组内中位绝对偏差全为 0，无法检验"})
            else:
                f_value, p_value = variance_check
                if args.test == "ttest":
                    assumptions.append({
                        "name": "方差齐性",
                        "status": "na",
                        "detail": f"Levene p={p_value:.3f}；Welch 校正不依赖方差齐性",
                    })
                elif args.test == "anova":
                    status = "ok" if p_value >= 0.05 else "warn"
                    assumptions.append({
                        "name": "方差齐性",
                        "status": status,
                        "detail": f"Levene(Brown–Forsythe) F={f_value:.3f}, p={p_value:.3f}",
                    })
                    if p_value < 0.05:
                        findings.append(finding("warn", "UNEVEN_VARIANCE", "-",
                                                "方差不齐（Levene p<0.05）：单因素 ANOVA 的前提不满足",
                                                f"p={p_value:.4f}"))
                else:
                    assumptions.append({
                        "name": "方差齐性",
                        "status": "na",
                        "detail": "秩检验不依赖方差齐性",
                    })

        result = None
        if args.test in ("ttest", "mannwhitney"):
            if len(groups) != 2:
                findings.append(finding("error", "NOT_ENOUGH_GROUPS", "-",
                                        f"{args.test} 需要恰好 2 组，实际 {len(groups)} 组：{', '.join(names)}"))
            elif any(len(group) < 2 for group in groups):
                findings.append(finding("error", "NOT_ENOUGH_OBSERVATIONS", "-",
                                        "每组至少需要 2 个观测",
                                        "；".join(f"{name}: n={len(grouped[name])}" for name in names)))
            elif args.test == "ttest":
                try:
                    t_value, df, p_value = welch_ttest(groups[0], groups[1])
                    result = {
                        "method": "Welch two-sample t-test",
                        "statistic": t_value,
                        "df": f"{df:.2f}",
                        "p": p_value,
                        "effectName": "cohens_d",
                        "effectValue": cohens_d(groups[0], groups[1]),
                    }
                except ValueError as error:
                    findings.append(finding("error", "CONSTANT_VALUES", "-", str(error)))
            else:
                try:
                    u_value, z_value, p_value, rank_biserial = mann_whitney(groups[0], groups[1])
                    result = {
                        "method": "Mann-Whitney U (normal approximation, tie-corrected)",
                        "statistic": u_value,
                        "df": "normal approx",
                        "p": p_value,
                        "effectName": "rank_biserial",
                        "effectValue": rank_biserial,
                    }
                    combined = groups[0] + groups[1]
                    _, tie_sum = ranked(combined)
                    if tie_sum > 0:
                        findings.append(finding("warn", "TIES_PRESENT", "-",
                                                "存在结（相同取值），已用结校正计算方差",
                                                f"Σ(t³-t)={tie_sum:.0f}"))
                        assumptions.append({
                            "name": "结的处理",
                            "status": "ok",
                            "detail": "结校正已应用（正态近似）",
                        })
                except ValueError as error:
                    findings.append(finding("error", "CONSTANT_VALUES", "-", str(error)))
        else:
            if len(groups) < 2:
                findings.append(finding("error", "NOT_ENOUGH_GROUPS", "-",
                                        f"{args.test} 至少需要 2 组，实际 {len(groups)} 组"))
            elif args.test == "anova":
                try:
                    f_value, df1, df2, p_value, eta2 = one_way_anova(groups)
                    result = {
                        "method": "One-way ANOVA",
                        "statistic": f_value,
                        "df": f"{df1}, {df2}",
                        "p": p_value,
                        "effectName": "eta_squared",
                        "effectValue": eta2,
                    }
                except ValueError as error:
                    findings.append(finding("error", "CONSTANT_VALUES", "-", str(error)))
            else:
                try:
                    h_value, p_value, epsilon2 = kruskal_wallis(groups)
                    result = {
                        "method": "Kruskal-Wallis H (tie-corrected)",
                        "statistic": h_value,
                        "df": str(len(groups) - 1),
                        "p": p_value,
                        "effectName": "epsilon_squared",
                        "effectValue": epsilon2,
                    }
                    combined = [x for group in groups for x in group]
                    _, tie_sum = ranked(combined)
                    if tie_sum > 0:
                        findings.append(finding("warn", "TIES_PRESENT", "-",
                                                "存在结（相同取值），已做结校正", f"Σ(t³-t)={tie_sum:.0f}"))
                except ValueError as error:
                    findings.append(finding("error", "CONSTANT_VALUES", "-", str(error)))

        stats_payload = {
            "data": str(data_path),
            "test": args.test,
            "value": args.value,
            "group": args.group,
            "group2": "",
            "rows": total_rows,
            "used": used,
            "dropped": dropped_numeric + dropped_group,
            "groups": groups_payload,
            "assumptions": assumptions,
        }

    if result is not None:
        stats_payload["result"] = result

    has_error = any(item["severity"] == "error" for item in findings)
    if has_error:
        verdict = "fail"
    elif any(item["code"] in UNKNOWN_CODES for item in findings):
        verdict = "unknown"
    else:
        verdict = "pass"

    report_path = ""
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        target.write_text(render_report(args.test, stats_payload, findings, verdict), encoding="utf-8")
        report_path = str(target)

    payload = {
        "tool": "stats_test",
        "target": str(data_path),
        "hash": common.sha256_file(data_path),
        "verdict": verdict,
        "findings": findings[: common.MAX_FINDINGS],
        "findingsTotal": len(findings),
        "stats": stats_payload,
        "reportPath": report_path,
        "receipt": common.receipt(),
    }
    return common.emit(payload)


def render_report(test: str, stats_payload: dict, findings: list[dict], verdict: str) -> str:
    lines = [
        f"# 统计检验报告：{Path(stats_payload['data']).name}",
        "",
        f"- 检验：**{test}**　结论：**{verdict}**",
        f"- 数据：`{stats_payload['data']}`（共 {stats_payload['rows']} 行，用 {stats_payload['used']} 行，剔除 {stats_payload['dropped']} 行）",
        "",
        "## 一、描述统计",
        "",
        "| 组 | n | 均值 | 标准差 | 中位数 | 偏度 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for group in stats_payload["groups"]:
        mean = f"{group['mean']:.4f}" if "mean" in group else "—"
        sd = f"{group['sd']:.4f}" if "sd" in group else "—"
        median = f"{group['median']:.4f}" if "median" in group else "—"
        skew = f"{group['skew']:.3f}" if "skew" in group else "—"
        lines.append(f"| {group['name']} | {group['n']} | {mean} | {sd} | {median} | {skew} |")

    result = stats_payload.get("result")
    lines.extend(["", "## 二、检验结果", ""])
    if result is None:
        lines.append("检验未执行（见发现项）。")
    else:
        lines.extend([
            f"- 方法：{result['method']}",
            f"- 统计量：**{result['statistic']:.6f}**　df={result['df']}　p=**{result['p']:.6g}**",
            f"- 效应量：{result['effectName']} = **{result['effectValue']:.4f}**",
        ])

    lines.extend(["", "## 三、前提检查", "", "| 项 | 状态 | 说明 |", "|---|---|---|"])
    for assumption in stats_payload["assumptions"]:
        lines.append(f"| {assumption['name']} | {assumption['status']} | {assumption['detail']} |")

    lines.extend(["", "## 四、发现项", ""])
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
        "> p 值由本机自实现的不完全 beta / gamma 函数计算（无 scipy），已用已知值核对。"
        "正态性未检验（无 Shapiro-Wilk）；偏度只是粗提示，不是正态性判据。",
        "",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
