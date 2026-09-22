"""
후보 밖 생성률 (grounding violation rate) 측정

graphrag_aq.py 는 두 프롬프트 모두에 제약을 건다:
    "❗ 주어진 키워드 외의 재료를 생성하면 안 됩니다."      (재료 질문)
    "❗ 제공된 키워드 외 메뉴를 생성하지 마세요."            (메뉴 질문)

저장된 답변(data/**/*graphrag*.jsonl)에는 그때 넘긴 후보 키워드가 같이 들어 있다.
그래서 API 재호출 없이 "실제로 후보 안에서만 생성했는가"를 셀 수 있다.

판정 기준 두 가지를 같이 낸다. 하나만 쓰면 기준 논쟁이 붙는다.
  strict  : 정규화(공백/대소문자 제거) 후 완전 일치해야 후보 안
  lenient : 후보 키워드가 생성 이름에 부분 문자열로 들어 있으면 후보 안
            ("크림" 후보 → "크림 아트" 생성 = 후보 안으로 인정)

실행:
    python GragpRAG/eval_grounding.py
    python GragpRAG/eval_grounding.py --json   # 기계 판독용
"""

from __future__ import annotations

import argparse
import glob
import json
import os

import sys
from typing import Any, Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATTERN  = os.path.join(BASE_DIR, "data", "**", "*graphrag*.jsonl")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 판정 기준의 단일 출처. graphrag_aq.py 의 런타임 격리도 같은 모듈을 쓴다 —
# 생성 시점과 측정 시점의 기준이 갈라지면 둘 다 못 믿게 된다.
from grounding import is_grounded, normalize as norm, strip_code_fence   # noqa: E402


def parse_answer(answer: Any) -> Tuple[Optional[List[str]], str]:
    """
    답변 → 생성된 이름 리스트.
    반환: (이름 리스트 or None, 사유)
    None 이면 이 줄은 집계에서 뺀다 — 산문 답변에서 이름을 억지로 뽑지 않는다.
    """
    if answer is None:
        return None, "answer 없음"

    # 문자열이면 JSON 인지 먼저 확인 (JSON 이 문자열로 저장된 경우가 있다)
    # 펜스 제거는 grounding.strip_code_fence 를 쓴다 — 생성 시점의 판정과 같은 기준이어야 한다.
    if isinstance(answer, str):
        try:
            answer = json.loads(strip_code_fence(answer))
        except (json.JSONDecodeError, ValueError):
            return None, "산문 답변 (구조화 안 됨)"

    if not isinstance(answer, list) or not answer:
        return None, "리스트 아님"

    names = []
    for item in answer:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        else:
            return None, "항목에 name 없음"
    return names, "ok"


def classify(name: str, candidates: List[str]) -> Tuple[bool, bool]:
    """(strict 기준 후보 안, lenient 기준 후보 안)"""
    return (is_grounded(name, candidates, strict=True),
            is_grounded(name, candidates, strict=False))


def main() -> None:
    ap = argparse.ArgumentParser(description="후보 밖 생성률 측정")
    ap.add_argument("--json", action="store_true", help="JSON 으로 출력")
    args = ap.parse_args()

    files = sorted(glob.glob(PATTERN, recursive=True))
    if not files:
        raise SystemExit(f"답변 파일 없음: {PATTERN}")

    rows: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for path in files:
        rel = os.path.relpath(path, BASE_DIR)
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                rec  = json.loads(line)
                cands = rec.get("keywords") or []
                names, why = parse_answer(rec.get("answer"))

                if names is None:
                    skipped.append({"file": rel, "line": lineno, "reason": why})
                    continue
                if not cands:
                    skipped.append({"file": rel, "line": lineno, "reason": "후보 키워드 없음"})
                    continue

                items = []
                for nm in names:
                    s, l = classify(nm, cands)
                    items.append({"name": nm, "in_strict": s, "in_lenient": l})
                rows.append({
                    "file": rel, "line": lineno,
                    "question": rec.get("question"),
                    "n_candidates": len(cands),
                    "candidates": cands,
                    "items": items,
                })

    total_items = sum(len(r["items"]) for r in rows)
    out_strict  = sum(1 for r in rows for i in r["items"] if not i["in_strict"])
    out_lenient = sum(1 for r in rows for i in r["items"] if not i["in_lenient"])
    ans_any_strict  = sum(1 for r in rows if any(not i["in_strict"]  for i in r["items"]))
    ans_any_lenient = sum(1 for r in rows if any(not i["in_lenient"] for i in r["items"]))

    summary = {
        "answers_evaluated": len(rows),
        "answers_skipped":   len(skipped),
        "items_total":       total_items,
        "items_out_strict":  out_strict,
        "items_out_lenient": out_lenient,
        "item_violation_rate_strict":  round(out_strict / total_items, 4) if total_items else None,
        "item_violation_rate_lenient": round(out_lenient / total_items, 4) if total_items else None,
        "answers_with_violation_strict":  ans_any_strict,
        "answers_with_violation_lenient": ans_any_lenient,
    }

    if args.json:
        print(json.dumps({"summary": summary, "rows": rows, "skipped": skipped},
                         ensure_ascii=False, indent=2))
        return

    print("=" * 66)
    print("  후보 밖 생성률 — GraphRAG 답변이 제공된 키워드 안에 머물렀는가")
    print("=" * 66)
    print(f"  평가 대상 답변 : {summary['answers_evaluated']}건")
    print(f"  제외된 답변    : {summary['answers_skipped']}건 (산문/무응답 — 이름 추출 불가)")
    print(f"  생성 항목 총   : {total_items}개")
    print()
    print(f"  [strict]  후보 밖 {out_strict}/{total_items}개 "
          f"({summary['item_violation_rate_strict']:.1%})  "
          f"— 위반 포함 답변 {ans_any_strict}/{len(rows)}건")
    print(f"  [lenient] 후보 밖 {out_lenient}/{total_items}개 "
          f"({summary['item_violation_rate_lenient']:.1%})  "
          f"— 위반 포함 답변 {ans_any_lenient}/{len(rows)}건")
    print()

    # graphrag_aq.py 는 "재료" 포함 여부로 Cypher 와 프롬프트를 갈라 쓴다.
    # 위반이 한쪽 경로에 몰려 있는지 봐야 원인을 좁힐 수 있다.
    print("  질문 경로별 (lenient = 후보 키워드를 하나도 안 쓴 진짜 후보 밖):")
    for label, pred in (("재료 경로", True), ("메뉴 경로", False)):
        sel = [r for r in rows if ("재료" in (r["question"] or "")) == pred]
        items = [i for r in sel for i in r["items"]]
        if not items:
            continue
        bad = sum(1 for i in items if not i["in_lenient"])
        print(f"    {label}  답변 {len(sel):>2}건 / 항목 {len(items):>3}개 "
              f"→ 후보 밖 {bad:>2}개 ({bad/len(items):.1%})")
    print()

    print("  질문별 (strict 기준):")
    for r in rows:
        bad = [i["name"] for i in r["items"] if not i["in_strict"]]
        mark = "✘" if bad else "✔"
        q = (r["question"] or "?")[:22]
        print(f"   {mark} {q:<24} {len(r['items'])}개 중 후보 밖 {len(bad)}개"
              + (f"  → {', '.join(bad[:4])}" if bad else ""))

    if skipped:
        print("\n  제외 내역:")
        agg: Dict[str, int] = {}
        for s in skipped:
            agg[s["reason"]] = agg.get(s["reason"], 0) + 1
        for reason, n in sorted(agg.items(), key=lambda x: -x[1]):
            print(f"    {reason:<28} {n}건")

    print("\n  주의: N이 작다. 이 수치는 저장된 답변 전수이지 통계적 표본이 아니다.")


if __name__ == "__main__":
    main()
