"""
재현성 검증 — 같은 입력으로 N회 돌려 산출물이 일치하는지 확인

## 왜 범위를 한정하는가

파이프라인 전체를 N회 돌려 "산출물 일치"를 주장할 수는 없다. LLM 단계가 4개이고
그중 3개가 temperature > 0 이며 캐시도 시드도 없다.

| 단계 | 모델 | temperature | 결정적? |
|---|---|---|---|
| `spark/spark_keyword.py` | — (TF-IDF) | — | **예** (같은 입력 18회 100% 일치 실측) |
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

## 두 질문을 섞지 말 것

- `compare` — **다른 회차의 산출물**을 비교한다. 입력이 서로 다르므로 불일치가 나와도
  잡의 비결정성이 아니라 **코퍼스 의존성**이다.
- `run` — **같은 입력**을 N회 넣어 잡 자체의 결정성을 본다. 입력은 여기서 만들지 않는다.
  `kafka/replay_to_kafka.py` 로 토픽에 **한 번만** 주입해 두고 그것을 N회 읽는다.
  회차마다 주입하면 "같은 입력"이 깨져 질문 자체가 성립하지 않는다.

## 실행

    # 이미 있는 산출물 2개 이상을 비교 (재실행 없음)
    python spark/verify_reproducibility.py compare data/8차/keywords.jsonl data/2025-05-03/keywords.jsonl

    # 같은 입력 N회 재실행 (Kafka 기동 명령은 docs/adr/0010-reproducibility-scope.md)
    python kafka/replay_to_kafka.py --source data/8차/keywords.jsonl
    python spark/verify_reproducibility.py run --times 10 --source data/8차/keywords.jsonl

## 측정 결과 (2026-09)

3개 코퍼스(575 / 1,105 / 1,365 문서) 합계 **18회 재실행, 전부 완전 일치.**
순서만 다른 건수 0. 원본은 `spark/bench_results/repro_run_*.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYWORDS   = os.path.join(BASE_DIR, "data", "keywords.jsonl")
RUNS_DIR   = os.path.join(BASE_DIR, "eval", "repro_runs")
BENCH_DIR  = os.path.join(BASE_DIR, "spark", "bench_results")
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


def cmd_compare(paths: List[str], out: str | None = None) -> None:
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
    records = []
    for p, d in loaded[1:]:
        r = compare(base, d)
        print_report(f"A = {os.path.relpath(base_path, BASE_DIR)}",
                     f"B = {os.path.relpath(p, BASE_DIR)}", r)
        records.append({"a": os.path.relpath(base_path, BASE_DIR),
                        "b": os.path.relpath(p, BASE_DIR), **r})
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

    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({
                "files": [{"path": os.path.relpath(p, BASE_DIR),
                           "digest": digest(d), "videos": len(d)} for p, d in loaded],
                "pairs": records,
            }, f, ensure_ascii=False, indent=2)
        print(f"\n  결과 저장 → {os.path.relpath(out, BASE_DIR)}")


def _injected_count(sources: List[str]) -> int:
    """주입된 문서 수. `replay_to_kafka.py --extra` 와 같은 규칙으로 센다 —
    video_id 중복은 앞 파일이 이기고 한 번만 주입된다."""
    seen = set()
    for p in sources:
        for line in open(p, encoding="utf-8"):
            if line.strip():
                seen.add(json.loads(line)["video_id"])
    return len(seen)


def cmd_run(times: int, sources: List[str] | None = None) -> None:
    """spark_keyword.py 를 times 회 실행하고 산출물을 보관 후 비교.

    입력은 여기서 만들지 않는다. Kafka 토픽에 **미리 한 번** 주입해 두고
    (`kafka/replay_to_kafka.py`) 그것을 N회 읽는다. 매 회차 주입하면 입력이 같다는
    보장이 없어져 재현성 질문 자체가 성립하지 않는다.
    `sources` 는 그때 주입한 파일들이고 결과 JSON 에 측정 조건으로만 남는다.
    """
    os.makedirs(RUNS_DIR, exist_ok=True)
    saved: List[str] = []
    runs: List[Dict[str, Any]] = []

    for i in range(1, times + 1):
        print(f"[{i}/{times}] spark_keyword.py 실행...")
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, SPARK_JOB], cwd=BASE_DIR,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        elapsed = time.perf_counter() - t0
        if proc.returncode != 0:
            print(proc.stdout[-1500:])
            print(proc.stderr[-1500:])
            raise SystemExit(f"{i}회차 실패 — Kafka 가 떠 있는지 확인할 것")
        if not os.path.isfile(KEYWORDS):
            raise SystemExit(f"산출물 없음: {KEYWORDS}")
        dst = os.path.join(RUNS_DIR, f"keywords_run{i}.jsonl")
        shutil.copy2(KEYWORDS, dst)
        saved.append(dst)
        d = load(dst)
        runs.append({"run": i, "seconds": round(elapsed, 3),
                     "videos": len(d), "digest": digest(d)})
        print(f"     → {os.path.relpath(dst, BASE_DIR)}  "
              f"{runs[-1]['digest']}  {elapsed:.1f}s")

    cmd_compare(saved)

    loaded = [load(p) for p in saved]
    base = loaded[0]
    pairs = []
    for i, d in enumerate(loaded[1:], start=2):
        r = compare(base, d)
        pairs.append({"a": 1, "b": i, "common": r["common"], "exact": r["exact"],
                      "set_equal": r["set_equal"], "order_only": r["order_only"],
                      "content_diff": r["content_diff"], "diffs": r["diffs"]})

    digests = sorted({r["digest"] for r in runs})
    result = {
        "args": {
            "times": times,
            "source": [os.path.relpath(s, BASE_DIR) for s in sources] if sources else None,
            "input_records": _injected_count(sources) if sources else None,
            "topic": "raw-youtube",
            "bootstrap": "localhost:9092",
        },
        "env": {
            "python": sys.version.split()[0],
            "pyspark": _pyspark_version(),
            "java_home": os.environ.get("JAVA_HOME"),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "runs": runs,
        "summary": {
            "distinct_digests": len(digests),
            "digests": digests,
            # 회차 전부가 같은 해시면 순서까지 완전히 같다는 뜻이다.
            "all_identical": len(digests) == 1,
            "pairs_vs_run1": pairs,
        },
    }
    os.makedirs(BENCH_DIR, exist_ok=True)
    out = os.path.join(BENCH_DIR,
                       f"repro_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  결과 저장 → {os.path.relpath(out, BASE_DIR)}")


def _pyspark_version() -> str | None:
    try:
        import pyspark
        return pyspark.__version__
    except Exception:
        return None


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
    c.add_argument("--out", default=None, help="비교 결과를 JSON 으로 저장할 경로")
    r = sub.add_parser("run", help="N회 재실행 후 비교 (Kafka 필요)")
    r.add_argument("--times", type=int, default=3)
    r.add_argument("--source", nargs="+", default=None,
                   help="Kafka 에 주입해 둔 파일들 (측정 조건 기록용, 주입은 하지 않는다)")
    sub.add_parser("selftest")

    a = ap.parse_args()
    if a.cmd == "compare":
        cmd_compare(a.paths, a.out)
    elif a.cmd == "run":
        cmd_run(a.times, a.source)
    else:
        selftest()
