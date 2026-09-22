"""
후보 Cypher 수정이 후보 밖 생성률을 낮췄는가 — A/B 실측

## 왜 그냥 재측정하면 안 되는가

기존 수치(재료 경로 50.0%)는 **gpt-4** 로 잰 값이다. 비용 때문에 다른 모델로 다시 재면
차이가 Cypher 수정 때문인지 모델 때문인지 구분할 수 없다. 교란 변수가 생긴다.

그래서 재측정이 아니라 **A/B** 로 간다.

    A (before) : 옛 Cypher 후보  →  LLM
    B (after)  : 새 Cypher 후보  →  LLM

같은 모델, 같은 temperature, 같은 프롬프트, 같은 질문, 같은 반복 수.
**후보 목록만 다르다.** 두 팔의 차이는 전부 Cypher 에서 온다.

기존 50% 와의 직접 비교는 모델이 달라 성립하지 않는다. 그 값은 참고로만 적는다.

## 프롬프트는 복사하지 않는다

`graphrag_aq.py` 의 프롬프트를 베껴 오면 원본이 바뀔 때 갈라져서 "같은 조건"이
아니게 된다. 그래서 프롬프트 생성을 이 모듈의 `build_prompt()` 하나로 두고,
문구가 원본과 일치하는지 자체 검증에서 소스를 읽어 대조한다.

판정은 `grounding.is_grounded` 와 `eval_grounding.classify` 를 그대로 쓴다.
생성 시점과 측정 시점의 기준이 갈라지면 둘 다 못 믿게 된다.

## 사용

    set OPENAI_API_KEY=...        (또는 configs/.env)
    python GragpRAG/eval_grounding_ab.py --repeat 4 --model gpt-4o-mini
    python GragpRAG/eval_grounding_ab.py --selftest    # API·DB 없이
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Tuple

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from grounding import is_grounded                                    # noqa: E402
from eval_grounding import parse_answer                              # noqa: E402
from candidates import (                                             # noqa: E402
    INGREDIENT_CYPHER, INGREDIENT_FALLBACK_CYPHER,
    MENU_FALLBACK_CYPHER, LIMIT, parse_question, build_menu_cypher,
)

OUT_DIR = os.path.join(BASE_DIR, "eval")

QUESTIONS = [
    "최근 유행하는 재료",
    "SNS에서 핫한 메뉴",
    "카페 신메뉴 추천",
    "달콤한 메뉴가 필요할 때",
    "비주얼이 예쁜 메뉴",
    "계절 한정 디저트",
]

# 수정 전 재료 경로 Cypher. 대조군이 코드에 없으면 "고쳤다"를 검증할 수 없다.
OLD_INGREDIENT_CYPHER = """
MATCH (i:Ingredient)
OPTIONAL MATCH (e:Example)-[:CONTAINS_INGREDIENT]->(i)
WITH i, count(e) AS usage_count
RETURN i.name AS keyword
ORDER BY usage_count DESC
LIMIT $limit
"""


def build_prompt(question: str, keywords: List[str]) -> str:
    """`graphrag_aq.py` 와 같은 프롬프트. 문구 일치는 selftest 가 소스와 대조한다."""
    bullets = "\n".join(f"- {kw}" for kw in keywords)
    if "재료" in question:
        return (
            f"당신은 카페 재료 기획 전문가입니다.\n"
            f"다음은 사용 가능한 재료 키워드입니다. 이 키워드만 참고해서 '{question}'에 어울리는 재료 3~5개를 추천하세요.\n"
            f"❗ 주어진 키워드 외의 재료를 생성하면 안 됩니다.\n\n"
            f"{bullets}\n\n"
            f"출력 형식: JSON 배열 [{{\"name\": \"이름\", \"description\": \"설명\"}} ...]"
        )
    return (
        f"당신은 디저트 마케팅 전문가입니다.\n"
        f"다음은 사용 가능한 디저트 키워드입니다. 이 키워드만 참고해서 '{question}'에 어울리는 메뉴를 3~5개 추천하세요.\n"
        f"❗ 제공된 키워드 외 메뉴를 생성하지 마세요.\n\n"
        f"{bullets}\n\n"
        f"출력 형식: JSON 배열 [{{\"name\": \"이름\", \"description\": \"설명\"}} ...]"
    )


def fetch_candidates(tx, question: str, arm: str) -> List[str]:
    """arm='old' 면 재료 경로만 옛 Cypher 를 쓴다. 메뉴 경로는 수정 대상이 아니라 동일하다."""
    cond = parse_question(question)
    if cond["use_ingredient"]:
        cypher = OLD_INGREDIENT_CYPHER if arm == "old" else INGREDIENT_CYPHER
        rows = [r["keyword"] for r in tx.run(cypher, limit=LIMIT)]
        if not rows:
            rows = [r["keyword"] for r in tx.run(INGREDIENT_FALLBACK_CYPHER, limit=LIMIT)]
        return rows
    cypher, params = build_menu_cypher(cond)
    rows = [r["keyword"] for r in tx.run(cypher, **params)]
    if not rows:
        rows = [r["keyword"] for r in tx.run(MENU_FALLBACK_CYPHER, limit=LIMIT)]
    return rows


def score(rows: List[dict]) -> Dict[str, Any]:
    """생성 항목 단위 후보 밖 비율. eval_grounding.py 와 같은 기준."""
    items = 0
    out_lenient = 0
    out_strict = 0
    parse_fail = 0
    for r in rows:
        names, _ = parse_answer(r["answer"])
        if names is None:
            parse_fail += 1
            continue
        for nm in names:
            items += 1
            if not is_grounded(nm, r["keywords"], strict=False):
                out_lenient += 1
            if not is_grounded(nm, r["keywords"], strict=True):
                out_strict += 1
    return {
        "answers": len(rows), "parse_fail": parse_fail, "items": items,
        "out_lenient": out_lenient, "out_strict": out_strict,
        "rate_lenient": out_lenient / items if items else None,
        "rate_strict":  out_strict / items if items else None,
    }


def run_arm(session, llm, arm: str, repeat: int) -> Tuple[List[dict], List[str]]:
    rows: List[dict] = []
    seen_candidates: List[str] = []
    # langchain 0.1 이후 메시지 클래스가 langchain_core 로 옮겨졌다.
    # graphrag_aq.py 는 옛 경로(`langchain.schema`)를 쓰는데 최신 버전에는 없다.
    # 양쪽을 받아 어느 환경에서도 같은 조건으로 돌게 한다.
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
    except ImportError:                                            # pragma: no cover
        from langchain.schema import SystemMessage, HumanMessage

    for rep in range(repeat):
        for q in QUESTIONS:
            kws = session.execute_read(fetch_candidates, q, arm)
            if "재료" in q and not seen_candidates:
                seen_candidates = kws
            resp = llm.invoke([SystemMessage(content=build_prompt(q, kws)),
                               HumanMessage(content=q)])
            rows.append({"arm": arm, "rep": rep, "question": q,
                         "keywords": kws, "answer": resp.content})
            print(f"  [{arm}] rep{rep+1} {q}", flush=True)
    return rows, seen_candidates


def report(all_rows: List[dict], model: str, repeat: int, rounds: int,
           cands: Dict[str, List[str]], stamp: str) -> None:
    """채점과 출력. 저장된 원문으로 재채점할 때도 같은 경로를 탄다."""
    def subset(arm: str, ingredient: bool) -> Dict[str, Any]:
        return score([r for r in all_rows
                      if r["arm"] == arm and (("재료" in r["question"]) == ingredient)])

    print("\n" + "=" * 70)
    print(f"  후보 밖 생성률 A/B — model={model}, temp=0.3, 질문 {len(QUESTIONS)}개 × {repeat}회")
    print("=" * 70)
    print(f"  적재 회차 {rounds}개"
          + ("" if rounds == 1 else "   ⚠ count 누적으로 후보 순위가 달라질 수 있다"))
    if cands.get("old"):
        print(f"\n  재료 후보 (old): {', '.join(cands['old'][:5])} ...")
        print(f"  재료 후보 (new): {', '.join(cands['new'][:5])} ...")

    print(f"\n  {'경로':<12}{'팔':<6}{'답변':>5}{'항목':>6}{'후보밖':>7}{'비율':>9}{'파싱실패':>9}")
    print("  " + "-" * 57)
    result = {}
    for label, ing in (("재료 경로", True), ("메뉴 경로", False)):
        for arm in ("old", "new"):
            st = subset(arm, ing)
            result[f"{'ingredient' if ing else 'menu'}_{arm}"] = st
            rate = f"{st['rate_lenient']:.1%}" if st["rate_lenient"] is not None else "—"
            print(f"  {label:<12}{arm:<6}{st['answers']:>5}{st['items']:>6}"
                  f"{st['out_lenient']:>7}{rate:>9}{st['parse_fail']:>9}")

    for label, key in (("재료 경로", "ingredient"), ("메뉴 경로", "menu")):
        o, n = result[f"{key}_old"], result[f"{key}_new"]
        if o["rate_lenient"] is not None and n["rate_lenient"] is not None:
            d = n["rate_lenient"] - o["rate_lenient"]
            print(f"\n  {label} 변화: {o['rate_lenient']:.1%} → {n['rate_lenient']:.1%}  ({d:+.1%}p)")
    print("\n  메뉴 경로는 Cypher 를 고치지 않았으므로 두 팔이 같아야 한다 (통제군).")
    print("  기존 gpt-4 측정값(재료 50.0% / 메뉴 2.2%)과는 모델이 달라 직접 비교하지 않는다.")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = {"model": model, "temperature": 0.3, "repeat": repeat,
           "questions": QUESTIONS, "rounds_loaded": rounds,
           "ingredient_candidates": cands, "by_path": result, "measured_at": stamp}
    p = os.path.join(OUT_DIR, f"grounding_ab_{stamp}.json")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n  저장: {os.path.relpath(p, BASE_DIR)}")


def rescore(path: str) -> None:
    """저장된 원문으로 다시 채점한다 — 판정 로직을 고쳤을 때 API 를 다시 쓰지 않는다."""
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    meta_files = sorted(f for f in os.listdir(OUT_DIR) if f.startswith("grounding_ab_20"))
    model, repeat, rounds = "unknown", 0, 0
    if meta_files:
        m = json.load(open(os.path.join(OUT_DIR, meta_files[-1]), encoding="utf-8"))
        model, repeat, rounds = m.get("model", "unknown"), m.get("repeat", 0), m.get("rounds_loaded", 0)
    cands = {"old": next((r["keywords"] for r in rows if r["arm"] == "old" and "재료" in r["question"]), []),
             "new": next((r["keywords"] for r in rows if r["arm"] == "new" and "재료" in r["question"]), [])}
    report(rows, model, repeat, rounds, cands, datetime.now().strftime("%Y%m%d_%H%M%S"))


def main(repeat: int, model: str) -> None:
    from dotenv import load_dotenv
    from langchain_openai import ChatOpenAI
    from neo4j import GraphDatabase
    import logging
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

    load_dotenv(os.path.join(BASE_DIR, "configs", ".env"))
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY 없음 — 환경변수나 configs/.env 에 넣을 것")

    uri  = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    auth = (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password123"))
    driver = GraphDatabase.driver(uri, auth=auth)
    llm = ChatOpenAI(temperature=0.3, model=model)

    per_arm: Dict[str, dict] = {}
    all_rows: List[dict] = []
    cands: Dict[str, List[str]] = {}
    with driver.session() as s:
        rounds = s.execute_read(lambda tx: tx.run("MATCH (d:Date) RETURN count(d) AS n").single()["n"])
        for arm in ("old", "new"):
            rows, c = run_arm(s, llm, arm, repeat)
            per_arm[arm] = score(rows)
            cands[arm] = c
            all_rows += rows
    driver.close()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 원문을 먼저 남긴다 — 판정 로직을 고쳐도 API 를 다시 쓰지 않고 재채점할 수 있다.
    os.makedirs(OUT_DIR, exist_ok=True)
    raw = os.path.join(OUT_DIR, f"grounding_ab_raw_{stamp}.jsonl")
    with open(raw, "w", encoding="utf-8") as f:
        for r in all_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    report(all_rows, model, repeat, rounds, cands, stamp)
    print(f"  원문: {os.path.relpath(raw, BASE_DIR)}")


def selftest() -> None:
    """API·DB 없이 확인 가능한 것."""
    # 대조군이 실제로 옛 관계를 쓰고, 본 쿼리는 안 쓴다
    assert "CONTAINS_INGREDIENT" in OLD_INGREDIENT_CYPHER
    assert "CONTAINS_INGREDIENT" not in INGREDIENT_CYPHER

    # 프롬프트 문구가 원본과 같아야 "같은 조건"이 성립한다
    src = open(os.path.join(HERE, "graphrag_aq.py"), encoding="utf-8").read()
    for marker in ("주어진 키워드 외의 재료를 생성하면 안 됩니다",
                   "제공된 키워드 외 메뉴를 생성하지 마세요",
                   "카페 재료 기획 전문가", "디저트 마케팅 전문가"):
        assert marker in src, f"원본에 없는 문구: {marker}"
        assert marker in build_prompt("최근 유행하는 재료", ["딸기"]) + \
                         build_prompt("카페 신메뉴 추천", ["딸기"]), marker

    # 질문 목록이 원본과 같아야 한다
    for q in QUESTIONS:
        assert q in src, f"원본 질문 목록에 없음: {q}"

    # 경로 분기
    assert parse_question(QUESTIONS[0])["use_ingredient"] is True
    assert all(not parse_question(q)["use_ingredient"] for q in QUESTIONS[1:])

    # 채점 — 후보 밖이 잡히는가
    st = score([{"keywords": ["딸기", "크림"],
                 "answer": [{"name": "딸기 케이크"}, {"name": "망고 빙수"}]}])
    assert st["items"] == 2 and st["out_lenient"] == 1, st
    st = score([{"keywords": ["딸기"], "answer": "not json"}])
    assert st["parse_fail"] == 1 and st["items"] == 0, st

    print("eval_grounding_ab 자체 검증 통과")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="후보 Cypher 수정의 A/B 효과")
    ap.add_argument("--repeat", type=int, default=4, help="질문 세트 반복 횟수 (팔마다)")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rescore", metavar="RAW_JSONL",
                    help="저장된 원문으로 재채점 (API 호출 없음)")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.rescore:
        rescore(a.rescore)
    else:
        main(a.repeat, a.model)
