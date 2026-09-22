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

현재 저장된 라벨은 Claude 가 위 기준으로 단독 판정한 것이다 (ANNOTATOR 참조).
사람이 다시 라벨링하면 ANNOTATOR 를 바꾸고 score 를 다시 돌릴 것 —
판정자가 누구냐가 이 지표의 일부다.

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

# 라벨을 누가 달았는지가 지표의 일부다. 사람이 다시 달면 이 값을 바꿀 것.
ANNOTATOR = "Claude (LLM 단일 판정, 사람 교차검증 없음)"

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
    print(f"        판정자: {ANNOTATOR}")

    out = {
        "criterion": CRITERION, "annotator": ANNOTATOR,
        "denominator_top_n": TOP_N, "seed": SEED,
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


def read_label_map(path: str) -> Dict[str, str]:
    """keyword → 'y'/'n'. 미기입은 넣지 않는다."""
    out: Dict[str, str] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            v = (row.get("label") or "").strip().lower()
            if v in ("y", "yes", "1", "o", "예"):
                out[row["keyword"]] = "y"
            elif v in ("n", "no", "0", "x", "아니오"):
                out[row["keyword"]] = "n"
    return out


def cmd_baseline() -> None:
    """
    LLM 이 값을 하는가 — 기준선 대조

    "정밀도 57.1% 가 좋은 값인가"는 목표치를 내가 정하면 답이 안 된다.
    비교 대상이 있어야 한다. 가장 정직한 대조군은 **LLM 을 빼는 것**이다.

        food_fillter.py 는 322개를 score_priority 로 정렬한 뒤 LLM 에 넘겨 49개를 받는다.
        LLM 을 빼고 같은 정렬의 상위 49개를 그냥 쓰면 정밀도가 얼마인가?

    같은 개수(49)를 내므로 공평한 대조다. LLM 이 이 값을 못 넘기면
    API 비용과 비결정성을 지불할 근거가 없다.

    추정 방식 — 층화(stratified)
        상위 49개는 두 층으로 나뉜다.
          ① LLM 도 고른 것  → 49건 전수 라벨이 있다
          ② LLM 이 버린 것  → 273건 중 무작위 100건 표본에 일부가 들어 있다
        ②는 무작위 표본이므로 그 안의 메뉴 비율을 층 전체에 적용한다.
        층별 개수로 가중해 합치면 상위 49개의 메뉴 비율이 나온다.

    ②의 표본이 작으면 추정이 흔들린다. 그래서 표본 크기를 같이 출력한다.
    """
    counter, top = build_pool()
    kept_set = {r["keyword"] for r in load_kept()}
    n = len(kept_set)                      # LLM 출력 개수와 같은 N 으로 자른다
    head = top[:n]

    kept_lab = read_label_map(KEPT_CSV)
    drop_lab = read_label_map(DROP_CSV)

    in_kept = [k for k in head if k in kept_set]
    in_drop = [k for k in head if k not in kept_set]

    # ① 통과 층 — 전수 라벨
    a_lab = [kept_lab[k] for k in in_kept if k in kept_lab]
    a_y   = a_lab.count("y")
    # ② 탈락 층 — 표본 라벨
    b_lab = [drop_lab[k] for k in in_drop if k in drop_lab]
    b_y   = b_lab.count("y")

    if not a_lab or not b_lab:
        raise SystemExit("라벨이 부족하다 — 먼저 두 CSV 의 label 칸을 채울 것")

    a_rate = a_y / len(a_lab)
    b_rate = b_y / len(b_lab)
    est_y  = len(in_kept) * a_rate + len(in_drop) * b_rate
    baseline = est_y / n

    llm_lab = list(kept_lab.values())
    llm_prec = llm_lab.count("y") / len(llm_lab)
    lo, hi = wilson(llm_lab.count("y"), len(llm_lab))

    print("=" * 66)
    print(f"  LLM 필터 vs 점수 정렬 기준선 (같은 출력 개수 N={n})")
    print("=" * 66)
    print(f"  판정자 : {ANNOTATOR}")
    print()
    print(f"  [기준선] LLM 없이 score_priority 상위 {n}개")
    print(f"    ① LLM 도 고른 것 {len(in_kept):>2}개 중 라벨 {len(a_lab):>2}개 → 메뉴 {a_y:>2}개 ({a_rate:.1%})")
    print(f"    ② LLM 이 버린 것 {len(in_drop):>2}개 중 라벨 {len(b_lab):>2}개 → 메뉴 {b_y:>2}개 ({b_rate:.1%})")
    print(f"    층화 추정 메뉴 {est_y:.1f}개 / {n}  →  정밀도 {baseline:.1%}")
    print()
    print(f"  [LLM 필터] {llm_lab.count('y')}/{len(llm_lab)}  →  정밀도 {llm_prec:.1%}  (95% CI {lo:.1%} ~ {hi:.1%})")
    print()
    gain = llm_prec - baseline
    print(f"  차이: {gain:+.1%}p"
          + ("   → LLM 이 기준선을 넘는다" if lo > baseline else
             "   → 기준선이 LLM 의 신뢰구간 안에 있다 (우위 불확실)"))
    print()
    print(f"  주의: ②는 표본 {len(b_lab)}개에서 외삽한 값이다. 표본이 작을수록 기준선이 흔들린다.")

    out = {
        "annotator": ANNOTATOR, "n": n,
        "baseline": {"precision": baseline, "est_menu": est_y,
                     "stratum_kept": {"size": len(in_kept), "labeled": len(a_lab),
                                      "menu": a_y, "rate": a_rate},
                     "stratum_dropped": {"size": len(in_drop), "labeled": len(b_lab),
                                         "menu": b_y, "rate": b_rate}},
        "llm": {"precision": llm_prec, "ci95": [lo, hi]},
        "gain_pp": gain,
        "llm_beats_baseline": lo > baseline,
    }
    path = os.path.join(EVAL_DIR, "filter_baseline.json")
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
    ap.add_argument("cmd", choices=["sample", "score", "baseline", "selftest"])
    a = ap.parse_args()
    {"sample": cmd_sample, "score": cmd_score,
     "baseline": cmd_baseline, "selftest": demo}[a.cmd]()
