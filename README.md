# 风机偏航对中台

现场技师登记机组编号与偏航误差（度）；后台 worker 用数据库行锁认领待处理记录，按 ±1.5° 阈值写入「合格」或「偏航超差」。前端为 Lit 组件 + Vite，接口为 Quart + Hypercorn。

顶栏可进入「偏航漂移斜率台」专页：上区拖统计窗宽，中区逐机列出后台拟合斜率（°/h）与窗内样本点，下区为只读口径说明。斜率只能由后台按办结时刻拟合，前端不改数、不重算。

## 端口

| 服务 | 地址 |
|------|------|
| 页面 | http://localhost:3199 |
| 接口 | http://localhost:8199 |
| PostgreSQL | localhost:54399（库名 `yawalign`） |

## 账号

| 用户 | 密码 | 权限 |
|------|------|------|
| technician | tech123456 | 可提交 |
| observer | obs123456 | 只读 |

## 启动

```bash
cd projects/20-yaw-align-log
docker compose up --build
```

健康检查：`GET http://localhost:8199/api/health` → `{"status":"ok","service":"yaw-align-log"}`。

## 验收

1. 种子数据：机组 W01 误差 0.4° 结论「合格」；机组 W07 误差 3.2° 结论「偏航超差」。
2. technician 提交新记录后，列表先显示「待处理」，数秒内 worker 处理后变为对应结论。
3. observer 可查看列表，无提交表单。
4. 顶栏进入「偏航漂移斜率台」：上区拖窗宽（仅技师可调；观察员禁改禁报），中区逐机列拟合斜率与样本点，下区只读口径说明。
5. 斜率口径：仅「已完成」记录按办结时刻（processed_at）落入统计窗的点入样本，待处理记录不入样本；后台做最小二乘直线拟合，单位 °/h；窗内样本不足 2 点（含窗口缩到样本之外）时斜率为空显示「—」。连抬三单办结后该机斜率应变正。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/drift/slope?window_minutes=N` | 登录即可（0–1440 分钟，默认 60）；返回逐机窗内样本点与后台拟合斜率（°/h） |

## 技术栈

- 后端：Quart、psycopg、`worker.py`（`FOR UPDATE SKIP LOCKED`）、Hypercorn
- 前端：Lit、TypeScript、Vite；生产镜像内 nginx 反代 `/api`
- 镜像源：DaoCloud 基础镜像、清华 PyPI、npmmirror npm
