# ✅ keyword_generator_agent.py

import json
import re
import os
from langchain.chat_models import ChatOpenAI
from langchain.schema import SystemMessage, HumanMessage
from dotenv import load_dotenv
from langsmith import traceable  # ✅ LangSmith 트레이스 기능 추가

# ✅ 환경 변수 로드
load_dotenv(dotenv_path="configs/.env")

# ✅ LLM 초기화
llm = ChatOpenAI(temperature=0.3, model="gpt-4")

# ✅ 키워드 로딩 (키워드 + count)
input_path = "data/filtered_keywords_with_count.jsonl"
with open(input_path, "r", encoding="utf-8") as f:
    keyword_items = [json.loads(line) for line in f if line.strip()]

keywords = [item["keyword"] for item in keyword_items]
count_map = {item["keyword"]: item["count"] for item in keyword_items}

# ✅ 프롬프트 구성
system_msg = SystemMessage(
    content=(
        "당신은 푸드 마케팅 전문가입니다. 아래 디저트/카페 메뉴 키워드들을 참고하여 트렌디한 카페 메뉴 키워드를 20개 이상 생성하세요.\n"
        "각 키워드는 실제 검색에 사용될 수 있는 자연스러운 메뉴 조합이어야 하며, 2025년 유행할 법한 신메뉴/핫메뉴를 중심으로 구성해주세요.\n"
        "예: ['피넛 버터 로투스 쿠키', '딸기 크림 디저트', '솔티 카라멜 케이크', '누텔라 크레페', '초코칩 머핀']\n"
        "🍽️ 주의: 새우 튀김과 솔티 카라멜, 딸기 케이크와 핫 치즈처럼 맛이 어울리지 않는 조합은 절대 생성하지 마세요.\n"
        "❗ 결과는 반드시 JSON 배열 형식으로만 반환하세요. 다른 설명은 하지 마세요."
    )
)

user_msg = HumanMessage(
    content=f"다음 키워드들을 조합해서 트렌디한 카페 메뉴 키워드를 20개 생성해줘: {keywords}"
)

# ✅ LangSmith 트레이스용 함수 정의
@traceable(run_type="chain", name="GenerateTrendyCafeKeywords")  # ✅ 트레이스 추가
def generate_keywords(system_msg, user_msg) -> str:
    response = llm.invoke([system_msg, user_msg])
    return response.content

# ✅ LLM 호출
response_content = generate_keywords(system_msg, user_msg)

# ✅ 응답 파싱 및 저장
try:
    try:
        # 먼저 JSON 배열 형식으로 파싱 시도
        new_keywords = json.loads(response_content)
    except json.JSONDecodeError:
        # JSON 형식이 아닐 경우, 숫자 목록으로 파싱
        new_keywords = re.findall(r"\d+\.\s*['\"]?(.+?)['\"]?(?:\n|$)", response_content)

    print("\n📌 생성된 키워드 목록:")
    for kw in new_keywords:
        print("-", kw)

    enriched = []
    for new_kw in new_keywords:
        matched = [k for k in count_map if k in new_kw]
        matched_counts = [count_map[k] for k in matched]
        total_count = sum(matched_counts) if matched_counts else 0

        enriched.append({
            "keyword": new_kw,
            "new_count": total_count,
            "matched_parts": matched
        })

    output_path = "data/generated_keywords_with_new_count.jsonl"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f_out:
        for kw in enriched:
            f_out.write(json.dumps(kw, ensure_ascii=False) + "\n")

    print("\n✅ 저장 완료:", output_path)

except Exception as e:
    print("❌ 응답 파싱 실패:", response_content)
    print(e)
