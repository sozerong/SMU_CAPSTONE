"""
VCC 그래프 MCP 서버: 후보 탐색과 후보 안/밖 판정을 외부 에이전트에 도구로 연다

파이프라인의 핵심 규칙은 "LLM 은 그래프가 준 후보 안에서만 추천한다"이다.
그 규칙은 지금 `graphrag_aq.py` 안에서만 집행된다. Claude 같은 외부 에이전트가
그래프를 쓰려면 같은 규칙에 묶여야 하는데, 후보 쿼리와 판정 로직을 따로 옮기면
`grounding.py` 가 막으려던 "기준이 두 곳에서 갈라지는" 문제가 그대로 재발한다.

그래서 이 서버는 새 로직을 만들지 않는다.
  - 후보 탐색: `GragpRAG/candidates.py::get_keywords_by_question` 를 그대로 호출
  - 후보 판정: `GragpRAG/grounding.py::validate_answer` 를 그대로 호출 (lenient 기준)
외부 에이전트는 스스로 생성하되, 결과를 `validate_recommendations` 에 넘겨
파이프라인과 같은 기준으로 격리 여부를 받는다.

조건 추출도 외부 에이전트가 한다. `graph_vocabulary` 로 그래프에 실제 있는 태그·계절·종류를
받아 `find_candidates` 에 넘기면, 서버는 어휘 밖 값을 `unknown` 으로 돌려주고 버린다.
조건을 안 넘기면 규칙 파서(`parse_question`)를 쓴다. 후보 검색과 조건 완화는
`ask_graph.retrieve_candidates` 를 그대로 쓴다 (`/ask` 와 같은 함수).

Neo4j 에는 읽기만 한다 (`execute_read`). 쓰기 쿼리는 이 파일에 없다.

## 도구

    graph_vocabulary()                             → {tags, seasons, categories}  조건에 쓸 수 있는 값
    find_candidates(question, [조건])              → {path, conditions, condition_source, unknown, relaxed, candidates}
    ask(question)                                  → 서버 쪽 LLM 으로 질문 → 후보 → 생성 → 검증 (`/ask` 와 같은 워크플로)
    validate_recommendations(answer, candidates)   → {ok, items | reason}
    keyword_neighbors(keyword)                     → {found, count, tags, seasons, categories, combos, related, dates}
    trending_keywords(limit=10)                    → {keywords: [{keyword, count, latest}]}

## 그래프에 실재하는 관계 (2026-11-03 실측, Keyword 337개)

    Keyword -[:RELATED {score}]-> Keyword    339   (방향 있음, 역방향 쌍 0)
    Keyword -[:RECORDED_ON]->     Date       337   (Date 노드 1개)
    Keyword -[:HAS_TAG]->         Tag        235
    Combo   -[:INCLUDES]->        Ingredient 216
    Keyword -[:IS_COMBO_WITH]->   Combo      125
    Keyword -[:HAS_EXAMPLE]->     Example    120
    Keyword -[:HAS_CATEGORY]->    Category    86
    Keyword -[:HAS_SEASON]->      Season      23

`keyword_neighbors` 는 위 관계 중 HAS_EXAMPLE 만 뺀다. Example 은 원문 문장이라
이웃 요약에 넣으면 응답만 길어진다.

## 실행

    python app/mcp_server.py              # stdio 서버
    python app/mcp_server.py --selftest   # 서버를 자식 프로세스로 띄워 MCP 프로토콜로 검증 + 지연 측정

stdio 전송은 stdout 을 프로토콜에 쓴다. 서버 쪽에서는 stdout 에 아무것도 출력하지 않는다
(로그는 stderr). 출력이 섞이면 클라이언트가 JSON-RPC 파싱에 실패한다.

## 등록 (Claude Desktop `claude_desktop_config.json` / Claude Code `.mcp.json`)

    {
      "mcpServers": {
        "vcc-graph": {
          "command": "python",
          "args": ["C:/Users/dudfo/Desktop/git/11123/SMU_CAPSTONE/app/mcp_server.py"],
          "env": {
            "NEO4J_URI": "bolt://localhost:7687",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": "password123"
          }
        }
      }
    }

Claude Code 는 CLI 로도 등록된다.
    claude mcp add vcc-graph -e NEO4J_PASSWORD=password123 -- python app/mcp_server.py

`env` 를 생략하면 아래 기본값을 쓴다. MCP 클라이언트는 부모 환경 변수를 일부만 넘기므로
(Windows 는 PATH, USERPROFILE 등) 기본값과 다른 접속 정보는 `env` 에 적어야 한다.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 저장소 루트의 neo4j/ 디렉터리가 드라이버 패키지를 가린다 (conftest.py 참고).
# 루트는 빼고 GragpRAG/ 만 넣는다.
sys.path[:] = [p for p in sys.path if os.path.abspath(p) != _ROOT]
sys.path.insert(0, os.path.join(_ROOT, "GragpRAG"))

from neo4j import GraphDatabase  # noqa: E402

from ask_graph import graph_vocabulary as _vocab_query, retrieve_candidates  # noqa: E402
from candidates import parse_question  # noqa: E402
from grounding import validate_answer  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

URI      = os.environ.get("NEO4J_URI",      "bolt://localhost:7687")
USER     = os.environ.get("NEO4J_USER",     "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")

log = logging.getLogger("vcc-mcp")

mcp = MCPServer(
    "vcc-graph",
    instructions=(
        "카페 메뉴 트렌드 그래프. 추천은 find_candidates 의 candidates 안에서만 만들고, "
        "내기 전에 validate_recommendations 로 확인한다."
    ),
)

_driver = None


def _read(work):
    """읽기 트랜잭션 하나. 드라이버는 첫 호출에 만들어 재사용한다 (연결 수립이 호출당 비용의 대부분)."""
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    with _driver.session() as s:
        return s.execute_read(work)


# 반환형은 내장 dict[str, Any] 로 쓴다. typing.Dict 는 MCPServer 가 제네릭으로 인식하지 못해
# {"result": ...} 로 한 번 더 감싼다 (mcp 2.3.0 func_metadata._create_output_model).

# 패턴 컴프리헨션으로 관계마다 따로 모은다.
# OPTIONAL MATCH 를 이어 붙이면 태그 x 콤보 x 연관어 곱만큼 행이 불어난다.
NEIGHBORS_CYPHER = """
MATCH (k:Keyword {name: $keyword})
RETURN k.count AS count,
       [(k)-[:HAS_TAG]->(t:Tag)           | t.name]  AS tags,
       [(k)-[:HAS_SEASON]->(s:Season)     | s.value] AS seasons,
       [(k)-[:HAS_CATEGORY]->(c:Category) | c.value] AS categories,
       [(k)-[:IS_COMBO_WITH]->(c:Combo)   |
          {combo: c.name, ingredients: [(c)-[:INCLUDES]->(i:Ingredient) | i.name]}] AS combos,
       [(k)-[r:RELATED]-(o:Keyword)       | {keyword: o.name, score: r.score}] AS related,
       [(k)-[:RECORDED_ON]->(d:Date)      | d.value] AS dates
"""

# candidates.py 재료 경로와 같은 정렬: 최근 기록일 1순위, 언급 빈도 2순위.
TRENDING_CYPHER = """
MATCH (k:Keyword)
OPTIONAL MATCH (k)-[:RECORDED_ON]->(d:Date)
WITH k, max(d.value) AS latest
RETURN k.name AS keyword, k.count AS count, latest
ORDER BY latest DESC, count DESC
LIMIT $limit
"""


@mcp.tool()
def graph_vocabulary() -> dict[str, Any]:
    """find_candidates 의 조건에 쓸 수 있는 값: 그래프에 실재하는 Tag 이름, Season, Category."""
    return _read(_vocab_query)


@mcp.tool()
def find_candidates(question: str, use_ingredient: bool | None = None,
                    tags: list[str] | None = None, season: str | None = None,
                    category: str | None = None) -> dict[str, Any]:
    """질문에 맞는 후보 키워드 (최대 10개). 추천은 이 candidates 안에서만 만들어야 한다.
    조건(tags/season/category)은 graph_vocabulary 의 값만 유효하다. 하나도 안 주면 규칙 파서를 쓴다.
    relaxed 는 후보를 찾느라 푼 조건이고, "all" 이면 조건을 무시한 빈도순 후보다."""
    rule = parse_question(question)
    given = any(v is not None for v in (use_ingredient, tags, season, category))
    unknown: list[str] = []
    if given:
        vocab = _read(_vocab_query)
        ok_tags = [t for t in (tags or []) if t in vocab["tags"]]
        unknown += [t for t in (tags or []) if t not in vocab["tags"]]
        if season is not None and season not in vocab["seasons"]:
            unknown.append(season)
            season = None
        if category is not None and category not in vocab["categories"]:
            unknown.append(category)
            category = None
        cond = {"use_ingredient": rule["use_ingredient"] if use_ingredient is None else use_ingredient,
                "tags": ok_tags, "season": season, "category": category}
    else:
        cond = rule
    cands, relaxed = _read(lambda tx: retrieve_candidates(tx, cond))
    return {
        "path": "ingredient" if cond["use_ingredient"] else "menu",
        "conditions": cond,
        "condition_source": "agent" if given else "rule",
        "unknown": unknown,
        "relaxed": relaxed,
        "candidates": cands,
    }


@mcp.tool()
def validate_recommendations(answer: list[dict[str, Any]] | str, candidates: list[str]) -> dict[str, Any]:
    """추천 결과가 후보 안인지 판정한다 (파이프라인 런타임 격리와 같은 lenient 기준).
    answer 는 [{name, description}] 리스트 또는 그 JSON 문자열.
    ok=false 면 reason: json_parse_error / not_a_list / missing_name_field / ungrounded:<이름들>"""
    ok, out = validate_answer(answer, candidates)
    return {"ok": True, "items": out} if ok else {"ok": False, "reason": out}


@mcp.tool()
def keyword_neighbors(keyword: str) -> dict[str, Any]:
    """키워드 하나의 그래프 이웃: 태그, 계절, 카테고리, 콤보(재료 포함), 연관 키워드, 언급 수."""
    rec = _read(lambda tx: tx.run(NEIGHBORS_CYPHER, keyword=keyword).single())
    if rec is None:
        return {"found": False, "keyword": keyword}
    return {"found": True, "keyword": keyword, **rec.data()}


@mcp.tool()
def trending_keywords(limit: int = 10) -> dict[str, Any]:
    """최근 기록일 기준, 언급 수가 많은 키워드 순."""
    limit = max(1, min(int(limit), 50))
    rows = _read(lambda tx: [r.data() for r in tx.run(TRENDING_CYPHER, limit=limit)])
    return {"keywords": rows}


_llm = None


@mcp.tool()
def ask(question: str) -> dict[str, Any]:
    """서버 쪽 LLM(LLM_PROVIDER/LLM_MODEL, 기본 로컬 Ollama)으로 질문에 답한다.
    조건 추출 → 후보 → 생성 → 검증 → 실패 시 사유를 넣어 재생성. ok=false 면 답 대신 reason."""
    global _llm
    from ask_graph import build_app
    from generate import make_llm
    _llm = _llm or make_llm()
    if _driver is None:
        _read(lambda tx: None)
    with _driver.session() as s:
        st = build_app(s, _llm).invoke({"question": question})
    out = {k: st.get(k) for k in ("conditions", "condition_source", "candidates",
                                    "relaxed", "attempts")}
    return {**out, "ok": True, "answer": st["answer"]} if st["ok"] else            {**out, "ok": False, "reason": st["reason"]}


def selftest(n: int = 20) -> None:
    """서버를 자식 프로세스로 띄워 MCP stdio 로 호출한다 (`python app/mcp_server.py --selftest`)

    함수를 직접 부르면 도구 등록, 인자 스키마, 직렬화가 검증되지 않는다. 그래서 프로토콜을 거친다.
    """
    import anyio
    import statistics
    import time

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    sys.stdout.reconfigure(encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k.startswith("NEO4J_")}
    params = StdioServerParameters(command=sys.executable, args=[os.path.abspath(__file__)], env=env)

    async def call(c, name, args):
        r = await c.call_tool(name, args)
        assert not r.is_error, (name, r.content)
        return r.structured_content

    async def bench(c, name, args):
        ms = []
        for _ in range(n):
            t = time.perf_counter()
            await call(c, name, args)
            ms.append((time.perf_counter() - t) * 1000)
        ms.sort()
        return statistics.median(ms), ms[int(len(ms) * 0.95) - 1], ms[-1]

    async def main():
        async with Client(params) as c:
            tools = sorted(t.name for t in (await c.list_tools()).tools)
            print("tools:", tools)
            assert tools == ["ask", "find_candidates", "graph_vocabulary", "keyword_neighbors",
                             "trending_keywords", "validate_recommendations"], tools

            ing = await call(c, "find_candidates", {"question": "최근 유행하는 재료"})
            print("재료 경로:", ing["path"], ing["candidates"])
            assert ing["path"] == "ingredient" and ing["candidates"]

            menu = await call(c, "find_candidates", {"question": "달콤한 메뉴가 필요할 때"})
            print("메뉴 경로(규칙):", menu["conditions"]["tags"], menu["relaxed"], menu["candidates"])
            assert menu["path"] == "menu" and menu["condition_source"] == "rule"
            assert menu["candidates"]

            vocab = await call(c, "graph_vocabulary", {})
            assert "달콤함" in vocab["tags"] and "여름" in vocab["seasons"], vocab
            agent = await call(c, "find_candidates", {"question": "달콤한 메뉴가 필요할 때",
                                                      "tags": ["달콤함", "없는태그"]})
            print("메뉴 경로(에이전트 조건):", agent["conditions"]["tags"], agent["unknown"],
                  agent["relaxed"], agent["candidates"])
            assert agent["condition_source"] == "agent" and agent["unknown"] == ["없는태그"]
            assert agent["relaxed"] == [] and agent["candidates"], agent

            kw = agent["candidates"][0]
            nb = await call(c, "keyword_neighbors", {"keyword": kw})
            assert set(nb["tags"]) & {"달콤함"}, nb   # 에이전트 조건으로 뽑은 후보는 그 태그를 가진다
            print(f"이웃({kw}): count={nb['count']} tags={nb['tags']} seasons={nb['seasons']} "
                  f"categories={nb['categories']} combos={len(nb['combos'])} related={len(nb['related'])}")
            assert nb["found"] and nb["keyword"] == kw and isinstance(nb["count"], int), nb
            assert all(isinstance(x["ingredients"], list) for x in nb["combos"]), nb

            miss = await call(c, "keyword_neighbors", {"keyword": "존재하지않는키워드"})
            assert miss["found"] is False

            tr = await call(c, "trending_keywords", {"limit": 5})
            print("트렌드:", [(r["keyword"], r["count"], r["latest"]) for r in tr["keywords"]])
            assert len(tr["keywords"]) == 5

            cands = menu["candidates"]
            good = [{"name": f"{cands[0]} 라떼", "description": "후보 조합"}]
            v = await call(c, "validate_recommendations", {"answer": good, "candidates": cands})
            print("후보 안:", v)
            assert v["ok"] is True

            bad = '[{"name": "쿠알라룸푸르 빙수", "description": "후보 밖"}]'
            v = await call(c, "validate_recommendations", {"answer": bad, "candidates": cands})
            print("후보 밖:", v)
            assert v["ok"] is False and v["reason"].startswith("ungrounded:")

            # 지연: 클라이언트에서 call_tool 왕복 (stdio + JSON 직렬화 + Neo4j 읽기 트랜잭션)
            for name, args in [("find_candidates", {"question": "달콤한 메뉴가 필요할 때"}),
                               ("find_candidates", {"question": "최근 유행하는 재료"}),
                               ("keyword_neighbors", {"keyword": kw})]:
                p50, p95, mx = await bench(c, name, args)
                print(f"지연 {name}{args}: n={n} p50={p50:.2f}ms p95={p95:.2f}ms max={mx:.2f}ms")

    anyio.run(main)
    print("mcp_server 자체 검증 통과")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        mcp.run("stdio")
