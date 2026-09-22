"""
후보 목록 수정 전/후 비교 — LLM 없이 잴 수 있는 부분

`eval_grounding.py` 는 **후보 밖 생성률**을 잰다. 그건 LLM 이 만든 답변이 있어야 하므로
재측정에 OpenAI 키가 필요하다. 그런데 ADR-0006 이 지목한 근본 원인은 LLM 이 아니라
**후보 목록 자체**였다. 후보는 Neo4j 만 있으면 잴 수 있다.

이 스크립트는 생성률을 대신하지 않는다. 생성률의 **입력**이 실제로 고쳐졌는지를 잰다.

## 무엇을 재는가

1. **결정성** — 같은 DB 에 같은 쿼리를 N회 던져 결과 집합이 같은가.
   옛 쿼리는 `usage_count` 가 전부 0 이라 `ORDER BY` 가 동점 정렬이 된다.
   동점일 때 Neo4j 는 순서를 보장하지 않으므로 후보가 조용히 바뀔 수 있다.

2. **근거 있는 순위** — 순위를 설명하는 값이 실제로 존재하는가.
   옛 쿼리의 정렬 키(`usage_count`)가 전부 0 이면 순위에 의미가 없다.

3. **고아 재료 포함 여부** — 어떤 Keyword 와도 연결되지 않은 Ingredient 가
   후보에 들어가는가. 들어가면 "최근 유행하는 재료"라는 질문에 답이 못 된다.

## 왜 이게 생성률과 연결되는가

ADR-0006 의 논지는 "후보가 질문에 답이 못 되면 모델이 밖에서 끌어온다" 였다.
그 주장의 전제는 **후보가 실제로 엉망이었다**는 것이고, 여기서 그 전제를 직접 확인한다.
생성률 재측정은 여전히 남는다 — 이 스크립트는 그 자리를 채우지 않는다.

사용
    python GragpRAG/eval_candidates.py            # Neo4j 필요
    python GragpRAG/eval_candidates.py --selftest # DB 없이 로직만
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from candidates import INGREDIENT_CYPHER, LIMIT            # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT      = os.path.join(BASE_DIR, "eval", "candidate_quality.json")
REPEATS  = 10

# 수정 전 쿼리. 저장소에서 지웠지만 대조군으로 여기 남긴다 —
# 비교 대상이 코드에 없으면 "고쳤다"를 검증할 수 없다.
OLD_CYPHER = """
MATCH (i:Ingredient)
OPTIONAL MATCH (e:Example)-[:CONTAINS_INGREDIENT]->(i)
WITH i, count(e) AS usage_count
RETURN i.name AS keyword, usage_count AS score
ORDER BY usage_count DESC
LIMIT $limit
"""

NEW_WITH_SCORE = """
MATCH (i:Ingredient)<-[:INCLUDES]-(:Combo)<-[:IS_COMBO_WITH]-(k:Keyword)
OPTIONAL MATCH (k)-[:RECORDED_ON]->(d:Date)
WITH i, max(d.value) AS latest, sum(k.count) AS freq
RETURN i.name AS keyword, freq AS score
ORDER BY latest DESC, freq DESC
LIMIT $limit
"""

# 어떤 Keyword 와도 이어지지 않은 Ingredient. "유행"을 말할 근거가 없는 노드다.
ORPHAN_CYPHER = """
MATCH (i:Ingredient)
WHERE NOT (i)<-[:INCLUDES]-(:Combo)<-[:IS_COMBO_WITH]-(:Keyword)
RETURN i.name AS keyword
"""

TOTAL_CYPHER = "MATCH (i:Ingredient) RETURN count(i) AS n"

# 옛 쿼리가 타던 관계가 DB 에 실제로 있는지. 경고 메시지를 읽는 것보다 직접 묻는 편이 낫다.
RELTYPES_CYPHER = "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType AS t"

# `Keyword.count` 는 `ON MATCH SET k.count = coalesce(k.count,0) + $count` 로 **누적**된다.
# 같은 파일을 두 번 적재하면 freq 가 정확히 2배가 된다(실측: 딸기 209 → 418).
# 측정값이 적재 횟수에 의존하므로 회차 수를 같이 기록한다.
ROUNDS_CYPHER = "MATCH (d:Date) RETURN count(d) AS n"


def run(tx, cypher: str, **kw) -> List[dict]:
    return [dict(r) for r in tx.run(cypher, **kw)]


def measure(session, name: str, cypher: str, orphans: set) -> dict:
    """같은 쿼리를 REPEATS 회 돌려 결정성·순위 근거·고아 포함을 본다."""
    runs = [session.execute_read(run, cypher, limit=LIMIT) for _ in range(REPEATS)]
    sets = {tuple(sorted(r["keyword"] for r in rows)) for rows in runs}
    orders = {tuple(r["keyword"] for r in rows) for rows in runs}

    first  = runs[0]
    scores = [r["score"] for r in first]
    names  = [r["keyword"] for r in first]

    return {
        "query": name,
        "top": names,
        "scores": scores,
        "distinct_sets":   len(sets),     # 1 이면 집합이 항상 같다
        "distinct_orders": len(orders),   # 1 이면 순서까지 같다
        "all_scores_zero": all(s == 0 for s in scores),
        "distinct_scores": len(set(scores)),
        "orphans_in_top":  sorted(set(names) & orphans),
    }


def main() -> None:
    import logging
    # 옛 쿼리는 없는 관계를 타므로 실행마다 서버 경고가 온다. 같은 내용을 아래에서
    # db.relationshipTypes() 로 한 번만 확인하므로 로그는 막는다.
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

    from neo4j import GraphDatabase
    uri  = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    auth = (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password123"))
    driver = GraphDatabase.driver(uri, auth=auth)

    with driver.session() as s:
        total    = s.execute_read(run, TOTAL_CYPHER)[0]["n"]
        rounds   = s.execute_read(run, ROUNDS_CYPHER)[0]["n"]
        reltypes = {r["t"] for r in s.execute_read(run, RELTYPES_CYPHER)}
        orphans  = {r["keyword"] for r in s.execute_read(run, ORPHAN_CYPHER)}
        before   = measure(s, "before (CONTAINS_INGREDIENT)", OLD_CYPHER, orphans)
        after    = measure(s, "after (INCLUDES/IS_COMBO_WITH)", NEW_WITH_SCORE, orphans)
    driver.close()

    print("=" * 70)
    print(f"  재료 후보 쿼리 전/후 — Ingredient {total}개 중 고아 {len(orphans)}개, 각 {REPEATS}회 실행")
    print("=" * 70)
    print(f"  적재 회차 {rounds}개."
          + ("  freq 는 1회 적재 기준이다." if rounds == 1 else
             "  ⚠ count 는 누적되므로 freq 가 회차 수만큼 부풀어 있다."))
    print(f"  DB 관계 타입: {', '.join(sorted(reltypes))}")
    print(f"  CONTAINS_INGREDIENT 존재 여부: "
          f"{'있음' if 'CONTAINS_INGREDIENT' in reltypes else '없음 — 옛 쿼리는 빈 OPTIONAL MATCH 였다'}")

    for r in (before, after):
        print(f"\n  [{r['query']}]")
        print(f"    상위 {LIMIT}: {', '.join(f'{k}({v})' for k, v in zip(r['top'], r['scores']))}")
        print(f"    정렬 키가 전부 0 : {'예 — 순위에 근거가 없다' if r['all_scores_zero'] else '아니오'}"
              f"   (서로 다른 값 {r['distinct_scores']}종)")
        print(f"    {REPEATS}회 결과 집합 : {r['distinct_sets']}종"
              f"   {'(일정)' if r['distinct_sets'] == 1 else '← 실행마다 다르다'}")
        print(f"    {REPEATS}회 결과 순서 : {r['distinct_orders']}종"
              f"   {'(일정)' if r['distinct_orders'] == 1 else '← 실행마다 다르다'}")
        print(f"    고아 재료 포함     : {len(r['orphans_in_top'])}개"
              + (f" {r['orphans_in_top']}" if r["orphans_in_top"] else ""))

    changed = set(before["top"]) - set(after["top"])
    print(f"\n  후보에서 빠진 재료 {len(changed)}개: {', '.join(sorted(changed)) or '없음'}")
    print()
    print("  주의: 이것은 후보 밖 생성률이 아니다. 생성률 재측정에는 OpenAI 키가 필요하다.")
    print("        여기서 잰 것은 생성률의 입력, 즉 후보 목록의 품질이다.")

    out = {"ingredients_total": total, "orphans": len(orphans),
           "rounds_loaded": rounds, "repeats": REPEATS,
           "contains_ingredient_exists": "CONTAINS_INGREDIENT" in reltypes,
           "before": before, "after": after,
           "dropped_from_candidates": sorted(changed)}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n  저장: {os.path.relpath(OUT, BASE_DIR)}")


def selftest() -> None:
    """DB 없이 확인 가능한 것 — 두 쿼리가 실제로 다른 관계를 타는가."""
    assert "CONTAINS_INGREDIENT" in OLD_CYPHER
    assert "CONTAINS_INGREDIENT" not in NEW_WITH_SCORE
    assert "CONTAINS_INGREDIENT" not in INGREDIENT_CYPHER
    for rel in ("INCLUDES", "IS_COMBO_WITH", "RECORDED_ON"):
        assert rel in NEW_WITH_SCORE, rel
        assert rel in INGREDIENT_CYPHER, rel
    # 대조군이 본 쿼리와 정렬 기준이 같아야 비교가 성립한다
    assert "ORDER BY latest DESC, freq DESC" in NEW_WITH_SCORE
    assert "ORDER BY latest DESC, freq DESC" in INGREDIENT_CYPHER
    # 고아 정의가 본 쿼리의 경로와 같아야 한다
    assert "INCLUDES" in ORPHAN_CYPHER and "IS_COMBO_WITH" in ORPHAN_CYPHER
    print("eval_candidates 자체 검증 통과")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="재료 후보 쿼리 전/후 비교")
    ap.add_argument("--selftest", action="store_true")
    selftest() if ap.parse_args().selftest else main()
