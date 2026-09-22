"""
후보 탐색 — 질문에서 Cypher 를 만들어 Neo4j 에서 후보 키워드를 뽑는다

`graphrag_aq.py` 안에 있던 로직을 분리했다. 그 파일은 import 만 해도
Neo4j 연결과 OpenAI 클라이언트 초기화가 같이 실행돼서 테스트할 수가 없다.
후보 선정은 이 파이프라인에서 가장 중요한 단계(여기가 틀리면 프롬프트로 못 막는다)라
검증 가능한 자리에 둔다.

## 두 경로

질문에 "재료"가 들어가면 **재료 경로**, 아니면 **메뉴 경로**다.
둘이 타는 관계가 다르다.

    메뉴 경로: Keyword -[:HAS_TAG|HAS_SEASON|HAS_CATEGORY]-> ...
    재료 경로: Keyword -[:IS_COMBO_WITH]-> Combo -[:INCLUDES]-> Ingredient

## 재료 경로가 고쳐진 이유

예전 쿼리는 `(e:Example)-[:CONTAINS_INGREDIENT]->(i)` 를 셌는데,
**그 관계를 만드는 코드가 저장소에 없다.** `OPTIONAL MATCH` 라 에러도 나지 않고
모든 재료의 `usage_count` 가 0 이 되어 `ORDER BY` 가 무의미해졌다.
결과적으로 임의의 재료 10개를 넘기고 있었고, 그게 재료 경로 후보 밖 생성률 50% 의 원인이다.

실측 (2025-05-03 데이터, Ingredient 98개):
    예전: 밀가루, 버터, 크림, 빵, 그릭 요거트 ...   (usage_count 전부 0)
    지금: 딸기(209), 크림(202), 초콜릿(179) ...     (빈도 합 기준)

자세한 근거는 `docs/adr/0006-llm-hallucination-candidate-restriction.md`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

LIMIT = 10

# 재료 경로 — 실재하는 INCLUDES / IS_COMBO_WITH / RECORDED_ON 만 쓴다.
# 최근 기록일이 1순위, 언급 빈도 합이 2순위. "최근 유행하는" 이라는 질문에 맞춘 정렬이다.
INGREDIENT_CYPHER = """
MATCH (i:Ingredient)<-[:INCLUDES]-(:Combo)<-[:IS_COMBO_WITH]-(k:Keyword)
OPTIONAL MATCH (k)-[:RECORDED_ON]->(d:Date)
WITH i, max(d.value) AS latest, sum(k.count) AS freq
RETURN i.name AS keyword
ORDER BY latest DESC, freq DESC
LIMIT $limit
"""

# Combo 가 아직 적재되지 않은 회차 대비.
# 후보가 비면 LLM 이 제약 없이 생성하므로 빈 목록을 넘기면 안 된다.
INGREDIENT_FALLBACK_CYPHER = """
MATCH (i:Ingredient)
RETURN i.name AS keyword
ORDER BY i.name
LIMIT $limit
"""

MENU_FALLBACK_CYPHER = """
MATCH (k:Keyword)
RETURN k.name AS keyword
ORDER BY k.count DESC
LIMIT $limit
"""


def parse_question(question: str) -> Dict[str, Any]:
    """질문 → 필터 조건. 문자열 규칙이 흩어져 있으면 검증할 수 없어 한곳에 모았다."""
    tags: List[str] = []
    if "달콤" in question:
        tags.append("단맛")
    if "비주얼" in question or "예쁜" in question:
        tags.append("비주얼")
    if "SNS" in question or "핫한" in question:
        tags.append("트렌디")
    return {
        "use_ingredient": "재료" in question,
        "tags":     tags,
        "season":   "봄" if "계절" in question else None,
        "category": "디저트" if ("신메뉴" in question or "카페" in question) else None,
    }


def build_menu_cypher(cond: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """메뉴 경로 Cypher 와 파라미터를 만든다."""
    cypher = "WITH 1 AS _\nMATCH (k:Keyword)\n"
    where: List[str] = []
    params: Dict[str, Any] = {"limit": LIMIT}

    if cond["tags"]:
        cypher += "MATCH (k)-[:HAS_TAG]->(t:Tag)\n"
        where.append("t.name IN $tags")
        params["tags"] = cond["tags"]
    if cond["season"]:
        cypher += "MATCH (k)-[:HAS_SEASON]->(s:Season)\n"
        where.append("s.value = $season")
        params["season"] = cond["season"]
    if cond["category"]:
        cypher += "MATCH (k)-[:HAS_CATEGORY]->(c:Category)\n"
        where.append("c.value = $category")
        params["category"] = cond["category"]

    if where:
        cypher += "WHERE " + " AND ".join(where) + "\n"
    cypher += "RETURN k.name AS keyword, k.count AS count ORDER BY count DESC LIMIT $limit"
    return cypher, params


def get_keywords_by_question(tx, question: str) -> List[str]:
    """질문에 맞는 후보 키워드 최대 10개. 빈 목록은 돌려주지 않는다."""
    cond = parse_question(question)

    if cond["use_ingredient"]:
        rows = [r["keyword"] for r in tx.run(INGREDIENT_CYPHER, limit=LIMIT)]
        if not rows:
            rows = [r["keyword"] for r in tx.run(INGREDIENT_FALLBACK_CYPHER, limit=LIMIT)]
        return rows

    cypher, params = build_menu_cypher(cond)
    rows = [r["keyword"] for r in tx.run(cypher, **params)]
    if not rows:
        rows = [r["keyword"] for r in tx.run(MENU_FALLBACK_CYPHER, limit=LIMIT)]
    return rows


def demo() -> None:
    """Neo4j 없이 되는 부분만 검증 — `python GragpRAG/candidates.py`"""
    # 경로 분기
    assert parse_question("최근 유행하는 재료")["use_ingredient"] is True
    assert parse_question("SNS에서 핫한 메뉴")["use_ingredient"] is False

    # 태그 추출
    assert parse_question("달콤한 메뉴가 필요할 때")["tags"] == ["단맛"]
    assert parse_question("비주얼이 예쁜 메뉴")["tags"] == ["비주얼"]
    assert parse_question("SNS에서 핫한 메뉴")["tags"] == ["트렌디"]
    assert parse_question("계절 한정 디저트")["season"] == "봄"
    assert parse_question("카페 신메뉴 추천")["category"] == "디저트"

    # 조건이 없으면 WHERE 가 붙지 않아야 한다
    c, p = build_menu_cypher(parse_question("아무거나"))
    assert "WHERE" not in c, c
    assert p == {"limit": LIMIT}

    # 조건이 있으면 그 MATCH 와 파라미터가 들어가야 한다
    c, p = build_menu_cypher(parse_question("달콤한 계절 한정 디저트"))
    assert "HAS_TAG" in c and "HAS_SEASON" in c, c
    assert p["tags"] == ["단맛"] and p["season"] == "봄"
    assert c.count("WHERE") == 1

    # 재료 쿼리는 존재하지 않는 관계를 쓰면 안 된다 — 이게 회귀의 핵심이다
    assert "CONTAINS_INGREDIENT" not in INGREDIENT_CYPHER
    assert "INCLUDES" in INGREDIENT_CYPHER and "IS_COMBO_WITH" in INGREDIENT_CYPHER

    print("candidates 자체 검증 통과")


if __name__ == "__main__":
    demo()
