"""
메뉴 키워드 필터 정밀도/재현율 측정

food_fillter.py 의 LLM 필터가 얼마나 정확한지 재려면 라벨이 필요한데,
라벨을 만들 사람이 프로젝트 주인뿐이다. 그래서 전수가 아니라 표본으로 간다.
이 스크립트는 **표본을 뽑고 지표를 계산할 뿐, 판정은 하지 않는다.**

분모 주의 — 흔한 착각
    data/8차/keywords.jsonl 은 575줄이지만 그건 **영상 수**다.
    food_fillter.py 의 실제 흐름은 이렇다:
        영상 575줄 → 키워드 2,507개(고유 852)
        → Okt 명사 + 2~3gram 추출, 금칙어 제거 → phrase_counter = **322개**
        → 점수순 정렬 후 상위 500 컷 (풀이 322라 전부 통과)
        → LLM 이 골라낸 것 중 phrase_counter 에 있는 것만 저장 → 49개
    따라서 필터의 분모는 **322**, 버린 것은 **273**, 잔존율은 **15.2%** 다.
    "575 - 49 = 526" 도 "잔존율 8.5%" 도 성립하지 않는다.

판정 기준 (라벨링할 때 이 한 줄만 보면 된다)
    ▶ "이 문구가 카페 메뉴판에 단독 품목으로 올라갈 수 있는가?"
      예: "딸기 티라미수" → 예 / "초코" → 아니오(재료·일반명사) /
          "먹방 리뷰" → 아니오 / "모찌 롤" → 예
    기준이 문서에 적혀 있으면 주관적 라벨도 방어된다.

사용
    1) 표본 뽑기 (라벨 칸은 비어 있다)
       python agent/eval_filter.py sample
       → eval/kept_49.csv         남긴 것 전수
       → eval/dropped_sample.csv  버린 것에서 무작위 100개

    2) 두 CSV 의 label 칸을 y / n 으로 채운다 (엑셀 등)

    3) 지표 계산
       python agent/eval_filter.py score

JAVA_HOME 이 Java 9+ 를 가리켜야 한다 (Okt/JPype 요구사항).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from collections import Counter
from typing import Dict, List, Tuple

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYWORDS   = os.path.join(BASE_DIR, "data", "8차", "keywords.jsonl")
FILTERED   = os.path.join(BASE_DIR, "data", "8차", "filtered_keywords_with_count.jsonl")
EVAL_DIR   = os.path.join(BASE_DIR, "eval")
KEPT_CSV   = os.path.join(EVAL_DIR, "kept_49.csv")
DROP_CSV   = os.path.join(EVAL_DIR, "dropped_sample.csv")

TOP_N        = 500     # food_fillter.py 의 sorted_phrases[:500] 과 같아야 한다
SAMPLE_N     = 100
SEED         = 20260921

CRITERION = "카페 메뉴판에 단독 품목으로 올라갈 수 있는가? (y/n)"

# food_fillter.py 에서 그대로 가져온다 — 재현이 목적이라 값이 갈라지면 안 된다
sys.path.insert(0, os.path.join(BASE_DIR, "agent"))


def _stub_llm_imports() -> None:
    """
    food_fillter.py 는 모듈 상단에서 langchain/langsmith/dotenv 를 import 한다.
    우리가 쓰는 extract_noun_phrases_with_counts 는 Okt 만 있으면 되는 순수 함수라,
    LLM 스택을 설치하는 대신 없는 모듈만 껍데기로 막는다.
    함수를 복사해 오면 원본과 갈라져서 '재현'이 아니게 된다 — 그래서 실제 모듈을 쓴다.
    """
    import types

    def put(name: str, **attrs) -> None:
        if name in sys.modules:
            return
        try:
            __import__(name)
            return
        except ImportError:
            pass
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod

    noop = lambda *a, **k: None                                    # noqa: E731
    put("dotenv", load_dotenv=noop)
    put("langchain_openai", ChatOpenAI=object)
    put("langchain", __path__=[])
    put("langchain.schema", SystemMessage=object, HumanMessage=object)
    put("langsmith", traceable=lambda *a, **k: (lambda f: f))


def build_pool() -> Tuple[Counter, List[str]]:
    """food_fillter.py 의 추출 + 정렬을 그대로 재현해 (phrase_counter, top500) 반환."""
    _stub_llm_imports()
    from food_fillter import extract_noun_phrases_with_counts   # noqa: E402

    counter = extract_noun_phrases_with_counts(KEYWORDS)

    # food_fillter.py 의 score_priority 와 동일해야 한다
    def score_priority(k: str) -> float:
        return counter[k] * (1.5 if " " in k else 1)

    top = sorted(counter.keys(), key=score_priority, reverse=True)[:TOP_N]
    return counter, top


def load_kept() -> List[Dict[str, object]]:
    rows = []
    with open(FILTERED, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_csv(path: str, rows: List[Dict[str, object]], fields: List[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # utf-8-sig — 엑셀이 BOM 없으면 한글을 깨뜨린다
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def cmd_sample() -> None:
    counter, top = build_pool()
    kept = load_kept()
    kept_set = {r["keyword"] for r in kept}

    print(f"후보 풀 (phrase_counter) : {len(counter):,}개")
    print(f"LLM 입력 (상위 {TOP_N} 컷)  : {len(top)}개"
          + ("  ← 풀 전체 (컷에 안 걸림)" if len(top) == len(counter) else ""))
    print(f"LLM 통과 (저장된 것)      : {len(kept)}개")

    missing = [k for k in kept_set if k not in set(top)]
    if missing:
        print(f"⚠️  통과 키워드 중 상위 {TOP_N} 밖: {len(missing)}개 → {missing[:5]}")
        print("    (food_fillter.py 실행 시점과 데이터가 달라졌을 수 있다)")

    dropped = [k for k in top if k not in kept_set]
    print(f"버린 것 (후보 - 통과)     : {len(dropped)}개   "
          f"잔존율 {len(kept)/len(top):.1%}")

    rnd = random.Random(SEED)
    sample = rnd.sample(dropped, min(SAMPLE_N, len(dropped)))

    rank = {k: i + 1 for i, k in enumerate(top)}
    write_csv(KEPT_CSV,
              [{"keyword": r["keyword"], "count": r.get("count", counter.get(r["keyword"], 0)),
                "rank_in_top500": rank.get(r["keyword"], ""), "label": ""} for r in kept],
              ["keyword", "count", "rank_in_top500", "label"])
    write_csv(DROP_CSV,
              [{"keyword": k, "count": counter[k], "rank_in_top500": rank[k], "label": ""}
               for k in sample],
              ["keyword", "count", "rank_in_top500", "label"])

    print()
    print(f"→ {os.path.relpath(KEPT_CSV, BASE_DIR)}  ({len(kept)}행, 전수)")
    print(f"→ {os.path.relpath(DROP_CSV, BASE_DIR)}  ({len(sample)}행, seed={SEED})")
    print()
    print(f"판정 기준: {CRITERION}")
    print("label 칸을 y / n 으로 채운 뒤 `python agent/eval_filter.py score` 실행")


def read_labels(path: str) -> Tuple[int, int, int]:
    """(y 개수, n 개수, 미기입 개수)"""
    y = n = blank = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            v = (row.get("label") or "").strip().lower()
            if v in ("y", "yes", "1", "o", "예"):
                y += 1
            elif v in ("n", "no", "0", "x", "아니오"):
                n += 1
            else:
                blank += 1
    return y, n, blank


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """이항비율 95% 신뢰구간 (Wilson). 표본이 작아서 정규근사는 부적절하다."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (max(0.0, (c - s) / d), min(1.0, (c + s) / d))


def cmd_score() -> None:
    for p in (KEPT_CSV, DROP_CSV):
        if not os.path.isfile(p):
            raise SystemExit(f"{p} 없음 — 먼저 `python agent/eval_filter.py sample`")

    tp, fp, kept_blank = read_labels(KEPT_CSV)     # 남긴 것: y=진짜 메뉴(TP), n=오통과(FP)
    fn_s, tn_s, drop_blank = read_labels(DROP_CSV) # 버린 것: y=사실 메뉴였음(FN), n=올바른 제거(TN)

    if kept_blank or drop_blank:
        print(f"⚠️  미기입 — 남긴 것 {kept_blank}행 / 버린 것 {drop_blank}행. 그만큼 빼고 계산한다.\n")

    kept_n   = tp + fp
    sample_n = fn_s + tn_s
    if kept_n == 0 or sample_n == 0:
        raise SystemExit("라벨이 없다. CSV 의 label 칸을 채울 것.")

    counter, top = build_pool()
    kept_total    = len(load_kept())
    dropped_total = len(top) - kept_total

    precision = tp / kept_n
    p_lo, p_hi = wilson(tp, kept_n)

    # 버린 것 중 '사실은 메뉴' 비율 → 전체 버린 것으로 외삽해 놓친 개수를 추정
    miss_rate  = fn_s / sample_n
    m_lo, m_hi = wilson(fn_s, sample_n)
    fn_est     = miss_rate * dropped_total
    fn_lo, fn_hi = m_lo * dropped_total, m_hi * dropped_total

    # 재현율 = TP / (TP + FN). FN 이 구간 추정이라 재현율도 구간이 된다
    def recall(fn: float) -> float:
        return tp / (tp + fn) if (tp + fn) else 0.0

    print("=" * 62)
    print("  메뉴 키워드 필터 정밀도 / 재현율")
    print("=" * 62)
    print(f"  판정 기준 : {CRITERION}")
    print(f"  분모      : LLM 이 실제로 본 후보 {len(top)}개 "
          f"(sorted_phrases[:{TOP_N}] — 풀이 {TOP_N}보다 작으면 전부)")
    print(f"              영상 575줄도, 고유 키워드 852개도 아니다")
    print()
    print(f"  [남긴 것] 전수 {kept_n}/{kept_total}건 라벨링")
    print(f"    실제 메뉴  {tp:>3}건   오통과 {fp:>3}건")
    print(f"    정밀도 = {precision:.1%}   (95% CI {p_lo:.1%} ~ {p_hi:.1%})")
    print()
    print(f"  [버린 것] 전체 {dropped_total}건 중 무작위 {sample_n}건 라벨링 (seed={SEED})")
    print(f"    사실은 메뉴였음(오탈락) {fn_s:>3}건   올바른 제거 {tn_s:>3}건")
    print(f"    오탈락률 = {miss_rate:.1%}  (95% CI {m_lo:.1%} ~ {m_hi:.1%})")
    print(f"    → 전체 버린 것 {dropped_total}건으로 외삽: 놓친 메뉴 약 {fn_est:.0f}건 "
          f"({fn_lo:.0f} ~ {fn_hi:.0f})")
    print()
    print(f"  재현율 = {recall(fn_est):.1%}   (구간 {recall(fn_hi):.1%} ~ {recall(fn_lo):.1%})")
    print()
    print("  주의: 재현율은 표본 외삽이라 점추정이 아니라 구간으로 읽어야 한다.")
    print("        라벨은 단일 판정자 기준이다 (교차 검증 없음).")

    out = {
        "criterion": CRITERION, "denominator_top_n": TOP_N, "seed": SEED,
        "kept": {"labeled": kept_n, "total": kept_total, "true_menu": tp, "false_pass": fp,
                 "precision": precision, "precision_ci95": [p_lo, p_hi]},
        "dropped": {"labeled": sample_n, "total": dropped_total,
                    "missed": fn_s, "correct": tn_s,
                    "miss_rate": miss_rate, "miss_rate_ci95": [m_lo, m_hi],
                    "missed_est": fn_est, "missed_est_ci95": [fn_lo, fn_hi]},
        "recall_point": recall(fn_est),
        "recall_ci95": [recall(fn_hi), recall(fn_lo)],
    }
    path = os.path.join(EVAL_DIR, "filter_metrics.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  저장: {os.path.relpath(path, BASE_DIR)}")


def demo() -> None:
    """Wilson 구간 자체 검증 — 지표 계산이 틀리면 전부 무의미해진다."""
    lo, hi = wilson(49, 49)
    assert 0.90 < lo < 1.0 and hi > 0.999, (lo, hi)   # hi 는 부동소수라 1.0 과 정확히 같지 않다
    lo, hi = wilson(0, 100)
    assert lo == 0.0 and 0.0 < hi < 0.05, (lo, hi)
    lo, hi = wilson(50, 100)
    assert lo < 0.5 < hi and 0.39 < lo < 0.41, (lo, hi)
    assert wilson(0, 0) == (0.0, 0.0)
    print("wilson 자체 검증 통과")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="메뉴 키워드 필터 정밀도/재현율")
    ap.add_argument("cmd", choices=["sample", "score", "selftest"])
    a = ap.parse_args()
    {"sample": cmd_sample, "score": cmd_score, "selftest": demo}[a.cmd]()
