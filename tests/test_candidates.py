"""
후보 탐색 쿼리 검증 — 실제 Neo4j 에 픽스처 그래프를 심고 확인한다

가장 중요한 회귀는 이것이다:
**존재하지 않는 관계를 `OPTIONAL MATCH` 로 조회하면 에러 없이 전부 0 이 되고,
`ORDER BY` 가 무의미해져 임의의 후보가 나간다.** 실제로 그래서 재료 경로의
후보 밖 생성률이 50% 였다.

단위 테스트로는 이걸 못 잡는다. Cypher 가 문법적으로 맞고 예외도 안 나기 때문이다.
그래서 실제 Neo4j 에 알려진 그래프를 심고 **순위가 의도대로 나오는지** 본다.

Neo4j 가 없으면 전체를 skip 한다.
    docker compose -f docker/docker-compose.yml up -d neo4j
    python -m pytest tests/test_candidates.py -v

⚠️ 이 테스트는 그래프를 지우고 픽스처를 심는다. 개발용 인스턴스에서만 쓸 것.
"""

from __future__ import annotations

import os
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "GragpRAG"))

from candidates import (  # noqa: E402
    INGREDIENT_CYPHER,
    get_keywords_by_question,
    parse_question,
)

URI      = os.environ.get("NEO4J_URI",      "bolt://localhost:7687")
USER     = os.environ.get("NEO4J_USER",     "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")


@pytest.fixture(scope="module")
def session():
    neo4j = pytest.importorskip("neo4j", reason="neo4j 드라이버 미설치")
    try:
        driver = neo4j.GraphDatabase.driver(URI, auth=(USER, PASSWORD))
        driver.verify_connectivity()
    except Exception as e:                                   # noqa: BLE001
        pytest.skip(f"Neo4j 접속 불가 ({URI}): {e}")

    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
        _load_fixture(s)
        yield s
        s.run("MATCH (n) DETACH DELETE n")
    driver.close()


def _load_fixture(s) -> None:
    """
    순위가 예측 가능한 작은 그래프.

    재료별 기대 순위 (최근일 DESC, 빈도합 DESC):
      딸기   : 2026-01-02, count 100+50 = 150   → 1위
      크림   : 2026-01-02, count 100     = 100   → 2위
      바닐라 : 2026-01-01, count 999            → 3위 (빈도는 크지만 날짜가 이전)
      고아   : Combo 에 안 걸림                  → 나오면 안 됨
    """
    s.run("""
    CREATE (d1:Date {value:'2026-01-01'}), (d2:Date {value:'2026-01-02'})

    CREATE (k1:Keyword {name:'딸기 크림 케이크', count:100})
    CREATE (k2:Keyword {name:'딸기 타르트',      count:50})
    CREATE (k3:Keyword {name:'바닐라 라떼',      count:999})

    CREATE (c1:Combo {name:'딸기+크림'}), (c2:Combo {name:'딸기'}), (c3:Combo {name:'바닐라'})
    CREATE (i1:Ingredient {name:'딸기'}), (i2:Ingredient {name:'크림'}),
           (i3:Ingredient {name:'바닐라'}), (i4:Ingredient {name:'고아재료'})

    CREATE (k1)-[:IS_COMBO_WITH]->(c1), (c1)-[:INCLUDES]->(i1), (c1)-[:INCLUDES]->(i2)
    CREATE (k2)-[:IS_COMBO_WITH]->(c2), (c2)-[:INCLUDES]->(i1)
    CREATE (k3)-[:IS_COMBO_WITH]->(c3), (c3)-[:INCLUDES]->(i3)

    CREATE (k1)-[:RECORDED_ON]->(d2), (k2)-[:RECORDED_ON]->(d2), (k3)-[:RECORDED_ON]->(d1)

    CREATE (t:Tag {name:'단맛'}), (ca:Category {value:'디저트'}), (se:Season {value:'봄'})
    CREATE (k1)-[:HAS_TAG]->(t), (k1)-[:HAS_CATEGORY]->(ca), (k1)-[:HAS_SEASON]->(se)
    """)


# ── 재료 경로 ─────────────────────────────────────────────────
def test_ingredient_candidates_are_ranked_not_arbitrary(session):
    """빈도·최근일 기준으로 정렬돼야 한다. 이게 깨져 있던 부분이다."""
    got = session.execute_read(get_keywords_by_question, "최근 유행하는 재료")
    assert got[:3] == ["딸기", "크림", "바닐라"], got


def test_ingredient_candidates_exclude_orphans(session):
    """Combo 에 안 걸린 재료는 Keyword 와 이어지지 않으므로 후보가 아니다."""
    got = session.execute_read(get_keywords_by_question, "최근 유행하는 재료")
    assert "고아재료" not in got, got


def test_recency_beats_frequency(session):
    """
    바닐라는 count 999 로 가장 크지만 기록일이 하루 이르다.
    "최근 유행하는" 이라는 질문이므로 최근일이 우선이어야 한다.
    """
    got = session.execute_read(get_keywords_by_question, "최근 유행하는 재료")
    assert got.index("딸기") < got.index("바닐라")
    assert got.index("크림") < got.index("바닐라")


def test_ingredient_query_uses_only_existing_relationships(session):
    """
    쿼리에 쓰인 관계가 실제 DB 에 있어야 한다.
    없는 관계는 OPTIONAL MATCH 로 조용히 0 이 되므로 문자열 검사만으로는 부족하다 —
    DB 에 물어본다.
    """
    present = {r["t"] for r in session.run("CALL db.relationshipTypes() YIELD relationshipType AS t RETURN t")}
    for rel in ("INCLUDES", "IS_COMBO_WITH", "RECORDED_ON"):
        assert rel in present, f"{rel} 가 그래프에 없다 — 쿼리가 빈 결과를 낸다"
    assert "CONTAINS_INGREDIENT" not in INGREDIENT_CYPHER


def test_never_returns_empty_candidates(session):
    """
    후보가 비면 LLM 이 제약 없이 생성한다. 어떤 질문이든 빈 목록은 안 된다.
    """
    for q in ["최근 유행하는 재료", "SNS에서 핫한 메뉴", "카페 신메뉴 추천",
              "달콤한 메뉴가 필요할 때", "비주얼이 예쁜 메뉴", "계절 한정 디저트",
              "조건에 아무것도 안 걸리는 질문"]:
        got = session.execute_read(get_keywords_by_question, q)
        assert got, f"후보가 비었다: {q}"


# ── 메뉴 경로 ─────────────────────────────────────────────────
def test_menu_path_filters_by_tag(session):
    got = session.execute_read(get_keywords_by_question, "달콤한 메뉴가 필요할 때")
    assert "딸기 크림 케이크" in got, got


def test_menu_path_falls_back_when_no_match(session):
    """
    조건에 맞는 게 없으면 폴백이 돈다. 폴백은 Keyword 를 주므로
    재료(딸기/크림)가 아니라 키워드 이름이 나와야 한다.
    """
    got = session.execute_read(get_keywords_by_question, "비주얼이 예쁜 메뉴")
    assert got
    assert any("케이크" in g or "라떼" in g or "타르트" in g for g in got), got


def test_question_routing():
    """Neo4j 없이도 되는 부분 — 경로 분기."""
    assert parse_question("최근 유행하는 재료")["use_ingredient"] is True
    assert parse_question("SNS에서 핫한 메뉴")["use_ingredient"] is False
