import json
import os
import sys
from collections import Counter
from datetime import datetime

from neo4j import GraphDatabase
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.schema import SystemMessage, HumanMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 후보 안/밖 판정의 단일 출처. eval_grounding.py 도 같은 모듈을 쓴다 —
# 생성 시점과 측정 시점의 기준이 갈라지면 둘 다 못 믿게 된다.
from grounding import validate_answer          # noqa: E402
# 후보 탐색 Cypher. graphrag_aq 는 import 만 해도 Neo4j·OpenAI 가 붙어서 테스트가 안 된다.
# 후보 선정이 이 파이프라인에서 가장 틀리기 쉬운 자리라 검증 가능한 모듈로 뺐다.
from candidates import get_keywords_by_question   # noqa: E402

DEAD_LETTER_PATH = "data/graphrag_dead_letter.jsonl"

# ✅ 환경 변수 로드
load_dotenv("configs/.env")
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# ✅ LLM 초기화
llm = ChatOpenAI(temperature=0.3, model="gpt-4")

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

        # 💬 키워드 context 목록 (LLM 프롬프트 무시 방지용)
        keyword_bullets = "\n".join(f"- {kw}" for kw in keywords)

        # 🎯 LLM 프롬프트 구분
        if "재료" in question:
            system_prompt = (
                f"당신은 카페 재료 기획 전문가입니다.\n"
                f"다음은 사용 가능한 재료 키워드입니다. 이 키워드만 참고해서 '{question}'에 어울리는 재료 3~5개를 추천하세요.\n"
                f"❗ 주어진 키워드 외의 재료를 생성하면 안 됩니다.\n\n"
                f"{keyword_bullets}\n\n"
                f"출력 형식: JSON 배열 [{{\"name\": \"이름\", \"description\": \"설명\"}} ...]"
            )
        else:
            system_prompt = (
                f"당신은 디저트 마케팅 전문가입니다.\n"
                f"다음은 사용 가능한 디저트 키워드입니다. 이 키워드만 참고해서 '{question}'에 어울리는 메뉴를 3~5개 추천하세요.\n"
                f"❗ 제공된 키워드 외 메뉴를 생성하지 마세요.\n\n"
                f"{keyword_bullets}\n\n"
                f"출력 형식: JSON 배열 [{{\"name\": \"이름\", \"description\": \"설명\"}} ...]"
            )

        # ✅ LLM 호출
        system_msg = SystemMessage(content=system_prompt)
        user_msg = HumanMessage(content=question)
        response = llm.invoke([system_msg, user_msg])

        # ✅ 파싱 검증 — 실패는 버리지 않고 격리한다
        ok, parsed_or_reason = validate_answer(response.content, keywords)
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
                "raw_response": response.content,
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

