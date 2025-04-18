import json
from langchain_openai import ChatOpenAI
from langchain.schema import SystemMessage, HumanMessage
from dotenv import load_dotenv
import ast

# ✅ 환경변수 로드
load_dotenv(dotenv_path="configs/.env")

# ✅ LLM 초기화
llm = ChatOpenAI(temperature=0.2, model="gpt-4")

# ✅ 키워드 로드
with open("data/generated_keywords_with_new_count.jsonl", "r", encoding="utf-8") as f:
    keyword_items = [json.loads(line) for line in f if line.strip() != ""]

# ✅ 결과 저장 리스트
results = []

# ✅ 하나씩 질문
for item in keyword_items:
    kw = item["keyword"]
    count = item["new_count"]
    components = item.get("matched_parts", [])  # 사용된 키워드들

    system_msg = SystemMessage(
        content=(
            "당신은 마케팅 데이터 아키텍처 전문가입니다. GraghRAG 사용자 키워드에 대해 "
            "Neo4j 노드로 저장할 정보를 구성하려고 합니다. 아래 형식의 JSON으로 응답하세요:\n\n"
            "{\n"
            "  'keyword': 키워드,\n"
            "  'category': 키워드 분류 (예: 디저트, 간식, 음료, 베이커리 등),\n"
            "  'description': 한 줄 설명,\n"
            "  'example': 대표적인 예시나 서브메뉴 (예: 다양한 맛, 인기 조합 등)\n"
            "  'season': 해당메뉴가 어울리는 계절(예: 딸기관련-겨울, 수박관련-여름 등) 단, 봄,여름,가을,겨울,사계절 중 하나로 정해줘\n"
            "}\n"
        )
    )

    user_msg = HumanMessage(content=f"'{kw}'에 대해 위 포맷에 맞춰 정보를 반환해줘.")

    try:
        response = llm.invoke([system_msg, user_msg])
        parsed = ast.literal_eval(response.content)

        parsed["count"] = count
        parsed["keyword"] = kw
        parsed["components"] = components  # 원래 키워드들도 함께 저장
        results.append(parsed)

        print(f"✅ '{kw}' 처리 완료")
    except Exception as e:
        print(f"❌ '{kw}' 처리 실패:", e)
        print("응답 내용:", response.content)

# ✅ 저장
with open("data/neo4j_ready_keywords.jsonl", "w", encoding="utf-8") as f:
    for item in results:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

print("✅ 저장 완료: data/neo4j_ready_keywords.jsonl")
