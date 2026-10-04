"""偏航漂移斜率：沿「办结时刻」串点，对窗内每个机组做最小二乘直线拟合。

口径（与专页下区只读说明一致）：

- 样本只取 ``yaw_logs.status = 'done'`` 且有 ``processed_at`` 的记录；pending /
  未办结的点一律不入样本。
- 时间轴取办结时刻 ``processed_at``，按机组分组、组内按办结时刻升序串点。
- 窗为 ``[当前时刻 - 窗宽, 当前时刻]``；窗宽 0 秒（空窗）时所有点都在窗外。
- 斜率单位为 度/小时；样本少于 2 点、或窗内所有点办结时刻相同（分母为 0）时
  不出斜率（空）。
- 斜率只能由本模块在后台拟合产出并整表重建，接口不接受任何手工填写的斜率。
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg.types.json import Json

DEFAULT_WINDOW_SECONDS = 24 * 3600
MIN_WINDOW_SECONDS = 0
MAX_WINDOW_SECONDS = 7 * 24 * 3600

DRIFT_SCHEMA = """
CREATE TABLE IF NOT EXISTS drift_settings (
    id integer PRIMARY KEY DEFAULT 1,
    window_seconds bigint NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    fitted_at timestamptz NOT NULL,
    CONSTRAINT drift_settings_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS drift_slopes (
    id serial PRIMARY KEY,
    turbine_code text NOT NULL UNIQUE,
    slope_deg_per_hour double precision,
    n_samples integer NOT NULL,
    samples jsonb NOT NULL DEFAULT '[]'::jsonb,
    window_seconds bigint NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    fitted_at timestamptz NOT NULL
);
"""


def fit_slope(points: list[tuple[datetime, float]]) -> Optional[dict]:
    """对 (办结时刻, 偏航误差°) 序列做 OLS 线性拟合，x 单位为小时。

    样本不足 2 点，或所有点办结时刻相同导致分母为 0 时返回 None（空）。
    """
    n = len(points)
    if n < 2:
        return None
    t0 = min(t for t, _ in points)
    xs = [(t - t0).total_seconds() / 3600.0 for t, _ in points]
    ys = [float(y) for _, y in points]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    sxx = sum((x - x_mean) ** 2 for x in xs)
    if sxx == 0.0:
        return None
    sxy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    slope = sxy / sxx
    # -0.0 统一成 0.0，便于前端判「平」
    if slope == 0.0:
        slope = 0.0
    return {"slope": slope, "intercept": y_mean - slope * x_mean, "n": n}


def refit_all(conn, window_seconds: int, now: Optional[datetime] = None) -> dict:
    """在当前事务内按给定窗宽重建全部机组斜率并落库，返回快照字典。

    机组全集来自「历史上有过办结记录」的机组；某机组窗内一个点都没有时
    （窗缩到点都在窗外）仍保留一行 n_samples=0、斜率为 NULL（空）。
    """
    window_seconds = int(window_seconds)
    if not (MIN_WINDOW_SECONDS <= window_seconds <= MAX_WINDOW_SECONDS):
        raise ValueError("window_seconds out of range")

    now = now or datetime.now(timezone.utc)
    start = now - timedelta(seconds=window_seconds)

    turbine_rows = conn.execute(
        """SELECT DISTINCT turbine_code
           FROM yaw_logs
           WHERE status = 'done'
           ORDER BY turbine_code"""
    ).fetchall()
    codes = [r["turbine_code"] for r in turbine_rows]

    point_rows = conn.execute(
        """SELECT id, turbine_code, yaw_err_deg, processed_at
           FROM yaw_logs
           WHERE status = 'done'
             AND processed_at IS NOT NULL
             AND processed_at >= %s
             AND processed_at <= %s
           ORDER BY turbine_code, processed_at, id""",
        (start, now),
    ).fetchall()

    grouped: dict[str, list] = {code: [] for code in codes}
    for r in point_rows:
        grouped.setdefault(r["turbine_code"], []).append(
            (r["processed_at"], float(r["yaw_err_deg"]), r["id"])
        )

    payload_rows = []
    for code in codes:
        series = grouped.get(code, [])
        fit = fit_slope([(t, y) for t, y, _ in series])
        # upsert：worker 办结自动重拟合与技师手动重拟合可能并发，
        # ON CONFLICT 避免 DELETE+INSERT 在机组唯一键上相撞。
        conn.execute(
            """INSERT INTO drift_slopes
               (turbine_code, slope_deg_per_hour, n_samples, samples,
                window_seconds, window_start, window_end, fitted_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (turbine_code) DO UPDATE
               SET slope_deg_per_hour = EXCLUDED.slope_deg_per_hour,
                   n_samples = EXCLUDED.n_samples,
                   samples = EXCLUDED.samples,
                   window_seconds = EXCLUDED.window_seconds,
                   window_start = EXCLUDED.window_start,
                   window_end = EXCLUDED.window_end,
                   fitted_at = EXCLUDED.fitted_at""",
            (
                code,
                fit["slope"] if fit else None,
                len(series),
                Json(
                    [
                        {
                            "log_id": log_id,
                            "processed_at": t.isoformat(),
                            "yaw_err_deg": y,
                        }
                        for t, y, log_id in series
                    ]
                ),
                window_seconds,
                start,
                now,
                now,
            ),
        )
        payload_rows.append(
            {
                "turbine_code": code,
                "slope_deg_per_hour": fit["slope"] if fit else None,
                "n_samples": len(series),
                "samples": [
                    {
                        "log_id": log_id,
                        "processed_at": t.isoformat(),
                        "yaw_err_deg": y,
                    }
                    for t, y, log_id in series
                ],
                "window_seconds": window_seconds,
                "window_start": start.isoformat(),
                "window_end": now.isoformat(),
                "fitted_at": now.isoformat(),
            }
        )

    # 机组集合只增不减（yaw_logs 从不删除），故用 upsert 即可，无需删除旧机组；
    # 不在此处做 DELETE，以免与并发的「办结即拟合」事务互相误删新机组行。

    conn.execute(
        """INSERT INTO drift_settings (id, window_seconds, window_start, window_end, fitted_at)
           VALUES (1, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE
           SET window_seconds = EXCLUDED.window_seconds,
               window_start = EXCLUDED.window_start,
               window_end = EXCLUDED.window_end,
               fitted_at = EXCLUDED.fitted_at""",
        (window_seconds, start, now, now),
    )

    return {
        "window_seconds": window_seconds,
        "window_start": start.isoformat(),
        "window_end": now.isoformat(),
        "fitted_at": now.isoformat(),
        "slopes": payload_rows,
    }


def current_window_seconds(conn) -> int:
    """读取已保存窗宽；尚未拟合过时返回默认窗宽（不落库）。"""
    row = conn.execute(
        "SELECT window_seconds FROM drift_settings WHERE id = 1"
    ).fetchone()
    return int(row["window_seconds"]) if row else DEFAULT_WINDOW_SECONDS


def snapshot(conn) -> Optional[dict]:
    """读取最近一次后台拟合的结果快照；从未拟合过返回 None。"""
    setting = conn.execute(
        """SELECT window_seconds, window_start, window_end, fitted_at
           FROM drift_settings WHERE id = 1"""
    ).fetchone()
    if setting is None:
        return None
    rows = conn.execute(
        """SELECT turbine_code, slope_deg_per_hour, n_samples, samples,
                  window_seconds, window_start, window_end, fitted_at
           FROM drift_slopes
           ORDER BY turbine_code"""
    ).fetchall()
    return {
        "window_seconds": int(setting["window_seconds"]),
        "window_start": setting["window_start"].isoformat(),
        "window_end": setting["window_end"].isoformat(),
        "fitted_at": setting["fitted_at"].isoformat(),
        "slopes": [
            {
                "turbine_code": r["turbine_code"],
                "slope_deg_per_hour": r["slope_deg_per_hour"],
                "n_samples": r["n_samples"],
                "samples": r["samples"],
                "window_seconds": int(r["window_seconds"]),
                "window_start": r["window_start"].isoformat(),
                "window_end": r["window_end"].isoformat(),
                "fitted_at": r["fitted_at"].isoformat(),
            }
            for r in rows
        ],
    }
