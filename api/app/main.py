"""镜号发放服务（shot number issuer）。

为每个场次（scene）从 1 开始分配严格连续的整数镜号。

事务边界（详见 README.md）：
  * 单个数据库事务内依次完成：幂等回放检查 -> 创建/锁定场次计数器行
    （SELECT ... FOR UPDATE）-> 取号 -> 计数器 +1 -> 写入操作映射 -> 提交。
  * 计数器行锁保证同一场序的发号串行化：无重复、无缺口，
    号码顺序即事务提交顺序。
  * 响应在提交之后发出；若服务在“落库后、回包前”崩溃，客户端用相同
    client_op_id 重试时会走幂等回放路径，取回最初号码。
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Iterator

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://shotnum:shotnum@localhost:5432/shotnum"
)
# 开发模式开关：允许请求携带 inject_failure_after_commit=true 来模拟
# “落库后、回包前崩溃”。生产部署不应打开。
ENABLE_FAILURE_INJECTION = os.environ.get("ENABLE_FAILURE_INJECTION", "").lower() in {
    "1",
    "true",
    "yes",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scene_counters (
    scene_id    TEXT PRIMARY KEY,
    next_number INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS shot_operations (
    client_op_id TEXT PRIMARY KEY,
    scene_id     TEXT NOT NULL,
    note         TEXT NOT NULL,
    number       INTEGER NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scene_id, number)
);
"""

log = logging.getLogger("shotnum")

# open=False：连接池在 lifespan 启动阶段显式打开（带重试，等待数据库就绪）。
pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=32,
    open=False,
    check=ConnectionPool.check_connection,
)


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """显式事务边界：块内全部语句构成一个事务。

    正常结束时 commit（持久化点）；任何异常都会 rollback 后再抛出，
    因此失败的事务不会消耗镜号（计数器随事务回滚，不产生缺口）。
    """
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:  # 连接可能已断开，回滚失败不应掩盖原异常
            pass
        raise
    finally:
        pool.putconn(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    deadline = time.monotonic() + 90
    while True:
        try:
            pool.open(wait=True, timeout=10)
            with transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(SCHEMA_SQL)
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    yield
    pool.close()


app = FastAPI(title="shot-number-issuer", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AllocateRequest(BaseModel):
    client_op_id: str = Field(min_length=1, max_length=128)
    scene_id: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=2000)
    inject_failure_after_commit: bool = False


class AllocateResponse(BaseModel):
    client_op_id: str
    scene_id: str
    note: str
    number: int
    idempotent_replay: bool


class _Outcome:
    """一次请求在数据库中的结果：新分配的号码，或回放到的既有号码。"""

    __slots__ = ("number", "fresh")

    def __init__(self, number: int, fresh: bool) -> None:
        self.number = number
        self.fresh = fresh


def _assert_same_content(row: tuple, req: AllocateRequest) -> None:
    """相同 client_op_id 必须携带相同内容，否则 409。"""
    scene_id, note, number = row
    if scene_id != req.scene_id or note != req.note:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "client_op_id_conflict",
                "message": "client_op_id 已被不同内容使用，操作标识不可复用",
                "existing": {"scene_id": scene_id, "note": note, "number": number},
            },
        )


def _allocate_in_tx(conn: psycopg.Connection, req: AllocateRequest) -> _Outcome:
    """在调用方的事务内完成发号；本函数不自行提交。"""
    with conn.cursor() as cur:
        # 1) 幂等回放检查：该操作此前是否已落库
        cur.execute(
            "SELECT scene_id, note, number FROM shot_operations WHERE client_op_id = %s",
            (req.client_op_id,),
        )
        row = cur.fetchone()
        if row is not None:
            _assert_same_content(row, req)
            return _Outcome(number=row[2], fresh=False)

        # 2) 确保场次计数器行存在
        cur.execute(
            "INSERT INTO scene_counters (scene_id, next_number) VALUES (%s, 1) "
            "ON CONFLICT (scene_id) DO NOTHING",
            (req.scene_id,),
        )
        # 3) 行锁序列化该场次的发号，锁持有到事务提交
        cur.execute(
            "SELECT next_number FROM scene_counters WHERE scene_id = %s FOR UPDATE",
            (req.scene_id,),
        )
        number = cur.fetchone()[0]
        # 4) 计数器 +1（若事务回滚则一并回滚，不产生缺口）
        cur.execute(
            "UPDATE scene_counters SET next_number = next_number + 1 WHERE scene_id = %s",
            (req.scene_id,),
        )
        # 5) 写入操作映射（client_op_id 主键 + (scene_id, number) 唯一约束兜底）
        cur.execute(
            "INSERT INTO shot_operations (client_op_id, scene_id, note, number) "
            "VALUES (%s, %s, %s, %s)",
            (req.client_op_id, req.scene_id, req.note, number),
        )
        return _Outcome(number=number, fresh=True)


def _read_committed(req: AllocateRequest) -> _Outcome:
    """唯一约束冲突后回读：并发下同一 client_op_id 已被另一事务提交。"""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scene_id, note, number FROM shot_operations WHERE client_op_id = %s",
                (req.client_op_id,),
            )
            row = cur.fetchone()
    if row is None:
        # 唯一约束冲突却读不到记录，只可能是 (scene_id, number) 兜底约束被触发，
        # 在计数器行锁下不应发生。
        raise HTTPException(status_code=500, detail="allocation state inconsistent")
    _assert_same_content(row, req)
    return _Outcome(number=row[2], fresh=False)


@app.post("/api/shot-numbers", response_model=AllocateResponse)
def allocate(req: AllocateRequest) -> AllocateResponse:
    if req.inject_failure_after_commit and not ENABLE_FAILURE_INJECTION:
        raise HTTPException(
            status_code=400,
            detail="failure injection is disabled on this server",
        )

    try:
        with transaction() as conn:
            outcome = _allocate_in_tx(conn, req)
        # —— 事务在此提交，号码已持久化 ——
    except psycopg.errors.UniqueViolation:
        # 并发下同一 client_op_id 被另一事务抢先提交；回读已落库的记录。
        outcome = _read_committed(req)

    if outcome.fresh and req.inject_failure_after_commit:
        # 开发模式：模拟“落库后、回包前崩溃”。只有首次真实提交会触发；
        # 之后同标识重试走回放路径（fresh=False），不会再次触发。
        raise HTTPException(
            status_code=503, detail="injected failure after commit (dev mode)"
        )

    return AllocateResponse(
        client_op_id=req.client_op_id,
        scene_id=req.scene_id,
        note=req.note,
        number=outcome.number,
        idempotent_replay=not outcome.fresh,
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/scenes/{scene_id}/shot-numbers")
def list_scene(scene_id: str) -> dict[str, Any]:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT client_op_id, note, number, created_at FROM shot_operations "
                "WHERE scene_id = %s ORDER BY number",
                (scene_id,),
            )
            items = [
                {
                    "client_op_id": r[0],
                    "note": r[1],
                    "number": r[2],
                    "created_at": r[3].isoformat(),
                }
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT next_number FROM scene_counters WHERE scene_id = %s",
                (scene_id,),
            )
            row = cur.fetchone()
    return {
        "scene_id": scene_id,
        "next_number": row[0] if row else 1,
        "items": items,
    }


@app.get("/api/shot-numbers/{client_op_id}")
def get_operation(client_op_id: str) -> dict[str, Any]:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scene_id, note, number, created_at FROM shot_operations "
                "WHERE client_op_id = %s",
                (client_op_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="client_op_id not found")
    return {
        "client_op_id": client_op_id,
        "scene_id": row[0],
        "note": row[1],
        "number": row[2],
        "created_at": row[3].isoformat(),
    }
