# -*- coding: utf-8 -*-
"""自实现的分布尾函数（本机无 scipy / statsmodels）。

只用标准库 math：不完全 beta（t / F 尾）与不完全 gamma（卡方尾），
算法取自 Numerical Recipes 的 betacf / gser / gcf，容差 3e-14。
**改这里要用已知值回归**（见 stats_test 的自测脚本与开工单记录）：
  t(2.0, df=10) 双侧 = 0.0733880348…
  F(4.0, 2, 10) 上尾   = 0.0529221494…（df1=2 有闭式 (df2/(df2+2F))^(df2/2)，已逐位核对）
  chi2(3.841458820694124, df=1) 上尾 = 0.05
  chi2(5.991464547107979, df=2) 上尾 = 0.05
  恒等式 F(t², 1, df) 上尾 ≡ t(t, df) 双侧（回归时必须保持）
"""
from __future__ import annotations

import math

EPS = 3e-14
FPMIN = 1e-300
MAXIT = 500


def _betacf(a: float, b: float, x: float) -> float:
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    """正则化不完全 beta I_x(a,b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _gser(a: float, x: float) -> float:
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(MAXIT):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * EPS:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gcf(a: float, x: float) -> float:
    b = x + 1.0 - a
    c = 1.0 / FPMIN
    d = 1.0 / b
    h = d
    for i in range(1, MAXIT + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < FPMIN:
            d = FPMIN
        c = b + an / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def gammaincc(a: float, x: float) -> float:
    """正则化上不完全 gamma Q(a,x)。"""
    if a <= 0.0:
        raise ValueError("a must be positive")
    if x < 0.0:
        raise ValueError("x must be non-negative")
    if x == 0.0:
        return 1.0
    if x < a + 1.0:
        return 1.0 - _gser(a, x)
    return _gcf(a, x)


def t_two_sided_p(t: float, df: float) -> float:
    """Student t 双侧 p。"""
    if df <= 0:
        raise ValueError("df must be positive")
    return betai(df / 2.0, 0.5, df / (df + t * t))


def f_upper_p(f: float, df1: float, df2: float) -> float:
    """F 分布上尾 p。"""
    if df1 <= 0 or df2 <= 0:
        raise ValueError("df must be positive")
    if f <= 0:
        return 1.0
    return betai(df2 / 2.0, df1 / 2.0, df2 / (df2 + df1 * f))


def chi2_upper_p(x: float, df: float) -> float:
    """卡方上尾 p。"""
    if df <= 0:
        raise ValueError("df must be positive")
    if x <= 0:
        return 1.0
    return gammaincc(df / 2.0, x / 2.0)


def normal_two_sided_p(z: float) -> float:
    """标准正态双侧 p。"""
    return math.erfc(abs(z) / math.sqrt(2.0))
