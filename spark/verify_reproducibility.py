"""
재현성 검증 — 같은 입력으로 N회 돌려 산출물이 일치하는지 확인

## 왜 범위를 한정하는가

파이프라인 전체를 N회 돌려 "산출물 일치"를 주장할 수는 없다. LLM 단계가 4개이고
그중 3개가 temperature > 0 이며 캐시도 시드도 없다.

| 단계 | 모델 | temperature | 결정적? |
|---|---|---|---|
| `spark/spark_keyword.py` | — (TF-IDF) | — | **예** |
| `agent/food_fillter.py` | gpt-4 | 0 | 사실상 (보장은 아님) |
| `agent/keyword_combined.py` | gpt-4 | 0.3 | 아니오 |
| `agent/keyword_info_collector.py` | gpt-3.5-turbo | 0.2 | 아니오 |
| `GragpRAG/graphrag_aq.py` | gpt-4 | 0.3 | 아니오 |

temperature=0 도 완전한 결정성을 보장하지 않는다 (동일 프롬프트라도 백엔드 배치 구성에
따라 토큰이 갈릴 수 있다). 그래서 **LLM 앞단인 Spark TF-IDF 구간까지만** 검증한다.
이 구간이 흔들리면 뒤는 볼 것도 없고, 이 구간이 안정적이면 "비결정성은 LLM에서만 온다"고
말할 수 있다.

## 무엇을 비교하는가

`spark_keyword.py` 의 산출물 `data/keywords.jsonl` — video_id 별 키워드 8개.
비교는 순서까지 포함한 완전 일치와, 집합 기준 일치를 따로 낸다.
TF-IDF 동점 처리나 파티션 순서 때문에 순서만 갈리는 경우를 구분해야 원인을 좁힐 수 있다.

## 실행

    # 이미 있는 산출물 2개 이상을 비교 (재실행 없음)
    python spark/verify_reproducibility.py compare data/8차/keywords.jsonl data/2025-05-03/keywords.jsonl

    # Kafka 가 떠 있다면 N회 재실행 후 비교
    python spark/verify_reproducibility.py run --times 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Tuple

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYWORDS   = os.path.join(BASE_DIR, "data", "keywords.jsonl")
RUNS_DIR   = os.path.join(BASE_DIR, "eval", "repro_runs")
SPARK_JOB  = os.path.join(BASE_DIR, "spark", "spark_keyword.py")


def load(path: str) -> Dict[str, List[str]]:
    """video_id → keywords. 같은 id 가 두 번 나오면 마지막 것을 쓴다."""
    out: Dict[str, List[str]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            out[rec["video_id"]] = list(rec.get("keywords", []))
    return out


def digest(data: Dict[str, List[str]]) -> str:
    """순서까지 포함한 내용 해시. video_id 정렬로 파일 줄 순서는 무시한다."""
    h = hashlib.sha256()
    for vid in sorted(data):
        h.update(vid.encode())
        h.update("\x1f".join(data[vid]).encode())
        h.update(b"\x1e")
    return h.hexdigest()[:16]


def compare(a: Dict[str, List[str]], b: Dict[str, List[str]]) -> Dict[str, Any]:
    ids_a, ids_b = set(a), set(b)
    common = ids_a & ids_b

    exact = sum(1 for v in common if a[v] == b[v])            # 순서까지 동일
    as_set = sum(1 for v in common if set(a[v]) == set(b[v])) # 집합만 동일
    order_only = as_set - exact

    diffs = []
    for v in sorted(common):
        if a[v] != b[v]:
            diffs.append({
                "video_id": v,
                "only_in_a": [k for k in a[v] if k not in b[v]],
                "only_in_b": [k for k in b[v] if k not in a[v]],
                "order_only": set(a[v]) == set(b[v]),
            })

    return {
        "ids_a": len(ids_a), "ids_b": len(ids_b), "common": len(common),
        "only_a": sorted(ids_a - ids_b)[:5], "only_b": sorted(ids_b - ids_a)[:5],
        "n_only_a": len(ids_a - ids_b), "n_only_b": len(ids_b - ids_a),
        "exact": exact, "set_equal": as_set, "order_only": order_only,
        "content_diff": len(common) - as_set,
        "diffs": diffs[:10],
    }


def print_report(label_a: str, label_b: str, r: Dict[str, Any]) -> None:
    print(f"\n  {label_a}")
    print(f"  {label_b}")
    print(f"    영상 수      : {r['ids_a']} vs {r['ids_b']}  (공통 {r['common']})")
    if r["n_only_a"] or r["n_only_b"]:
        print(f"    한쪽에만     : A {r['n_only_a']}개 / B {r['n_only_b']}개")
    if not r["common"]:
        print("    → 공통 영상이 없다. 다른 수집분이라 재현성 비교 대상이 아니다.")
        return
    c = r["common"]
    print(f"    완전 일치    : {r['exact']}/{c} ({r['exact']/c:.1%})")
    print(f"    집합 일치    : {r['set_equal']}/{c} ({r['set_equal']/c:.1%})"
          f"   (순서만 다름 {r['order_only']})")
    print(f"    내용 다름    : {r['content_diff']}/{c}")
    for d in r["diffs"][:3]:
        tag = "순서만" if d["order_only"] else "내용"
        print(f"      [{tag}] {d['video_id']}  -A{d['only_in_a'][:2]} +B{d['only_in_b'][:2]}")


def cmd_compare(paths: List[str]) -> None:
    if len(paths) < 2:
        raise SystemExit("파일을 2개 이상 지정할 것")
    loaded = [(p, load(p)) for p in paths]

    print("=" * 70)
    print("  재현성 검증 — Spark TF-IDF 산출물 (LLM 앞단)")
    print("=" * 70)
    for p, d in loaded:
        print(f"  {digest(d)}  {len(d):>5}개 영상  {os.path.relpath(p, BASE_DIR)}")

    base_path, base = loaded[0]
    all_same = True
    for p, d in loaded[1:]:
        r = compare(base, d)
        print_report(f"A = {os.path.relpath(base_path, BASE_DIR)}",
                     f"B = {os.path.relpath(p, BASE_DIR)}", r)
        if r["common"] and r["exact"] != r["common"]:
            all_same = False

    # 입력 코퍼스가 다르면 "비결정성"이 아니라 "코퍼스 의존성"이다. 구분해서 말해야 한다.
    different_corpus = any(
        compare(base, d)["n_only_a"] or compare(base, d)["n_only_b"]
        for _, d in loaded[1:]
    )

    print()
    if all_same and len(loaded) > 1:
        print("  → 비교한 산출물이 완전히 일치한다.")
    elif different_corpus:
        print("  → 일치하지 않는다. 단 **입력 코퍼스가 서로 다르다** (영상 집합이 겹치지 않음).")
        print("     TF-IDF 의 IDF 는 배치 전체 문서에서 계산되므로, 같은 영상이라도")
        print("     그 주에 무엇이 같이 수집됐느냐에 따라 상위 8개 키워드가 달라진다.")
        print("     즉 이건 Spark 잡의 비결정성이 아니라 **배치 TF-IDF 의 코퍼스 의존성**이다.")
        print("     잡 자체의 결정성을 보려면 같은 입력으로 돌려야 한다 → `run` 서브커맨드.")
    else:
        print("  → 같은 입력인데 일치하지 않는다. Spark 잡 자체가 비결정적이다. 위 diff 참조.")

    print("\n  검증 범위는 LLM 앞단까지다. 뒤쪽 LLM 단계 3개는 temperature > 0 이라")
    print("  회차 간 일치가 성립하지 않는다 (모듈 docstring 표 참조).")


def cmd_run(times: int) -> None:
    """spark_keyword.py 를 times 회 실행하고 산출물을 보관 후 비교."""
    os.makedirs(RUNS_DIR, exist_ok=True)
    saved = []
    for i in range(1, times + 1):
        print(f"[{i}/{times}] spark_keyword.py 실행...")
        proc = subprocess.run([sys.executable, SPARK_JOB], cwd=BASE_DIR,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            print(proc.stdout[-1500:])
            print(proc.stderr[-1500:])
            raise SystemExit(f"{i}회차 실패 — Kafka 가 떠 있는지 확인할 것")
        if not os.path.isfile(KEYWORDS):
            raise SystemExit(f"산출물 없음: {KEYWORDS}")
        dst = os.path.join(RUNS_DIR, f"keywords_run{i}.jsonl")
        shutil.copy2(KEYWORDS, dst)
        saved.append(dst)
        print(f"     → {os.path.relpath(dst, BASE_DIR)}")
    cmd_compare(saved)


def selftest() -> None:
    """비교 로직 자체 검증."""
    a = {"v1": ["크림", "빵"], "v2": ["초코", "칩"]}
    same = {"v1": ["크림", "빵"], "v2": ["초코", "칩"]}
    order = {"v1": ["빵", "크림"], "v2": ["초코", "칩"]}
    content = {"v1": ["크림", "우유"], "v2": ["초코", "칩"]}

    r = compare(a, same)
    assert r["exact"] == 2 and r["order_only"] == 0 and r["content_diff"] == 0, r

    r = compare(a, order)
    assert r["exact"] == 1, r          # v2 만 순서까지 같다
    assert r["set_equal"] == 2, r      # v1 은 집합으로는 같다
    assert r["order_only"] == 1, r
    assert r["content_diff"] == 0, r

    r = compare(a, content)
    assert r["exact"] == 1 and r["set_equal"] == 1 and r["content_diff"] == 1, r

    # 한쪽에만 있는 영상은 공통에서 빠진다
    r = compare(a, {"v1": ["크림", "빵"]})
    assert r["common"] == 1 and r["n_only_a"] == 1, r

    # 같은 내용이면 해시도 같고, 순서가 바뀌면 달라야 한다
    assert digest(a) == digest(same)
    assert digest(a) != digest(order)

    print("verify_reproducibility 자체 검증 통과")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Spark 산출물 재현성 검증")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare", help="기존 산출물끼리 비교")
    c.add_argument("paths", nargs="+")
    r = sub.add_parser("run", help="N회 재실행 후 비교 (Kafka 필요)")
    r.add_argument("--times", type=int, default=3)
    sub.add_parser("selftest")

    a = ap.parse_args()
    if a.cmd == "compare":
        cmd_compare(a.paths)
    elif a.cmd == "run":
        cmd_run(a.times)
    else:
        selftest()
