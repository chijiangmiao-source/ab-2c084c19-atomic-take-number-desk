# 场记镜号发放系统

多台场记终端为同一场次领取下一条镜号。镜号按场次从 **1** 开始**严格连续**发放；
超时重试、重复提交、进程崩溃都不会造成重号或跳号。

- **客户端**：React + TypeScript（Vite），提交场次、不可复用的 `client_op_id` 和备注；
  失败后保留待重试操作（localStorage 持久化），恢复后显示唯一镜号。
- **服务端**：FastAPI + PostgreSQL。操作映射与场次计数**只**保存在数据库中，
  不引入任何额外线程/进程/队列 worker 做协调。
- **验收**：`verify` 一次性服务跑 Vitest（前端单元）、pytest（并发/幂等/重启）、
  Playwright（浏览器端到端）。

## 快速开始

```bash
# 启动完整系统（db + api + web）
docker compose up --build -d db api web

# 打开页面
open http://localhost:${WEB_PORT:-8080}
```

宿主端口可用环境变量覆盖（默认值如下）：

```bash
WEB_PORT=9090 API_PORT=9000 docker compose up --build -d db api web
```

| 变量       | 默认 | 说明                     |
| ---------- | ---- | ------------------------ |
| `WEB_PORT` | 8080 | 页面（nginx）宿主端口    |
| `API_PORT` | 8000 | FastAPI 服务宿主端口     |

## 一次性验收

```bash
docker compose up --build --exit-code-from verify
```

`verify` 容器依次执行：

1. **Vitest**：前端重试队列、API 客户端、错误反馈组件的单元测试；
2. **pytest**：20 个并发操作、重复提交（每操作并发双份/三份提交）、冲突载荷（409）、
   注入故障后并发恢复、**重启 API 进程**后的幂等回放与连续性；
3. **Playwright**：真实浏览器中注入故障 → 页面保留待重试操作并展示错误 →
   手动/自动重试 → 显示唯一镜号；重复提交同号；409 冲突反馈；连续领号。

全部通过后容器以 0 退出。重启测试通过挂载的 `/var/run/docker.sock`
重启 `shotnum-api` 容器（数据库容器不重启，数据独立持久化）。

## API

### `POST /api/shot-numbers`

```json
{
  "client_op_id": "op-7f3c…",          // 不可复用的操作标识
  "scene_id": "A-12",                  // 场次
  "note": "夜戏 追车",                  // 备注
  "inject_failure_after_commit": false // 仅开发模式（见下）
}
```

- **200**：`{ "number": 7, "idempotent_replay": false, … }`
  相同 `client_op_id` + 相同内容（`scene_id`、`note`）无论并发、重试还是进程重启，
  都返回最初号码（`idempotent_replay: true` 表示本次为回放）。
- **409**：相同 `client_op_id` 携带不同内容。冲突不消耗号码。
- **503**：仅在开发模式请求注入故障时返回（见下），且号码已落库。

其他端点：`GET /api/health`、`GET /api/scenes/{scene_id}/shot-numbers`、
`GET /api/shot-numbers/{client_op_id}`。

## 事务边界与一致性设计

数据库是唯一协调者。两张表：

- `scene_counters(scene_id PK, next_number)`：每场次的下一个待发号码；
- `shot_operations(client_op_id PK, scene_id, note, number, created_at,
  UNIQUE(scene_id, number))`：操作标识 → 镜号的持久映射。

一次发号请求的**事务边界**（单个 PostgreSQL 事务，显式 `commit`/`rollback`）：

```
BEGIN
  1. SELECT … FROM shot_operations WHERE client_op_id = $op
     → 已存在：内容一致则直接返回原号码（幂等回放）；不一致则 409
  2. INSERT … scene_counters … ON CONFLICT DO NOTHING   -- 确保计数器行存在
  3. SELECT next_number … FOR UPDATE                    -- 行锁：同场次发号串行化
  4. UPDATE scene_counters SET next_number = next_number + 1
  5. INSERT … shot_operations (client_op_id, scene_id, note, number)
COMMIT                                                   -- 持久化点
-- 提交之后才组装响应 --
```

关键性质：

- **无重复**：同一场次的号码由计数器行锁串行分配；`(scene_id, number)`
  唯一约束兜底。并发同一 `client_op_id` 时，后到的插入触发主键冲突，
  回滚后回读已提交记录，按幂等回放处理。
- **无缺口**：计数器 `+1` 与操作映射写入在**同一事务**内；事务失败整体回滚，
  号码不会被消耗。崩溃/重试不会留下空洞。
- **顺序**：号码分配顺序 = 计数器行锁获取顺序 = 事务提交顺序（锁持有到提交）。
- **崩溃安全**：服务在“落库后、回包前”崩溃时，客户端看到失败并重试；
  重试命中第 1 步的回放路径，取回最初号码，绝不重新占号。

## 开发模式：故障注入

`api` 容器设置了 `ENABLE_FAILURE_INJECTION=true`（生产部署应移除）。
此时请求可携带 `inject_failure_after_commit=true`：

- 该操作**首次完成持久提交后**，服务端返回 **503**（模拟落库后、回包前崩溃）；
- 之后同标识重试只走幂等回放、取回原号码，**不会**再次触发故障；
- 注入标志是传输级元数据，不参与“相同内容”比较，也不会被持久化。

页面上勾选“开发选项 → 注入故障”即可演练；URL 加 `?autoretry=off`
可关闭自动重试，便于观察手动重试。

## 项目结构

```
├── docker-compose.yml      # db / api / web / verify 一键启动
├── api/                    # FastAPI 发号服务
│   └── app/main.py         #   事务边界、幂等回放、故障注入
├── web/                    # React + TS 客户端（nginx 伺服，/api 反代到 api）
│   └── src/queue.ts        #   待重试操作队列（localStorage 持久化）
└── verify/                 # 一次性验收服务
    ├── tests/              #   pytest：并发 / 幂等 / 409 / 重启
    └── e2e/                #   Playwright：浏览器重试与错误反馈
```

## 本地开发

```bash
# 前端单元测试
cd web && npm ci && npm test

# 前端开发服务器（代理 /api 到本机 8000）
cd web && npm run dev

# API（需自备 PostgreSQL，设置 DATABASE_URL）
cd api && pip install -r requirements.txt
DATABASE_URL=postgresql://shotnum:shotnum@localhost:5432/shotnum \
ENABLE_FAILURE_INJECTION=true uvicorn app.main:app --reload
```
