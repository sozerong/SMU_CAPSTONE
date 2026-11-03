"""
질의형 GraphRAG — 임의 질문을 받아 그래프 후보 안에서만 답하는 LangGraph 워크플로

배치(`graphrag_aq.py`)는 고정 질문 6개에 미리 답을 만들어 두는 구조라
그 밖의 질문에는 답할 수 없다. 이 모듈은 같은 후보 제한·검증 규칙을 유지한 채
질문을 그때그때 받는다.

    understand ─▶ retrieve ─▶ generate ─▶ validate ─┬─ ok ─────────────▶ END
                                   ▲                ├─ 실패, 재시도 남음 ─┘ (사유를 프롬프트에 넣어 재생성)
                                   └────────────────┘
                                                    └─ 실패, 재시도 소진 ─▶ dead_letter ─▶ END

## understand: 질의 조건을 그래프 어휘에 묶는다

`candidates.parse_question` 은 문자열 규칙이라 규칙에 없는 표현("달달한", "여름 음료")을
못 읽고, 내는 태그 값("단맛", "트렌디")도 그래프의 Tag 이름("달콤함", "상큼함")과 다르다.
그래서 LLM 이 조건을 뽑되, 선택지를 **그래프에 실제로 있는 Tag/Season/Category 값으로
만든 enum** 으로 제한한다. 그래프에 없는 조건은 스키마상 만들 수 없다.
LLM 호출이 실패하면 규칙 파서로 떨어진다 — 조건 추출 실패가 답변 실패가 되면 안 된다.

## validate 이후 재생성

배치는 검증 실패를 dead letter 로 격리만 했다. 여기서는 실패 사유
(`ungrounded:망고` 등)를 프롬프트에 넣어 최대 `MAX_RETRY` 번 다시 생성하고,
그래도 실패하면 같은 형식으로 격리한다. 판정은 `grounding.validate_answer` 한 곳에서만 한다.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field, create_model

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from candidates import (INGREDIENT_CYPHER, INGREDIENT_FALLBACK_CYPHER, LIMIT,  # noqa: E402
                        MENU_FALLBACK_CYPHER, build_menu_cypher, parse_question)
from generate import generate, make_llm  # noqa: E402
from grounding import validate_answer  # noqa: E402

MAX_RETRY = int(os.getenv("ASK_MAX_RETRY", "2"))
# 프롬프트가 3~5개 추천을 요구한다. 후보가 이보다 적으면 모델은 후보 밖에서 채울 수밖에 없다.
MIN_CANDIDATES = 3
DEAD_LETTER_PATH = os.getenv("ASK_DEAD_LETTER", "data/ask_dead_letter.jsonl")


class AskState(TypedDict, total=False):
    question: str
    conditions: Dict[str, Any]
    condition_source: str          # llm | rule
    candidates: List[str]
    relaxed: List[str]             # 후보를 찾느라 푼 조건. "all" 이면 조건 무시 빈도순
    answer: Any
    feedback: Optional[str]
    attempts: int
    ok: bool
    reason: Optional[str]
    timings: Dict[str, float]      # 노드별 누적 초


def graph_vocabulary(session) -> Dict[str, List[str]]:
    """조건 enum 의 원천. 그래프가 바뀌면 선택지도 같이 바뀐다."""
    q = lambda c: [r["v"] for r in session.run(c) if r["v"]]  # noqa: E731
    return {"tags": q("MATCH (t:Tag) RETURN DISTINCT t.name AS v"),
            "seasons": q("MATCH (s:Season) RETURN DISTINCT s.value AS v"),
            "categories": q("MATCH (c:Category) RETURN DISTINCT c.value AS v")}


def conditions_model(vocab: Dict[str, List[str]]) -> type[BaseModel]:
    Tag = Literal[tuple(vocab["tags"])]                       # type: ignore[valid-type]
    Season = Literal[tuple(vocab["seasons"])]                 # type: ignore[valid-type]
    Category = Literal[tuple(vocab["categories"])]            # type: ignore[valid-type]
    return create_model(
        "Conditions",
        use_ingredient=(bool, Field(description="재료를 묻는 질문이면 true, 메뉴를 묻는 질문이면 false")),
        tags=(List[Tag], Field(default_factory=list, max_length=3,
                               description="질문이 요구하는 맛·식감·분위기. 해당 없으면 빈 목록")),
        season=(Optional[Season], Field(default=None, description="질문이 특정 계절을 말할 때만")),
        category=(Optional[Category], Field(default=None, description="질문이 메뉴 종류를 좁힐 때만")),
    )


def understand_prompt(vocab: Dict[str, List[str]]) -> str:
    """
    선택지를 스키마에만 두면 모델이 태그를 거의 고르지 않았다
    (v1: 태그 정답 26/60, 대부분 빈 목록. eval/ask_conditions_20261103_181346.json).
    그래서 그래프 어휘를 프롬프트에도 보여 주고 판단 규칙을 적는다.
    예시는 평가 질문셋(eval/ask_questions.jsonl)에 없는 표현만 쓴다.
    """
    return (
        "카페 메뉴 추천 시스템의 질의 분석기입니다. 사용자 질문에서 그래프 검색 조건을 뽑으세요.\n\n"
        f"태그 선택지: {', '.join(vocab['tags'])}\n"
        f"계절 선택지: {', '.join(vocab['seasons'])}\n"
        f"종류 선택지: {', '.join(vocab['categories'])}\n\n"
        "규칙:\n"
        "- use_ingredient: 질문이 '재료'(메뉴에 넣을 원료)를 물을 때만 true. "
        "메뉴 이름에 재료가 들어 있어도 메뉴를 묻는 질문이면 false\n"
        "- tags: 질문에 맛·식감·온도·분위기 표현이 있으면 태그 선택지에서 뜻이 가장 가까운 것을 1~2개 고른다. "
        "표현이 없으면 빈 목록\n"
        "- season: 질문이 계절을 직접 말할 때만. category: 질문이 음료·디저트·빵 같은 종류를 직접 말할 때만. "
        "아니면 null\n\n"
        "예: '매콤한 간식' → tags=[매콤함], category=null / "
        "'촉촉한 케이크' → tags=[촉촉함], category=디저트 / "
        "'라떼에 넣을 시럽 재료' → use_ingredient=true"
    )


def extract_conditions(llm, Conditions: type[BaseModel], question: str,
                       vocab: Dict[str, List[str]]) -> Optional[Dict[str, Any]]:
    """LLM 조건 추출. 실패하면 None — 호출자가 규칙 파서로 떨어진다."""
    try:
        c = llm.with_structured_output(Conditions, method="json_schema").invoke(
            [("system", understand_prompt(vocab)), ("human", question)])
        return c.model_dump() if c is not None else None
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 규칙 파서로 떨어진다
        print(f"[understand] 규칙 파서로 대체: {type(e).__name__}", file=sys.stderr)
        return None


def retrieve_candidates(runner, conditions: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    조건 → 후보 키워드. runner 는 `.run(cypher, **params)` 가 있는 Neo4j 세션이나 트랜잭션.
    질의 서비스와 MCP 서버(`app/mcp_server.py`)가 같이 쓴다.

    돌려주는 relaxed 는 후보를 찾느라 푼 조건이다. "all" 이면 조건을 전부 버리고 빈도순이다.
    """
    cond = dict(conditions)
    relaxed: List[str] = []
    if cond["use_ingredient"]:
        rows = [r["keyword"] for r in runner.run(INGREDIENT_CYPHER, limit=LIMIT)]
        fallback = INGREDIENT_FALLBACK_CYPHER
    else:
        # 조건이 AND 로 묶여 있어 셋을 다 주면 비거나 너무 적기 쉽다. 전부 버리고 빈도순으로
        # 떨어지기 전에 덜 중요한 조건부터 하나씩 푼다 (종류 → 계절 → 맛).
        # 실측: "여름에 시원하게 마실 음료" 가 여름+음료로 후보 1개 → 재시도 3회 모두 후보 밖.
        rows = []
        for drop in (None, "category", "season", "tags"):
            if drop:
                if not cond.get(drop):
                    continue
                cond[drop] = [] if drop == "tags" else None
                relaxed.append(drop)
            cypher, params = build_menu_cypher(cond)
            rows = [r["keyword"] for r in runner.run(cypher, **params)]
            if len(rows) >= MIN_CANDIDATES:
                break
        fallback = MENU_FALLBACK_CYPHER
    # 후보가 비면 LLM 이 제약 없이 생성한다. 빈 목록은 넘기지 않는다.
    if not rows:
        rows = [r["keyword"] for r in runner.run(fallback, limit=LIMIT)]
        relaxed.append("all")
    return rows, relaxed


def build_app(session, llm=None, max_retry: int = MAX_RETRY, structured: bool = True,
              llm_conditions: bool = True):
    """
    Neo4j 세션과 LLM 을 묶은 컴파일된 그래프. 세션 수명은 호출자가 관리한다.
    llm_conditions=False 면 규칙 파서만 쓴다 (`eval_ask.py` 의 대조군).
    """
    llm = llm or make_llm()
    vocab = graph_vocabulary(session)
    Conditions = conditions_model(vocab)

    def timed(name):
        def deco(fn):
            def run(state: AskState) -> AskState:
                t0 = time.perf_counter()
                out = fn(state)
                timings = dict(state.get("timings") or {})
                timings[name] = timings.get(name, 0.0) + time.perf_counter() - t0
                return {**out, "timings": timings}
            return run
        return deco

    @timed("understand")
    def understand(state: AskState) -> AskState:
        q = state["question"]
        if not llm_conditions:
            return {"conditions": parse_question(q), "condition_source": "rule"}
        cond = extract_conditions(llm, Conditions, q, vocab)
        if cond is None:
            return {"conditions": parse_question(q), "condition_source": "rule"}
        return {"conditions": cond, "condition_source": "llm"}

    @timed("retrieve")
    def retrieve(state: AskState) -> AskState:
        rows, relaxed = retrieve_candidates(session, state["conditions"])
        return {"candidates": rows, "relaxed": relaxed}

    @timed("generate")
    def gen(state: AskState) -> AskState:
        ans = generate(llm, state["question"], state["candidates"],
                       feedback=state.get("feedback"), structured=structured)
        return {"answer": ans, "attempts": state.get("attempts", 0) + 1}

    @timed("validate")
    def validate(state: AskState) -> AskState:
        ok, out = validate_answer(state["answer"], state["candidates"])
        if ok:
            return {"ok": True, "answer": out, "reason": None}
        return {"ok": False, "reason": out, "feedback": out}

    def dead_letter(state: AskState) -> AskState:
        os.makedirs(os.path.dirname(DEAD_LETTER_PATH) or ".", exist_ok=True)
        with open(DEAD_LETTER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "question": state["question"], "keywords": state["candidates"],
                "reason": state["reason"], "raw_response": state["answer"],
                "attempts": state["attempts"],
                "failed_at": datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False) + "\n")
        return {}

    def route(state: AskState) -> str:
        if state["ok"]:
            return END
        return "generate" if state["attempts"] <= max_retry else "dead_letter"

    g = StateGraph(AskState)
    g.add_node("understand", understand)
    g.add_node("retrieve", retrieve)
    g.add_node("generate", gen)
    g.add_node("validate", validate)
    g.add_node("dead_letter", dead_letter)
    g.set_entry_point("understand")
    g.add_edge("understand", "retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", "validate")
    g.add_conditional_edges("validate", route, ["generate", "dead_letter", END])
    g.add_edge("dead_letter", END)
    return g.compile()


def demo() -> None:
    """
    자체 검증 (Neo4j·모델 없이) — `python GragpRAG/ask_graph.py`
    가짜 세션·가짜 LLM 으로 재생성 루프와 격리 분기를 확인한다.
    """
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    global DEAD_LETTER_PATH
    import tempfile
    DEAD_LETTER_PATH = os.path.join(tempfile.mkdtemp(), "dl.jsonl")

    class FakeSession:
        def run(self, cypher, **_):
            if "DISTINCT t.name" in cypher:
                return [{"v": "달콤함"}]
            if "DISTINCT s.value" in cypher:
                return [{"v": "여름"}]
            if "DISTINCT c.value" in cypher:
                return [{"v": "디저트"}]
            return [{"keyword": "딸기"}, {"keyword": "크림"}]

    class FakeLLM(FakeListChatModel):
        def with_structured_output(self, *a, **k):  # 조건 추출 실패 → 규칙 파서 경로
            raise NotImplementedError

    bad = '[{"name":"망고 빙수","description":"x"}]'
    good = '[{"name":"딸기 크림","description":"x"}]'

    # 1회 실패 후 재생성으로 통과
    app = build_app(FakeSession(), FakeLLM(responses=[bad, good]), max_retry=2, structured=False)
    s = app.invoke({"question": "달콤한 메뉴"})
    assert s["ok"] and s["attempts"] == 2 and s["answer"][0]["name"] == "딸기 크림", s
    assert s["condition_source"] == "rule" and s["candidates"] == ["딸기", "크림"], s
    assert set(s["timings"]) == {"understand", "retrieve", "generate", "validate"}, s["timings"]

    # 재시도를 다 써도 실패하면 격리되고 기록이 남는다
    app = build_app(FakeSession(), FakeLLM(responses=[bad] * 3), max_retry=1, structured=False)
    s = app.invoke({"question": "달콤한 메뉴"})
    assert not s["ok"] and s["attempts"] == 2 and s["reason"].startswith("ungrounded"), s
    with open(DEAD_LETTER_PATH, encoding="utf-8") as f:
        assert json.loads(f.readline())["attempts"] == 2

    # 조건을 다 걸면 비는 경우: 종류부터 풀고, 맛 조건은 살린다
    class NoCategorySession(FakeSession):
        def run(self, cypher, **kw):
            if "HAS_CATEGORY" in cypher:             # 후보 1개: 3~5개 추천에 모자란다
                return [{"keyword": "딸기"}]
            if "LIMIT" in cypher and "MATCH (k:Keyword)" in cypher:
                return [{"keyword": k} for k in ("딸기", "크림", "초코")]
            return super().run(cypher, **kw)

    class RuleOnly(FakeLLM):
        pass
    app = build_app(NoCategorySession(), RuleOnly(responses=[good]), structured=False)
    s = app.invoke({"question": "카페 신메뉴 추천"})        # 규칙상 category=디저트
    assert s["relaxed"] == ["category"] and s["ok"], s

    # 조건 enum 은 그래프 어휘 밖의 값을 거부한다
    C = conditions_model({"tags": ["달콤함"], "seasons": ["여름"], "categories": ["디저트"]})
    assert C(use_ingredient=False, tags=["달콤함"]).tags == ["달콤함"]
    try:
        C(use_ingredient=False, tags=["단맛"])
        raise AssertionError("그래프에 없는 태그가 통과했다")
    except Exception as e:  # noqa: BLE001
        assert "단맛" in str(e) or "literal" in str(e).lower(), e

    print("ask_graph 자체 검증 통과")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    demo()
