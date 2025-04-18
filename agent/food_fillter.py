from collections import Counter
from konlpy.tag import Okt
import json
import os

from dotenv import load_dotenv
load_dotenv(dotenv_path="configs/.env")

from langchain_openai import ChatOpenAI
from langchain.schema import SystemMessage, HumanMessage


# ✅ 1. 명사 기반 키워드 추출 (연속된 명사)
def extract_noun_phrases_with_counts(filepath: str) -> Counter:
    okt = Okt()
    phrase_counter = Counter()

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            for keyword in obj["keywords"]:
                tokens = okt.pos(keyword, norm=True, stem=True)
                phrase = []
                for word, tag in tokens:
                    if tag == "Noun":
                        phrase.append(word)
                    else:
                        if phrase:
                            joined = " ".join(phrase)
                            phrase_counter[joined] += 1
                            phrase = []
                if phrase:
                    joined = " ".join(phrase)
                    phrase_counter[joined] += 1

    return phrase_counter


# ✅ 2. LLM에게 음식 키워드만 필터링 요청
def filter_food_keywords(all_keywords: list[str]) -> list[str]:
    llm = ChatOpenAI(temperature=0, model="gpt-4")

    system_msg = SystemMessage(
        content="당신은 음식 전문가입니다. 주어진 키워드 중 음식과 관련된 키워드만 골라 JSON 배열로 반환하세요. 예: [\"초콜릿\", \"딸기\"]"
    )

    user_msg = HumanMessage(
        content=f"키워드 목록: {all_keywords}"
    )

    response = llm.invoke([system_msg, user_msg])

    try:
        return json.loads(response.content)
    except json.JSONDecodeError:
        print("❌ JSON 파싱 실패:", response.content)
        return []


# ✅ 실행
if __name__ == "__main__":
    phrase_counter = extract_noun_phrases_with_counts("data/keywords.jsonl")
    top_phrases = [kw for kw, _ in phrase_counter.most_common(100)]

    print("\n🔍 추출된 연속 명사 키워드:")
    print(top_phrases)

    print("\n🍽️ LLM을 통한 음식 키워드 필터링:")
    food_keywords = filter_food_keywords(top_phrases)

    # ✅ 결과 병합 및 출력
    food_keyword_counts = [
        {"keyword": kw, "count": phrase_counter[kw]}
        for kw in food_keywords if kw in phrase_counter
    ]

    print(json.dumps(food_keyword_counts, ensure_ascii=False, indent=2))

    # ✅ JSONL 저장
    os.makedirs("data", exist_ok=True)
    with open("data/filtered_keywords_with_count.jsonl", "w", encoding="utf-8") as f:
        for item in food_keyword_counts:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print("\n✅ 필터링된 음식 키워드 저장 완료 → data/filtered_keywords_with_count.jsonl")
