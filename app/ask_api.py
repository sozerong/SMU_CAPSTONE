"""
질의 API — `POST /ask` 로 임의 질문을 받아 GraphRAG 워크플로(`GragpRAG/ask_graph.py`)를 돌린다

    uvicorn app.ask_api:app --port 8001
    curl -X POST localhost:8001/ask -H "Content-Type: application/json" -d '{"question":"여름에 시원한 음료"}'

`es_search_api.py` 와 따로 둔 이유: 그쪽은 import 시점에 PostgreSQL 에 붙는다.
질의 경로가 서빙 DB 상태에 묶이면 안 된다.

검증을 통과하지 못한 답은 200 으로 돌려주지 않는다. 422 와 실패 사유를 준다.
"검증 실패한 답을 사용자에게 보여주지 않는다"가 배치 시절부터의 규칙이다.
"""

from __future__ import annotations

import os
import sys

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "GragpRAG"))
# 저장소 루트의 neo4j/ 디렉터리가 드라이버를 가리지 않게 루트를 뺀다 (conftest.py 와 같은 이유)
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _ROOT]

from neo4j import GraphDatabase  # noqa: E402

from ask_graph import build_app  # noqa: E402
from generate import make_llm  # noqa: E402

driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password123")))
llm = make_llm()
app = FastAPI(title="VCC GraphRAG")


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=200)


@app.get("/health")
def health():
    driver.verify_connectivity()
    return {"neo4j": "ok", "model": getattr(llm, "model", None) or getattr(llm, "model_name", None)}


@app.post("/ask")
def ask(body: AskIn):
    # ponytail: 요청마다 세션과 그래프를 새로 만든다 (어휘 조회 3쿼리 포함).
    # Neo4j 세션은 스레드 안전하지 않고 FastAPI 동기 핸들러는 스레드풀에서 돈다.
    # 동시 요청이 많아지면 어휘를 캐시하고 노드 안에서 세션을 여는 쪽으로 바꾼다.
    with driver.session() as session:
        s = build_app(session, llm).invoke({"question": body.question})
    meta = {"question": body.question, "conditions": s["conditions"],
            "condition_source": s["condition_source"], "candidates": s["candidates"],
            "relaxed": s["relaxed"],
            "attempts": s["attempts"], "timings": s["timings"]}
    if not s["ok"]:
        raise HTTPException(422, {**meta, "reason": s["reason"]})
    return {**meta, "answer": s["answer"]}
