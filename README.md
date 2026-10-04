# 风机偏航对中台

现场技师登记机组编号与偏航误差（度）；后台 worker 用数据库行锁认领待处理记录，按 ±1.5° 阈值写入「合格」或「偏航超差」。前端为 Lit 组件 + Vite，接口为 Quart + Hypercorn。

顶栏另设「偏航漂移斜率」专页：交班前查看各机组偏航是否在漂。斜率沿**办结时刻**串点、由**后台最小二乘拟合**产出（度/小时），前端只展示、不能手填数字。

## 端口

| 服务 | 地址 |
|------|------|
| 页面 | http://localhost:3199 |
| 接口 | http://localhost:8199 |
| PostgreSQL | localhost:54399（库名 `yawalign`） |

## 账号

| 用户 | 密码 | 权限 |
|------|------|------|
| technician | tech123456 | 可提交、可调窗宽并触发后台拟合 |
| observer | obs123456 | 只读（禁改窗宽、禁报拟合） |

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
4. 偏航漂移斜率专页：
   - 上区拖窗宽（0～7 天，含预设），技师点「按此窗宽后台拟合」后由后台重算；观察员滑块禁用、无拟合按钮。
   - 中区逐机列显示拟合斜率与窗内样本点（办结时刻 + 误差°），标记正漂/负漂/平/空。
   - 下区为只读口径说明。
   - 链路：技师连抬三单 → worker 办结（写办结时刻）→ 同事务后台拟合 → 专页轮询取到**正斜率**；把窗宽缩到点都在窗外（空窗）→ 斜率变**空**。
   - 未办结（待处理）的点不入样本；前端无法手改斜率数字。

## 斜率口径

- 样本只取 `yaw_logs.status='done'` 且有 `processed_at` 的记录，按机组分组、组内按办结时刻升序。
- 窗为 `[当前时刻 − 窗宽, 当前时刻]`；窗内不足 2 点、无点、或各点办结时刻重合时斜率为空。
- OLS 直线拟合，斜率单位 度/小时；|斜率| < 0.001 度/小时记为「平」。
- worker 每条办结后按当前窗宽自动重拟合；结果存 `drift_slopes`，窗宽存 `drift_settings` 单例。

## 技术栈

- 后端：Quart、psycopg、`worker.py`（`FOR UPDATE SKIP LOCKED`）、Hypercorn
- 前端：Lit、TypeScript、Vite；生产镜像内 nginx 反代 `/api`
- 镜像源：DaoCloud 基础镜像、清华 PyPI、npmmirror npm
