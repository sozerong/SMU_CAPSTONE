"""
Neo4j 정리 — 회차 단위 삭제와 고아 노드 정리

`neo4j_schema.py` 는 `MERGE` 만 쓴다. 중복은 안 생기지만 **지울 방법이 없다.**
잘못 적재한 회차나 스키마를 바꾸기 전 데이터를 손으로 치울 수단이 필요했다.

전체 삭제로 가지 않은 이유는 [ADR-0007](../docs/adr/0007-idempotent-reload.md) 에 있다.
그래프는 `Date` / `RECORDED_ON` 축을 갖는 **누적 저장소**로 설계했다.
"지난주에는 있었는데 이번 주에 빠진 키워드"를 물으려면 과거가 남아 있어야 한다.
그래서 회차 단위로만 지운다.

## 삭제 순서가 중요하다

`Keyword` 가 뿌리고 나머지는 거기 매달려 있다.

    Keyword -[:RECORDED_ON]-> Date
    Keyword -[:HAS_TAG|HAS_CATEGORY|HAS_SEASON|HAS_EXAMPLE]-> ...
    Keyword -[:IS_COMBO_WITH]-> Combo -[:INCLUDES]-> Ingredient
    Keyword -[:RELATED]-> Keyword

회차를 지우면 그 회차에만 있던 Tag·Combo 등이 아무 데도 안 붙은 채 남는다.
`Ingredient` 는 `Combo` 에 매달려 있으므로 **Combo 를 먼저** 치워야 딸려 나온다.

## 사용

    python neo4j/cleanup.py stats                    # 현황
    python neo4j/cleanup.py delete-round 2025-05-03  # 그 회차만 삭제
    python neo4j/cleanup.py prune                    # 고아 노드만 정리
    python neo4j/cleanup.py reset --yes              # 전체 삭제 (확인 필요)

`--dry-run` 을 붙이면 무엇이 지워질지만 세고 실제로는 지우지 않는다.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List

sys.stdout.reconfigure(encoding="utf-8")

URI      = os.environ.get("NEO4J_URI",      "bolt://localhost:7687")
USER     = os.environ.get("NEO4J_USER",     "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")

# Combo 가 Ingredient 를 물고 있으므로 Combo 를 먼저 치워야 Ingredient 가 고아가 된다.
# 순서를 바꾸면 Ingredient 가 한 번 더 돌 때까지 남는다.
ORPHAN_ORDER = ["Combo", "Ingredient", "Tag", "Category", "Season", "Example", "Date"]


def _driver():
    try:
        from neo4j import GraphDatabase
    except ImportError:
        raise SystemExit("neo4j 드라이버가 없다: pip install neo4j")
    return GraphDatabase.driver(URI, auth=(USER, PASSWORD))


# ── 조회 ─────────────────────────────────────────────────────
def stats(tx) -> Dict[str, Any]:
    nodes = {r["label"]: r["n"] for r in tx.run(
        "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS n ORDER BY n DESC")}
    rels = {r["rel"]: r["n"] for r in tx.run(
        "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC")}
    rounds = [{"date": r["d"], "keywords": r["n"]} for r in tx.run(
        "MATCH (k:Keyword)-[:RECORDED_ON]->(d:Date) "
        "RETURN d.value AS d, count(k) AS n ORDER BY d")]
    untagged = tx.run(
        "MATCH (k:Keyword) WHERE NOT (k)-[:RECORDED_ON]->() RETURN count(k) AS n"
    ).single()["n"]
    return {"nodes": nodes, "rels": rels, "rounds": rounds, "keywords_without_date": untagged}


def count_orphans(tx) -> Dict[str, int]:
    """들어오는 관계가 하나도 없는 부속 노드. Keyword 는 뿌리라 제외한다."""
    out = {}
    for label in ORPHAN_ORDER:
        n = tx.run(f"MATCH (n:{label}) WHERE NOT ()-->(n) RETURN count(n) AS n").single()["n"]
        if n:
            out[label] = n
    return out


# ── 삭제 ─────────────────────────────────────────────────────
def delete_round(tx, date: str, dry_run: bool = False) -> Dict[str, int]:
    """
    한 회차를 지운다.

    1. 그 Date 로만 기록된 Keyword 를 지운다 (다른 회차에도 있으면 남긴다)
    2. 남은 Keyword 의 RECORDED_ON 만 끊는다
    3. Date 노드 삭제
    """
    only_here = tx.run(
        "MATCH (k:Keyword)-[:RECORDED_ON]->(d:Date {value: $date}) "
        "WHERE size([(k)-[:RECORDED_ON]->() | 1]) = 1 "
        "RETURN count(k) AS n", date=date).single()["n"]
    shared = tx.run(
        "MATCH (k:Keyword)-[:RECORDED_ON]->(d:Date {value: $date}) "
        "WHERE size([(k)-[:RECORDED_ON]->() | 1]) > 1 "
        "RETURN count(k) AS n", date=date).single()["n"]

    result: Dict[str, Any] = {"deleted_keywords": only_here, "kept_shared_keywords": shared}
    if dry_run:
        # dry-run 에서는 고아 수를 미리 셀 수 없다. 키워드를 지워봐야 생기기 때문이다.
        result["pruned"] = "(실행해야 알 수 있음)"
        return result

    tx.run("MATCH (k:Keyword)-[:RECORDED_ON]->(d:Date {value: $date}) "
           "WHERE size([(k)-[:RECORDED_ON]->() | 1]) = 1 "
           "DETACH DELETE k", date=date)
    tx.run("MATCH (k:Keyword)-[r:RECORDED_ON]->(d:Date {value: $date}) DELETE r", date=date)
    tx.run("MATCH (d:Date {value: $date}) DETACH DELETE d", date=date)

    # 회차를 지우면 그 회차에만 붙어 있던 Tag·Combo 등이 떠버린다.
    # 호출자가 기억해야 하는 일로 두면 언젠가 빠뜨린다. 여기서 같이 치운다.
    result["pruned"] = prune_orphans(tx, dry_run=False)
    return result


def prune_orphans(tx, dry_run: bool = False) -> Dict[str, int]:
    """아무 데도 안 붙은 부속 노드를 치운다. 순서가 중요하다 (ORPHAN_ORDER 주석 참조)."""
    removed: Dict[str, int] = {}
    for label in ORPHAN_ORDER:
        n = tx.run(f"MATCH (n:{label}) WHERE NOT ()-->(n) RETURN count(n) AS n").single()["n"]
        if not n:
            continue
        removed[label] = n
        if not dry_run:
            tx.run(f"MATCH (n:{label}) WHERE NOT ()-->(n) DETACH DELETE n")
    return removed


def reset(tx) -> int:
    n = tx.run("MATCH (n) RETURN count(n) AS n").single()["n"]
    tx.run("MATCH (n) DETACH DELETE n")
    return n


# ── CLI ──────────────────────────────────────────────────────
def _print_stats(s: Dict[str, Any]) -> None:
    print("노드:", ", ".join(f"{k} {v}" for k, v in s["nodes"].items()) or "없음")
    print("관계:", ", ".join(f"{k} {v}" for k, v in s["rels"].items()) or "없음")
    print("회차:")
    for r in s["rounds"]:
        print(f"  {r['date']}  키워드 {r['keywords']}개")
    if s["keywords_without_date"]:
        print(f"⚠️  회차 태그(RECORDED_ON) 없는 Keyword {s['keywords_without_date']}개 "
              f"— 회차 단위 삭제로는 지워지지 않는다")


def main() -> None:
    ap = argparse.ArgumentParser(description="Neo4j 정리")
    ap.add_argument("cmd", choices=["stats", "delete-round", "prune", "reset", "selftest"])
    ap.add_argument("date", nargs="?", help="delete-round 에 쓸 회차 (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true", help="세기만 하고 지우지 않는다")
    ap.add_argument("--yes", action="store_true", help="reset 확인")
    args = ap.parse_args()

    if args.cmd == "selftest":
        return _selftest()

    with _driver() as drv, drv.session() as s:
        if args.cmd == "stats":
            _print_stats(s.execute_read(stats))
            orphans = s.execute_read(count_orphans)
            print("고아 노드:", orphans or "없음")

        elif args.cmd == "delete-round":
            if not args.date:
                raise SystemExit("회차를 지정할 것: delete-round 2025-05-03")
            fn = (lambda tx: delete_round(tx, args.date, True)) if args.dry_run else \
                 (lambda tx: delete_round(tx, args.date, False))
            r = (s.execute_read if args.dry_run else s.execute_write)(fn)
            tag = "[dry-run] " if args.dry_run else ""
            print(f"{tag}회차 {args.date}: Keyword {r['deleted_keywords']}개 삭제, "
                  f"다른 회차에도 있는 {r['kept_shared_keywords']}개는 유지")
            if not args.dry_run:
                print("고아 정리:", r.get("pruned") or "없음")

        elif args.cmd == "prune":
            fn = lambda tx: prune_orphans(tx, args.dry_run)          # noqa: E731
            r = (s.execute_read if args.dry_run else s.execute_write)(fn)
            print(("[dry-run] " if args.dry_run else "") + f"고아 정리: {r or '없음'}")

        elif args.cmd == "reset":
            if not args.yes:
                raise SystemExit("전체 삭제다. 확실하면 --yes 를 붙일 것")
            print(f"전체 삭제: {s.execute_write(reset)}개 노드")


def _selftest() -> None:
    """실제 Neo4j 에 픽스처를 심고 검증. 없으면 skip."""
    try:
        drv = _driver()
        drv.verify_connectivity()
    except SystemExit:
        raise
    except Exception as e:                                            # noqa: BLE001
        print(f"Neo4j 접속 불가 — selftest 건너뜀 ({e})")
        return

    with drv, drv.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
        # k1 은 두 회차에, k2 는 A 회차에만, k3 는 B 회차에만
        s.run("""
        CREATE (dA:Date {value:'2026-01-01'}), (dB:Date {value:'2026-01-02'})
        CREATE (k1:Keyword {name:'공통',count:1}), (k2:Keyword {name:'A만',count:1}),
               (k3:Keyword {name:'B만',count:1})
        CREATE (k1)-[:RECORDED_ON]->(dA), (k1)-[:RECORDED_ON]->(dB)
        CREATE (k2)-[:RECORDED_ON]->(dA), (k3)-[:RECORDED_ON]->(dB)
        CREATE (tA:Tag {name:'A태그'}), (k2)-[:HAS_TAG]->(tA)
        CREATE (c:Combo {name:'A조합'}), (i:Ingredient {name:'A재료'})
        CREATE (k2)-[:IS_COMBO_WITH]->(c), (c)-[:INCLUDES]->(i)
        """)

        # dry-run 은 아무것도 지우지 않아야 한다
        before = s.execute_read(lambda tx: tx.run("MATCH (n) RETURN count(n) AS n").single()["n"])
        r = s.execute_read(lambda tx: delete_round(tx, "2026-01-01", True))
        after_dry = s.execute_read(lambda tx: tx.run("MATCH (n) RETURN count(n) AS n").single()["n"])
        assert before == after_dry, (before, after_dry)
        assert r["deleted_keywords"] == 1 and r["kept_shared_keywords"] == 1, r

        # 실제 삭제 — A 회차에만 있던 k2 만 사라지고 공통 k1 은 남아야 한다
        s.execute_write(lambda tx: delete_round(tx, "2026-01-01", False))
        names = {x["n"] for x in s.run("MATCH (k:Keyword) RETURN k.name AS n")}
        assert names == {"공통", "B만"}, names

        # 남은 k1 은 B 회차 기록만 갖는다
        dates = {x["d"] for x in s.run("MATCH (:Keyword)-[:RECORDED_ON]->(d:Date) RETURN d.value AS d")}
        assert dates == {"2026-01-02"}, dates

        # k2 에 매달려 있던 Tag/Combo/Ingredient 가 고아로 남지 않아야 한다
        leftover = {x["l"] for x in s.run(
            "MATCH (n) WHERE NOT ()-->(n) AND NOT n:Keyword RETURN DISTINCT labels(n)[0] AS l")}
        assert not leftover, f"고아가 남았다: {leftover}"
        assert s.run("MATCH (n:Ingredient) RETURN count(n) AS n").single()["n"] == 0

        # 없는 회차를 지워도 조용히 통과해야 한다
        r = s.execute_write(lambda tx: delete_round(tx, "1999-01-01", False))
        assert r["deleted_keywords"] == 0 and r["kept_shared_keywords"] == 0, r

        s.run("MATCH (n) DETACH DELETE n")

    print("cleanup 자체 검증 통과")


if __name__ == "__main__":
    main()
