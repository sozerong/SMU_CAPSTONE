"""
질의 서비스 평가 — 규칙 파서 + 재시도 없음(배치와 같은 동작) vs LLM 조건 추출 + 재생성 루프

    python GragpRAG/eval_ask.py conditions [--repeat 3]   # 조건 추출 정확도만 (생성 없음)
    python GragpRAG/eval_ask.py e2e [--repeat 3]          # 질문 → 답변 전체
    python GragpRAG/eval_ask.py resummarize eval/ask_e2e_<ts>.json   # 저장된 결과로 지표 재계산
    python GragpRAG/eval_ask.py --selftest

## 질문셋

`eval/ask_questions.jsonl` 20개. 배치의 고정 질문 6개 + 규칙에 없는 표현을 쓴 14개.
각 질문에 **허용 가능한 조건 값 목록**을 달았다 (예: "쫄깃한" → 쫄깃함 또는 쫀득함).
라벨 판정자는 Claude 다. 사람 교차검증이 없으므로 결과 JSON 의 `annotator` 에 남긴다.

`eval/ask_questions_holdout.jsonl` 15개는 조건 추출 프롬프트 v2 를 쓴 **뒤에** 새로 만든 질문이다.
v2 의 규칙은 20개 질문셋에서 v1 이 틀린 사례를 보고 썼으므로, 20개 셋 점수만으로는
과적합을 가릴 수 없다. 프롬프트 예시와 겹치는 표현은 홀드아웃에 넣지 않았다.

## 조건 정답 기준

필드별로 본다.
  use_ingredient : 일치
  tags           : 라벨이 비었으면 예측도 비어야 정답. 라벨이 있으면 예측과 교집합이 있어야 정답
  season/category: 예측값이 허용 목록 안 (None 포함)
네 필드가 모두 맞아야 질문 단위 정답이다.

## e2e 지표

  ok         : 검증 통과로 끝난 비율 (나머지는 dead letter)
  attempts   : 생성 호출 수
  unconditioned : 메뉴 경로에서 실제로 걸린 조건이 0개인 비율 — 후보가 질문과 무관한 빈도순 상위
  latency    : 질문 1건 전체 및 노드별 p50/p95
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _ROOT]
sys.path.insert(0, _HERE)

QUESTIONS = os.path.join(_ROOT, "eval", "ask_questions.jsonl")
OUT_DIR = os.path.join(_ROOT, "eval")


def load_questions(path: str = None) -> List[Dict[str, Any]]:
    with open(path or QUESTIONS, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def score_conditions(pred: Dict[str, Any], label: Dict[str, Any]) -> Dict[str, bool]:
    tags = set(pred.get("tags") or [])
    ok = set(label["tags_ok"])
    r = {
        "use_ingredient": bool(pred.get("use_ingredient")) == label["use_ingredient"],
        "tags": (bool(tags & ok) if ok else not tags),
        "season": pred.get("season") in label["season_ok"],
        "category": pred.get("category") in label["category_ok"],
    }
    r["all"] = all(r.values())
    return r


def pct(xs: List[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def conditions_cmd(repeat: int, questions: str = None) -> Dict[str, Any]:
    from neo4j import GraphDatabase
    from ask_graph import conditions_model, extract_conditions, graph_vocabulary
    from candidates import parse_question
    from generate import make_llm

    qs = load_questions(questions)
    llm = make_llm()
    drv = GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                               auth=(os.getenv("NEO4J_USER", "neo4j"),
                                     os.getenv("NEO4J_PASSWORD", "password123")))
    with drv.session() as s:
        vocab = graph_vocabulary(s)
    C = conditions_model(vocab)

    rows = []
    for q in qs:
        rule = parse_question(q["question"])
        rows.append({"question": q["question"], "src": "rule", "rep": 0, "pred": rule,
                     "score": score_conditions(rule, q),
                     "out_of_vocab_tags": [t for t in rule["tags"] if t not in vocab["tags"]]})
        for rep in range(repeat):
            t0 = time.perf_counter()
            pred = extract_conditions(llm, C, q["question"], vocab)
            dt = time.perf_counter() - t0
            rows.append({"question": q["question"], "src": "llm", "rep": rep, "pred": pred,
                         "latency_s": dt,
                         "score": score_conditions(pred or {}, q) if pred else None})

    def summary(src):
        rs = [r for r in rows if r["src"] == src and r["score"] is not None]
        n = len(rs)
        out = {"n": n, "failed_calls": sum(1 for r in rows if r["src"] == src and r["score"] is None)}
        for k in ("use_ingredient", "tags", "season", "category", "all"):
            k_ok = sum(r["score"][k] for r in rs)
            out[k] = {"k": k_ok, "rate": round(k_ok / n, 3) if n else None}
        return out

    res = {
        "measured_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "annotator": "claude", "questions": len(qs), "llm_repeat": repeat,
        "question_file": os.path.basename(questions or QUESTIONS),
        "model": getattr(llm, "model", None), "temperature": 0.3,
        "vocab_sizes": {k: len(v) for k, v in vocab.items()},
        "rule": summary("rule"), "llm": summary("llm"),
        "rule_out_of_vocab_tags": sorted({t for r in rows if r["src"] == "rule"
                                          for t in r.get("out_of_vocab_tags", [])}),
        "llm_latency_s": {"p50": pct([r["latency_s"] for r in rows if r["src"] == "llm"], .5),
                          "p95": pct([r["latency_s"] for r in rows if r["src"] == "llm"], .95)},
        "rows": rows,
    }
    path = os.path.join(OUT_DIR, f"ask_conditions_{res['measured_at']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, ensure_ascii=False, indent=2))
    print("→", path)
    return res


def unconditioned(row: Dict[str, Any]) -> bool:
    """
    메뉴 경로인데 실제로 걸린 조건이 하나도 없는가 — 후보가 질문과 무관한 빈도순 상위라는 뜻.
    조건을 처음부터 못 뽑은 경우와, 뽑았지만 후보를 찾느라 전부 푼 경우를 함께 센다.
    `relaxed` 의 "all" 만 세면 앞의 경우(규칙 파서가 아무 조건도 못 뽑음)가 빠진다.
    """
    c = row["conditions"]
    if c.get("use_ingredient"):
        return False
    given = {k for k in ("tags", "season", "category") if c.get(k)}
    return given <= set(row["relaxed"])


def summarize(rows: List[Dict[str, Any]], arm: str) -> Dict[str, Any]:
    rs = [r for r in rows if r["arm"] == arm]
    n = len(rs)
    tot = [r["total_s"] for r in rs]
    nodes = {k: [r["timings"].get(k, 0.0) for r in rs]
             for k in ("understand", "retrieve", "generate", "validate")}
    unc = [r for r in rs if unconditioned(r)]
    return {
        "n": n,
        "ok": {"k": sum(r["ok"] for r in rs), "rate": round(sum(r["ok"] for r in rs) / n, 3)},
        "first_try_ok": sum(r["ok"] and r["attempts"] == 1 for r in rs),
        "recovered_by_retry": sum(r["ok"] and r["attempts"] > 1 for r in rs),
        "attempts_mean": round(statistics.mean(r["attempts"] for r in rs), 3),
        "unconditioned": {"k": len(unc), "rate": round(len(unc) / n, 3),
                          "ok_within": sum(r["ok"] for r in unc)},
        "relaxed_any": sum(bool(r["relaxed"]) for r in rs),
        "fail_reasons": {k: sum(1 for r in rs if not r["ok"] and (r["reason"] or "").split(":")[0] == k)
                         for k in {(r["reason"] or "").split(":")[0] for r in rs if not r["ok"]}},
        "latency_s": {"p50": round(pct(tot, .5), 2), "p95": round(pct(tot, .95), 2),
                      "nodes_p50": {k: round(pct(v, .5), 3) for k, v in nodes.items()}},
    }


def resummarize_cmd(path: str) -> None:
    """저장된 rows 로 summary 를 다시 계산한다. 지표를 바꿔도 모델을 다시 부를 필요가 없다."""
    with open(path, encoding="utf-8") as f:
        res = json.load(f)
    res["summary"] = {a: summarize(res["rows"], a) for a in res["arms"]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(res["summary"], ensure_ascii=False, indent=2))


def e2e_cmd(repeat: int, questions: str = None) -> Dict[str, Any]:
    from neo4j import GraphDatabase
    import ask_graph
    from generate import make_llm

    ask_graph.DEAD_LETTER_PATH = os.path.join(_ROOT, "data", "ask_dead_letter_eval.jsonl")
    qs = load_questions(questions)
    llm = make_llm()
    drv = GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                               auth=(os.getenv("NEO4J_USER", "neo4j"),
                                     os.getenv("NEO4J_PASSWORD", "password123")))
    arms = {"baseline_rule_noretry": dict(llm_conditions=False, max_retry=0),
            "llm_cond_retry2": dict(llm_conditions=True, max_retry=2)}
    rows = []
    with drv.session() as s:
        llm.invoke("ok")  # 워밍업: 모델 로드 시간을 첫 질문에 싣지 않는다
        apps = {a: ask_graph.build_app(s, llm, **kw) for a, kw in arms.items()}
        for rep in range(repeat):
            for q in qs:
                for arm, app in apps.items():
                    t0 = time.perf_counter()
                    st = app.invoke({"question": q["question"]})
                    rows.append({"arm": arm, "rep": rep, "question": q["question"],
                                 "ok": st["ok"], "reason": st.get("reason"),
                                 "attempts": st["attempts"], "relaxed": st["relaxed"],
                                 "condition_source": st["condition_source"],
                                 "conditions": st["conditions"], "candidates": st["candidates"],
                                 "answer": st["answer"], "timings": st["timings"],
                                 "total_s": time.perf_counter() - t0})

    res = {"measured_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
           "model": getattr(llm, "model", None), "temperature": 0.3, "structured": True,
           "questions": len(qs), "repeat": repeat, "arms": arms,
           "question_file": os.path.basename(questions or QUESTIONS),
           "hardware": "AMD Ryzen 7 7800X3D, RTX 4070 SUPER 12GB, Ollama docker, Neo4j 5.15 docker",
           "summary": {a: summarize(rows, a) for a in arms}, "rows": rows}
    path = os.path.join(OUT_DIR, f"ask_e2e_{res['measured_at']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(res["summary"], ensure_ascii=False, indent=2))
    print("→", path)
    return res


def selftest() -> None:
    lab = {"use_ingredient": False, "tags_ok": ["쫄깃함", "쫀득함"], "season_ok": [None],
           "category_ok": ["디저트"]}
    assert score_conditions({"use_ingredient": False, "tags": ["쫀득함"], "season": None,
                             "category": "디저트"}, lab)["all"]
    s = score_conditions({"use_ingredient": False, "tags": ["달콤함"], "season": None,
                          "category": "디저트"}, lab)
    assert not s["tags"] and not s["all"] and s["category"], s
    # 라벨에 태그가 없으면 태그를 지어내도 오답
    lab2 = {**lab, "tags_ok": []}
    assert not score_conditions({"use_ingredient": False, "tags": ["달콤함"],
                                 "category": "디저트"}, lab2)["tags"]
    base = {"relaxed": [], "conditions": {"use_ingredient": False, "tags": [], "season": None, "category": None}}
    assert unconditioned(base)                                              # 조건 없음
    assert not unconditioned({**base, "conditions": {**base["conditions"], "tags": ["달콤함"]}})
    assert unconditioned({"relaxed": ["category", "tags"],                  # 걸었지만 다 풂
                          "conditions": {"use_ingredient": False, "tags": ["달콤함"], "category": "음료"}})
    assert not unconditioned({**base, "conditions": {**base["conditions"], "use_ingredient": True}})
    assert pct([1, 2, 3, 4, 100], .5) == 3 and pct([1, 2, 3, 4, 100], .95) == 100
    assert len(load_questions()) == 20
    print("eval_ask 자체 검증 통과")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["conditions", "e2e", "resummarize"])
    ap.add_argument("path", nargs="?", help="resummarize 대상 ask_e2e_*.json")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--questions", help="질문셋 경로 (기본 eval/ask_questions.jsonl)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest or not a.cmd:
        selftest()
    elif a.cmd == "conditions":
        conditions_cmd(a.repeat, a.questions)
    elif a.cmd == "resummarize":
        resummarize_cmd(a.path)
    else:
        e2e_cmd(a.repeat, a.questions)
