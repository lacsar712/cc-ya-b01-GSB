import asyncio
import os
from datetime import datetime, timedelta, timezone
from functools import wraps

from jose import JWTError, jwt
from passlib.context import CryptContext
from quart import Quart, jsonify, request

from db import SCHEMA, connect
from drift import fit_slope_deg_per_hour
from rules import judge

SECRET = os.environ.get("JWT_SECRET", "yaw-align-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

USERS = {
    "technician": {
        "role": "writer",
        "password_hash": pwd.hash("tech123456"),
    },
    "observer": {
        "role": "reader",
        "password_hash": pwd.hash("obs123456"),
    },
}

app = Quart(__name__)


def _run_db(fn, *args, **kwargs):
    return fn(*args, **kwargs)


async def run_db(fn, *args, **kwargs):
    return await asyncio.to_thread(_run_db, fn, *args, **kwargs)


def seed_if_empty(conn):
    conn.execute(SCHEMA)
    count = conn.execute("SELECT COUNT(*) AS n FROM yaw_logs").fetchone()["n"]
    if count > 0:
        return
    now = datetime.now(timezone.utc)
    samples = [
        ("W01", 0.4, "合格"),
        ("W07", 3.2, "偏航超差"),
    ]
    for code, err, expected_verdict in samples:
        verdict, reason = judge(err)
        assert verdict == expected_verdict
        conn.execute(
            """INSERT INTO yaw_logs
               (turbine_code, yaw_err_deg, status, verdict, reason,
                created_by, created_at, processed_at)
               VALUES (%s, %s, 'done', %s, %s, %s, %s, %s)""",
            (code, err, verdict, reason, "technician", now, now),
        )


@app.before_serving
async def startup():
    def init():
        with connect() as conn:
            seed_if_empty(conn)
            conn.commit()

    await run_db(init)


def parse_bearer():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


async def current_user():
    token = parse_bearer()
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    except JWTError:
        return None
    sub = payload.get("sub")
    if sub not in USERS:
        return None
    return {"username": sub, "role": payload.get("role")}


def require_login(handler):
    @wraps(handler)
    async def wrapper(*args, **kwargs):
        user = await current_user()
        if user is None:
            return jsonify({"detail": "未登录"}), 401
        return await handler(user, *args, **kwargs)

    return wrapper


def require_writer(handler):
    @wraps(handler)
    async def wrapper(*args, **kwargs):
        user = await current_user()
        if user is None:
            return jsonify({"detail": "未登录"}), 401
        if user["role"] != "writer":
            return jsonify({"detail": "仅现场技师可提交偏航记录"}), 403
        return await handler(user, *args, **kwargs)

    return wrapper


@app.get("/api/health")
async def health():
    return jsonify({"status": "ok", "service": "yaw-align-log"})


@app.post("/api/auth/login")
async def login():
    body = await request.get_json(force=True, silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = USERS.get(username)
    if not user or not pwd.verify(password, user["password_hash"]):
        return jsonify({"detail": "用户名或密码错误"}), 401
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": username, "role": user["role"], "exp": exp},
        SECRET,
        algorithm="HS256",
    )
    return jsonify(
        {
            "access_token": token,
            "username": username,
            "role": user["role"],
        }
    )


@app.get("/api/logs")
@require_login
async def list_logs(user):
    def query():
        with connect() as conn:
            return conn.execute(
                """SELECT id, turbine_code, yaw_err_deg, status, verdict, reason,
                          created_by, created_at, processed_at
                   FROM yaw_logs ORDER BY id DESC"""
            ).fetchall()

    rows = await run_db(query)
    return jsonify(rows)


@app.post("/api/logs")
@require_writer
async def create_log(user):
    body = await request.get_json(force=True, silent=True) or {}
    turbine_code = (body.get("turbine_code") or "").strip()
    if not turbine_code:
        return jsonify({"detail": "机组编号不能为空"}), 400
    try:
        yaw_err_deg = float(body.get("yaw_err_deg"))
    except (TypeError, ValueError):
        return jsonify({"detail": "偏航误差必须是数字"}), 400

    now = datetime.now(timezone.utc)

    def insert():
        with connect() as conn:
            row = conn.execute(
                """INSERT INTO yaw_logs
                   (turbine_code, yaw_err_deg, status, verdict, reason,
                    created_by, created_at)
                   VALUES (%s, %s, 'pending', NULL, NULL, %s, %s)
                   RETURNING id, turbine_code, yaw_err_deg, status, verdict, reason,
                             created_by, created_at, processed_at""",
                (turbine_code, yaw_err_deg, user["username"], now),
            ).fetchone()
            conn.commit()
            return row

    row = await run_db(insert)
    return jsonify(row), 201


DEFAULT_WINDOW_MINUTES = 60.0
MAX_WINDOW_MINUTES = 24.0 * 60.0


@app.get("/api/drift/slope")
@require_login
async def drift_slope(user):
    """偏航漂移斜率台：逐机列出窗内样本点与后台拟合斜率（度/小时）。

    斜率只能由后台按办结时刻拟合得出；尚未办结的记录不入样本。
    """
    raw = request.args.get("window_minutes")
    try:
        window_minutes = float(raw) if raw is not None else DEFAULT_WINDOW_MINUTES
    except (TypeError, ValueError):
        return jsonify({"detail": "window_minutes 必须是数字"}), 400
    if window_minutes < 0 or window_minutes > MAX_WINDOW_MINUTES:
        return jsonify({"detail": "window_minutes 须在 0 到 1440 分钟之间"}), 400

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)

    def query():
        with connect() as conn:
            return conn.execute(
                """SELECT turbine_code, yaw_err_deg, processed_at
                   FROM yaw_logs
                   WHERE status = 'done' AND processed_at IS NOT NULL
                   ORDER BY turbine_code, processed_at, id"""
            ).fetchall()

    rows = await run_db(query)
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["turbine_code"], []).append(row)

    turbines = []
    for code in sorted(grouped):
        in_window = [r for r in grouped[code] if r["processed_at"] >= cutoff]
        slope = fit_slope_deg_per_hour(
            [(r["processed_at"], r["yaw_err_deg"]) for r in in_window]
        )
        turbines.append(
            {
                "turbine_code": code,
                "slope_deg_per_hour": slope,
                "sample_count": len(in_window),
                "samples": [
                    {
                        "processed_at": r["processed_at"].isoformat(),
                        "yaw_err_deg": r["yaw_err_deg"],
                    }
                    for r in in_window
                ],
            }
        )

    return jsonify(
        {
            "window_minutes": window_minutes,
            "generated_at": now.isoformat(),
            "turbines": turbines,
        }
    )
