"""
gpt-4 를 로컬 오픈소스 LLM 으로 바꿀 수 있는가 — 모델·디코딩 방식 비교

## 왜 이 비교가 필요한가

공공·금융 폐쇄망에서는 외부 API(gpt-4)를 못 쓴다. 로컬 모델로 바꿨을 때
  (1) 후보 밖 생성률이 gpt-4 보다 나빠지는가
  (2) 구조화 출력(Ollama `format` 문법 제약)이 스키마 실패를 없애는가
를 추측이 아니라 실측으로 답한다.

## 같은 조건을 어떻게 만드는가

OpenAI 키 없이 gpt-4 를 다시 부를 수 없다. 그래서 gpt-4 쪽은 **저장된 원문**
(`eval/grounding_ab_raw_*.jsonl`, temperature 0.3)을 재채점한다.
로컬 모델은 그 원문에 들어 있는 **고유 (질문, 후보 목록) 쌍**을 그대로 입력으로 쓴다.
후보 목록이 같으니 Neo4j 를 다시 읽지 않는다. 바뀌는 변수는 모델과 디코딩 방식뿐이다.

원문 파일 하나(225151)는 gpt-4o-mini 산출물이다 (같은 시각대 요약 파일의 model 값,
그리고 전 답변이 마크다운 펜스로 감싸져 있다 — grounding.strip_code_fence 문서 참고).
gpt-4 기준선에 섞으면 기준선이 오염되므로 모델별로 따로 집계한다.

프롬프트와 호출은 `generate.py` 를 그대로 쓴다. 여기서 프롬프트를 베끼면 비교 결과를
서비스에 옮길 수 없다. 판정은 `grounding.validate_answer`(lenient, 런타임 기준)와
`grounding.is_grounded(strict=True)` 를 그대로 쓴다.

## 측정 정의

  - 스키마 실패: validate_answer 사유가 json_parse_error / not_a_list / missing_name_field
  - 후보 밖(항목 단위): 파싱된 이름 하나하나를 is_grounded 로 판정. 분모는 항목 수
  - 거부(답변 단위): validate_answer 가 False 인 답변. 런타임에서 격리되는 비율
  - 지연: generate() 한 번의 벽시계 시간. 워밍업 1회는 집계에서 뺀다
  - 구간: 95% Wilson. 항목은 한 답변 안에서 독립이 아니므로 항목 단위 구간은 낙관적이다.
    그래서 답변 단위 비율을 함께 낸다.

## 사용

    python GragpRAG/eval_models.py baseline
    python GragpRAG/eval_models.py run --model qwen2.5:7b --mode structured --repeat 3
    python GragpRAG/eval_models.py table
    python GragpRAG/eval_models.py --selftest     # 모델 호출 없음
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Tuple

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(HERE)
OUT_DIR = os.path.join(BASE_DIR, "eval")
sys.path.insert(0, HERE)

from grounding import is_grounded, validate_answer                  # noqa: E402
from eval_grounding import parse_answer                              # noqa: E402

TEMPERATURE = 0.3
HARDWARE = {"cpu": "AMD Ryzen 7 7800X3D", "gpu": "NVIDIA RTX 4070 SUPER 12GB"}
SCHEMA_FAIL = ("json_parse_error", "not_a_list", "missing_name_field")


def wilson(k: int, n: int, z: float = 1.96) -> List[float]:
    if n == 0:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def path_of(question: str) -> str:
    return "ingredient" if "재료" in question else "menu"


def score_one(answer: Any, keywords: List[str]) -> Dict[str, Any]:
    """답변 하나 채점. 기준선과 로컬 실행이 이 함수 하나를 지난다."""
    ok, why = validate_answer(answer, keywords)
    reason = "ok" if ok else str(why).split(":")[0]
    names, _ = parse_answer(answer)
    names = names or []
    return {"ok": ok, "reason": reason, "detail": None if ok else why, "names": names,
            "out_lenient": [n for n in names if not is_grounded(n, keywords)],
            "out_strict": [n for n in names if not is_grounded(n, keywords, strict=True)]}


def aggregate(rows: List[dict]) -> Dict[str, Any]:
    """rows: question, keywords, score(score_one 결과), latency_s(선택)."""
    def agg(rs: List[dict]) -> Dict[str, Any]:
        n = len(rs)
        reasons = Counter(r["score"]["reason"] for r in rs)
        schema = sum(reasons[k] for k in SCHEMA_FAIL)
        errors = sum(v for k, v in reasons.items() if k.startswith("call_error"))
        rejected = n - reasons["ok"]
        items = sum(len(r["score"]["names"]) for r in rs)
        ol = sum(len(r["score"]["out_lenient"]) for r in rs)
        os_ = sum(len(r["score"]["out_strict"]) for r in rs)
        parsed = [len(r["score"]["names"]) for r in rs if r["score"]["names"]]
        ans_ol = sum(1 for r in rs if r["score"]["out_lenient"])
        return {
            "answers": n, "reasons": dict(reasons),
            "schema_fail": {"k": schema, "n": n, "rate": schema / n if n else None, "ci95": wilson(schema, n)},
            "call_error": errors,
            "rejected": {"k": rejected, "n": n, "rate": rejected / n if n else None, "ci95": wilson(rejected, n)},
            "out_lenient_items": {"k": ol, "n": items, "rate": ol / items if items else None, "ci95": wilson(ol, items)},
            "out_strict_items": {"k": os_, "n": items, "rate": os_ / items if items else None, "ci95": wilson(os_, items)},
            "answers_with_out_lenient": {"k": ans_ol, "n": len(parsed),
                                         "rate": ans_ol / len(parsed) if parsed else None,
                                         "ci95": wilson(ans_ol, len(parsed))},
            "items_per_answer": {"mean": round(sum(parsed) / len(parsed), 2) if parsed else None,
                                 "min": min(parsed, default=None), "max": max(parsed, default=None),
                                 "outside_3to5": sum(1 for c in parsed if not 3 <= c <= 5)},
        }

    out = {"all": agg(rows)}
    for p in ("ingredient", "menu"):
        out[p] = agg([r for r in rows if path_of(r["question"]) == p])
    lat = sorted(r["latency_s"] for r in rows if r.get("latency_s") is not None)
    if lat:
        def q(p: float) -> float:          # nearest-rank
            return round(lat[max(0, math.ceil(p * len(lat)) - 1)], 2)
        p50 = q(0.5)
        out["latency_s"] = {"n": len(lat), "p50": p50, "p95": q(0.95), "min": round(lat[0], 2),
                            "max": round(lat[-1], 2),
                            "over_3x_p50": [round(x, 2) for x in lat if x > 3 * p50]}
    return out


# ---------------------------------------------------------------- 기준선 (저장된 원문)

def raw_files_by_model() -> Dict[str, List[str]]:
    """원문 파일의 모델은 같은 stamp 의 요약 파일에서 읽는다. 없으면 그 뒤 첫 요약(재채점 산출물)."""
    summaries = sorted(glob.glob(os.path.join(OUT_DIR, "grounding_ab_2*.json")))
    stamps = [(os.path.basename(s)[len("grounding_ab_"):-5], s) for s in summaries]
    out: Dict[str, List[str]] = {}
    for f in sorted(glob.glob(os.path.join(OUT_DIR, "grounding_ab_raw_*.jsonl"))):
        st = os.path.basename(f)[len("grounding_ab_raw_"):-6]
        match = [s for t, s in stamps if t == st] or [s for t, s in stamps if t > st][:1]
        model = json.load(open(match[0], encoding="utf-8"))["model"] if match else "unknown"
        out.setdefault(model, []).append(f)
    return out


def load_rows(files: List[str]) -> List[dict]:
    return [json.loads(l) for f in files for l in open(f, encoding="utf-8") if l.strip()]


def unique_pairs() -> List[Tuple[str, List[str]]]:
    seen: Dict[Tuple[str, tuple], None] = {}
    for files in raw_files_by_model().values():
        for r in load_rows(files):
            seen.setdefault((r["question"], tuple(r["keywords"])), None)
    return [(q, list(k)) for q, k in seen]


def baseline() -> None:
    res: Dict[str, Any] = {"source": "eval/grounding_ab_raw_*.jsonl (저장된 원문 재채점, API 호출 없음)",
                           "temperature": TEMPERATURE, "unique_pairs": len(unique_pairs()), "models": {}}
    for model, files in raw_files_by_model().items():
        rows = load_rows(files)
        for r in rows:
            r["score"] = score_one(r["answer"], r["keywords"])
        per_pair = Counter(f"{r['question']} | {r['keywords'][0]}" for r in rows)
        res["models"][model] = {"files": [os.path.basename(f) for f in files],
                                "rows_per_pair": dict(per_pair), **aggregate(rows)}
        a = res["models"][model]["all"]
        print(f"  {model}: 답변 {a['answers']}, 스키마 실패 {a['schema_fail']['k']}, "
              f"후보 밖(lenient) {a['out_lenient_items']['k']}/{a['out_lenient_items']['n']}")
    p = os.path.join(OUT_DIR, "model_compare_baseline.json")
    json.dump(res, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"  고유 (질문, 후보) 쌍: {res['unique_pairs']}\n  저장: {os.path.relpath(p, BASE_DIR)}")


# ---------------------------------------------------------------- 로컬 모델 실행

def ollama_info(model: str) -> Dict[str, Any]:
    """`ollama list` 와 같은 정보를 HTTP API 로 읽는다 (태그, digest, 서버 버전)."""
    import urllib.request
    url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    get = lambda p: json.load(urllib.request.urlopen(url + p, timeout=10))   # noqa: E731
    tag = next((m for m in get("/api/tags")["models"] if m["name"] == model), {})
    return {"url": url, "server_version": get("/api/version").get("version"),
            "tag": model, "digest": tag.get("digest"), "details": tag.get("details")}


def run(model: str, mode: str, repeat: int, runtime: str) -> None:
    from generate import make_llm, generate
    structured = mode == "structured"
    llm = make_llm("ollama", model, TEMPERATURE)
    pairs = unique_pairs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = model.replace(":", "-").replace("/", "-")
    cond = {"model": ollama_info(model), "mode": mode, "temperature": TEMPERATURE,
            "repeat": repeat, "unique_pairs": len(pairs), "calls": repeat * len(pairs),
            "hardware": {**HARDWARE, "ollama_runtime": runtime}, "measured_at": stamp}

    # 워밍업: 모델 적재 시간을 지연 집계에서 뺀다. 여기서 실패하면 전 호출이 같은 이유로 실패한다.
    try:
        t = time.perf_counter()
        generate(llm, pairs[0][0], pairs[0][1], structured=structured)
        cond["warmup_s"] = round(time.perf_counter() - t, 2)
    except Exception as e:                                           # noqa: BLE001
        cond["aborted"] = f"warmup {type(e).__name__}: {e}"
        p = os.path.join(OUT_DIR, f"model_compare_{safe}_{mode}_{stamp}.json")
        json.dump(cond, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        raise SystemExit(f"  중단: {cond['aborted']}\n  저장: {os.path.relpath(p, BASE_DIR)}")

    rows: List[dict] = []
    raw = os.path.join(OUT_DIR, f"model_compare_raw_{safe}_{mode}_{stamp}.jsonl")
    with open(raw, "w", encoding="utf-8") as f:
        for rep in range(repeat):
            for q, kws in pairs:
                t = time.perf_counter()
                try:
                    ans, err = generate(llm, q, kws, structured=structured), None
                except Exception as e:                               # noqa: BLE001
                    ans, err = None, f"{type(e).__name__}: {e}"
                lat = round(time.perf_counter() - t, 3)
                sc = score_one(ans, kws) if err is None else \
                    {"ok": False, "reason": "call_error", "detail": err, "names": [],
                     "out_lenient": [], "out_strict": []}
                r = {"model": model, "mode": mode, "rep": rep, "question": q, "keywords": kws,
                     "answer": ans, "error": err, "latency_s": lat, "score": sc}
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                rows.append(r)
                print(f"  rep{rep + 1} {q:<16} {lat:6.2f}s {sc['reason']}", flush=True)

    res = {**cond, "raw_file": os.path.basename(raw), **aggregate(rows)}
    p = os.path.join(OUT_DIR, f"model_compare_{safe}_{mode}_{stamp}.json")
    json.dump(res, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"  저장: {os.path.relpath(p, BASE_DIR)}\n  원문: {os.path.relpath(raw, BASE_DIR)}")


# ---------------------------------------------------------------- 표

def _fmt(m: Dict[str, Any]) -> str:
    if not m["n"]:
        return "n/a"
    lo, hi = m["ci95"]
    return f"{m['k']}/{m['n']} {m['rate']:.1%} [{lo:.1%}, {hi:.1%}]"


def table() -> None:
    entries: List[Tuple[str, str, Dict[str, Any]]] = []
    b = json.load(open(os.path.join(OUT_DIR, "model_compare_baseline.json"), encoding="utf-8"))
    for model, st in b["models"].items():
        entries.append((f"{model} (저장 원문)", "free", st))
    for p in sorted(glob.glob(os.path.join(OUT_DIR, "model_compare_*_*.json"))):
        if "baseline" in p:
            continue
        st = json.load(open(p, encoding="utf-8"))
        if "aborted" in st:
            print(f"<!-- {st['model']['tag']} {st['mode']} 중단: {st['aborted'][:120]} -->")
            continue
        entries.append((f"{st['model']['tag']} x{st['repeat']}", st["mode"], st))
    print("| 모델 | 모드 | 스키마 실패 k/n [95% CI] | 사유 | 거부(답변) k/n | 후보 밖 lenient 항목 전체 [95% CI] "
          "| 재료 경로 | 메뉴 경로 | 후보 밖 strict 항목 | 항목/답변 | 지연 p50/p95 (s) |")
    print("|" + "---|" * 11)
    for name, mode, st in entries:
        a = st["all"]
        rs = ", ".join(f"{k} {v}" for k, v in a["reasons"].items() if k != "ok") or "-"
        lat = st.get("latency_s")
        lat_s = f"{lat['p50']} / {lat['p95']}" if lat else "-"
        print(f"| {name} | {mode} | {_fmt(a['schema_fail'])} | {rs} | {a['rejected']['k']}/{a['rejected']['n']} "
              f"| {_fmt(a['out_lenient_items'])} | {_fmt(st['ingredient']['out_lenient_items'])} "
              f"| {_fmt(st['menu']['out_lenient_items'])} | {_fmt(a['out_strict_items'])} "
              f"| {a['items_per_answer']['mean']} | {lat_s} |")


# ---------------------------------------------------------------- 자체 검증

def selftest() -> None:
    """모델 호출 없이 채점 경로를 확인한다."""
    k = ["딸기", "크림", "케이크"]
    s = score_one('[{"name":"딸기 크림 케이크","description":"x"}]', k)
    assert s["ok"] and not s["out_lenient"] and s["out_strict"] == ["딸기 크림 케이크"], s
    s = score_one([{"name": "딸기", "description": "x"}, {"name": "망고 빙수", "description": "y"}], k)
    assert not s["ok"] and s["reason"] == "ungrounded" and s["out_lenient"] == ["망고 빙수"], s
    assert score_one("추천드립니다: 딸기", k)["reason"] == "json_parse_error"
    assert score_one('{"items": []}', k)["reason"] == "not_a_list"
    assert score_one('[{"description":"x"}]', k)["reason"] == "missing_name_field"
    assert score_one('```json\n[{"name":"크림"}]\n```', k)["ok"]     # 펜스는 런타임과 같이 벗긴다

    rows = [{"question": "최근 유행하는 재료", "keywords": k, "latency_s": 1.0,
             "score": score_one([{"name": "딸기"}, {"name": "망고"}], k)},
            {"question": "카페 신메뉴 추천", "keywords": k, "latency_s": 9.0,
             "score": score_one("not json", k)}]
    a = aggregate(rows)
    assert a["all"]["schema_fail"]["k"] == 1 and a["all"]["rejected"]["k"] == 2, a
    assert a["ingredient"]["out_lenient_items"] == {"k": 1, "n": 2, "rate": 0.5, "ci95": wilson(1, 2)}
    assert a["menu"]["out_lenient_items"]["n"] == 0
    assert a["latency_s"]["p50"] == 1.0 and a["latency_s"]["over_3x_p50"] == [9.0], a["latency_s"]

    lo, hi = wilson(0, 10)                       # 0/10 이어도 상한은 0 이 아니다
    assert lo == 0.0 and 0.27 < hi < 0.28, (lo, hi)
    lo, hi = wilson(5, 10)
    assert abs((lo + hi) / 2 - 0.5) < 1e-3
    assert path_of("최근 유행하는 재료") == "ingredient" and path_of("SNS에서 핫한 메뉴") == "menu"
    print("eval_models 자체 검증 통과")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="로컬 LLM 대 gpt-4, 구조화 출력 대 자유 텍스트")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("baseline", help="저장된 원문 재채점 (API 호출 없음)")
    r = sub.add_parser("run", help="로컬 모델 실행")
    r.add_argument("--model", required=True)
    r.add_argument("--mode", choices=["structured", "free"], required=True)
    r.add_argument("--repeat", type=int, default=3)
    r.add_argument("--runtime", default="unspecified", help="Ollama 실행 형태 (기록용)")
    sub.add_parser("table", help="저장된 요약으로 마크다운 표")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.cmd == "baseline":
        baseline()
    elif a.cmd == "run":
        run(a.model, a.mode, a.repeat, a.runtime)
    elif a.cmd == "table":
        table()
    else:
        ap.print_help()
