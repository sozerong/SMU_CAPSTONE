# ✅ keyword_generator_agent.py

import json
import re
from langchain.chat_models import ChatOpenAI
from langchain.schema import SystemMessage, HumanMessage
from dotenv import load_dotenv

# ✅ 환경 변수 로드
load_dotenv(dotenv_path="configs/.env")

# ✅ LLM 초기화
llm = ChatOpenAI(temperature=0.3, model="gpt-4")

# ✅ 키워드 로딩 (키워드 + count)
input_path = "data/filtered_keywords_with_count.jsonl"
with open(input_path, "r", encoding="utf-8") as f:
    keyword_items = [json.loads(line) for line in f if line.strip() != ""]

keywords = [item["keyword"] for item in keyword_items]
count_map = {item["keyword"]: item["count"] for item in keyword_items}

# ✅ 프롬프트 구성
system_msg = SystemMessage(
    content="당신은 푸드 마케팅 전문가입니다. 아래 음식 관련 키워드들을 조합해 새로운 트렌디한 음식 키워드 또는 검색어를 생성하세요. 각 키워드는 실제 검색에 사용될 수 있는 자연스러운 조합이어야 합니다. 예: ['노오븐 초코쿠키', '에어프라이어 디저트', '딸기 크림 케이크']"
)

user_msg = HumanMessage(
    content=f"다음 키워드들을 조합해서 트렌디한 음식 키워드를 10개 생성해줘: {keywords}"
)

# ✅ LLM 호출
response = llm.invoke([system_msg, user_msg])

# ✅ 응답 파싱 및 저장
try:
    # 숫자 기반 목록 파싱
    new_keywords = re.findall(r"\d+\.\s*['\"]?(.+?)['\"]?(?:\n|$)", response.content)
    print("\n📌 생성된 키워드 목록:")
    for kw in new_keywords:
        print("-", kw)

    # 기존 키워드 count 기반으로 new_count 계산
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

    # ✅ 저장
    output_path = "data/generated_keywords_with_new_count.jsonl"
    with open(output_path, "w", encoding="utf-8") as f_out:
        for kw in enriched:
            f_out.write(json.dumps(kw, ensure_ascii=False) + "\n")

    print("\n✅ 저장 완료:", output_path)

except Exception as e:
    print("❌ 응답 파싱 실패:", response.content)
    print(e)
