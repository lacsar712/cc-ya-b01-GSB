import asyncio
import os
from datetime import datetime, timedelta, timezone
from functools import wraps

from jose import JWTError, jwt
from passlib.context import CryptContext
from quart import Quart, jsonify, request

from db import SCHEMA, connect
from drift import (
    DEFAULT_WINDOW_SECONDS,
    MAX_WINDOW_SECONDS,
    MIN_WINDOW_SECONDS,
    current_window_seconds,
    refit_all,
    snapshot,
)
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

    # W02：三条办结时刻等间隔、误差水平的历史点。24h 窗内三点拟合斜率为 0（平），
    # 用于交班前对照「正在漂 / 没有漂」。
    flat_yaw = 0.2
    flat_verdict, flat_reason = judge(flat_yaw)
    for hours_ago in (12, 6, 1):
        ts = now - timedelta(hours=hours_ago)
        conn.execute(
            """INSERT INTO yaw_logs
               (turbine_code, yaw_err_deg, status, verdict, reason,
                created_by, created_at, processed_at)
               VALUES (%s, %s, 'done', %s, %s, %s, %s, %s)""",
            ("W02", flat_yaw, flat_verdict, flat_reason, "technician", ts, ts),
        )

    # 首次后台拟合，串「办结时刻」链路：种子点已是 done，直接进入样本。
    refit_all(conn, DEFAULT_WINDOW_SECONDS, now=now)


@app.before_serving
async def startup():
    def init():
        with connect() as conn:
            seed_if_empty(conn)
            if conn.execute(
                "SELECT 1 FROM drift_settings WHERE id = 1"
            ).fetchone() is None:
                # 旧库（已有办结点但从未拟合）启动时补一次后台拟合
                refit_all(conn, current_window_seconds(conn))
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


@app.get("/api/drift/slopes")
@require_login
async def drift_slopes(user):
    def query():
        with connect() as conn:
            return snapshot(conn)

    snap = await run_db(query)
    if snap is None:
        return jsonify({"detail": "尚未进行后台拟合"}), 409
    return jsonify(snap)


@app.post("/api/drift/refit")
@require_writer
async def drift_refit(user):
    body = await request.get_json(force=True, silent=True) or {}

    raw = body.get("window_seconds", DEFAULT_WINDOW_SECONDS)
    if isinstance(raw, bool):
        return jsonify({"detail": "窗宽必须是整数秒"}), 400
    try:
        window_seconds = int(raw)
    except (TypeError, ValueError):
        return jsonify({"detail": "窗宽必须是整数秒"}), 400
    if not (MIN_WINDOW_SECONDS <= window_seconds <= MAX_WINDOW_SECONDS):
        return jsonify(
            {"detail": f"窗宽需在 {MIN_WINDOW_SECONDS}~{MAX_WINDOW_SECONDS} 秒之间"}
        ), 400

    def fit():
        with connect() as conn:
            result = refit_all(conn, window_seconds)
            conn.commit()
            return result

    snap = await run_db(fit)
    return jsonify(snap)
