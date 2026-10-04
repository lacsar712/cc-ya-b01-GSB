"""偏航漂移斜率拟合口径。

链路：办结时刻串点 → 最小二乘拟合 → 专页逐机列出。
- 仅 status='done' 且 processed_at 不为空的记录可入样本，待处理记录不入样本；
- 样本须落在统计窗内（processed_at >= 窗口起点）；
- 横轴为办结时刻，纵轴为偏航误差（度），斜率单位为度/小时；
- 样本不足 2 点或横轴无跨度时返回 None（专页显示为空）。
"""

from datetime import datetime


def fit_slope_deg_per_hour(points: list[tuple[datetime, float]]) -> float | None:
    """对 (办结时刻, 偏航误差) 序列做最小二乘直线拟合，返回斜率（度/小时）。"""
    pts = sorted(points, key=lambda p: p[0])
    n = len(pts)
    if n < 2:
        return None
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 3600.0 for t, _ in pts]
    ys = [float(y) for _, y in pts]
    x_bar = sum(xs) / n
    y_bar = sum(ys) / n
    sxx = sum((x - x_bar) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys))
    return sxy / sxx
