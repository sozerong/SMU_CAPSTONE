import json
import os
import sys
from collections import Counter
from datetime import datetime

from neo4j import GraphDatabase
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 후보 안/밖 판정의 단일 출처. eval_grounding.py 도 같은 모듈을 쓴다 —
# 생성 시점과 측정 시점의 기준이 갈라지면 둘 다 못 믿게 된다.
from grounding import validate_answer          # noqa: E402
# 후보 탐색 Cypher. graphrag_aq 는 import 만 해도 Neo4j·OpenAI 가 붙어서 테스트가 안 된다.
# 후보 선정이 이 파이프라인에서 가장 틀리기 쉬운 자리라 검증 가능한 모듈로 뺐다.
from candidates import get_keywords_by_question   # noqa: E402
# 프롬프트·모델·구조화 출력은 질의 서비스(ask_graph)와 같은 함수를 쓴다.
# 모델은 LLM_PROVIDER / LLM_MODEL 로 고른다 (기본 로컬 qwen2.5:7b, ADR-0012).
from generate import generate, make_llm            # noqa: E402

DEAD_LETTER_PATH = "data/graphrag_dead_letter.jsonl"

# ✅ 환경 변수 로드
load_dotenv("configs/.env")
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# ✅ LLM 초기화
llm = make_llm()

# ✅ 질문 목록
questions = [
    "최근 유행하는 재료",
    "SNS에서 핫한 메뉴",
    "카페 신메뉴 추천",
    "달콤한 메뉴가 필요할 때",
    "비주얼이 예쁜 메뉴",
    "계절 한정 디저트"
]

# ✅ 질문별 LLM 응답 생성
results = []
dead_letters = []
with driver.session() as session:
    for question in questions:
        keywords = session.execute_read(get_keywords_by_question, question)

        # ✅ LLM 호출 (프롬프트는 generate.build_system_prompt)
        answer = generate(llm, question, keywords)

        # ✅ 파싱 검증 — 실패는 버리지 않고 격리한다
        ok, parsed_or_reason = validate_answer(answer, keywords)
        if ok:
            results.append({
                "question": question,
                "answer": parsed_or_reason,
                "keywords": keywords,
            })
            print(f"✅ 생성 완료: {question}")
        else:
            dead_letters.append({
                "question":    question,
                "keywords":    keywords,
                "reason":      parsed_or_reason,
                "raw_response": answer,
                "failed_at":   datetime.now().isoformat(timespec="seconds"),
            })
            print(f"❌ 격리: {question} — {parsed_or_reason}")

# ✅ 저장
os.makedirs("data", exist_ok=True)
output_path = "data/newnew_graphrag_answers.jsonl"
with open(output_path, "w", encoding="utf-8") as f:
    for item in results:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

# ✅ 격리된 실패 저장 — 건수를 세야 실패율을 말할 수 있다
total = len(results) + len(dead_letters)
if dead_letters:
    with open(DEAD_LETTER_PATH, "w", encoding="utf-8") as f:
        for item in dead_letters:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

print(f"\n✅ GraphRAG 응답 생성 및 저장 완료 → {output_path}")
print(f"   성공 {len(results)} / 전체 {total}"
      + (f"  |  격리 {len(dead_letters)}건 ({len(dead_letters)/total:.1%}) → {DEAD_LETTER_PATH}"
         if dead_letters else "  |  격리 0건"))
if dead_letters:
    reasons = Counter(d["reason"] for d in dead_letters)
    for reason, n in reasons.most_common():
        print(f"     {reason}: {n}건")

