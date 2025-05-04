from collections import Counter
from konlpy.tag import Okt
import json
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.schema import SystemMessage, HumanMessage
from langsmith import traceable

# ✅ 환경 변수 로드
load_dotenv(dotenv_path="configs/.env")

# ✅ 제외 키워드
BAN_KEYWORDS = {"먹방", "만들기", "레시피", "리뷰", "정보", "도전", "쇼츠", "타이틀", "브이로그"}
COMMON_WORDS = {"초코", "케이크", "우유", "빵", "쿠키", "디저트", "치즈", "도넛", "음료"}

def is_banned_phrase(phrase: str) -> bool:
    if phrase in COMMON_WORDS:
        return True
    if any(ban in phrase for ban in BAN_KEYWORDS):
        return True
    if phrase.endswith(" 디저트"):
        return True
    if len(phrase.strip()) <= 1:
        return True
    return False

# ✅ 키워드 추출 (nouns + 2~3gram 조합)
def extract_noun_phrases_with_counts(filepath: str) -> Counter:
    okt = Okt()
    phrase_counter = Counter()
    unique_keywords = set()

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            for keyword in obj["keywords"]:
                if not is_banned_phrase(keyword):
                    unique_keywords.add(keyword)

    for keyword in unique_keywords:
        tokens = okt.pos(keyword, norm=True, stem=True)
        nouns = [w for w, t in tokens if t == "Noun"]
        if len(nouns) < 2:
            continue

        for noun in nouns:
            if not is_banned_phrase(noun):
                phrase_counter[noun] += 1

        for n in range(2, 4):  # 2~3gram만
            for i in range(len(nouns) - n + 1):
                ngram = nouns[i:i+n]
                joined = " ".join(ngram)
                if not is_banned_phrase(joined):
                    phrase_counter[joined] += 2  # 조합 강조

    return phrase_counter

# ✅ LangChain LLM 필터링
@traceable(run_type="chain", name="FilterFoodKeywords")
def filter_food_keywords(all_keywords: list[str]) -> list[str]:
    llm = ChatOpenAI(temperature=0, model="gpt-4")
    system_msg = SystemMessage(
        content=(
            "당신은 디저트 메뉴 기획 전문가입니다.\n"
            "아래 키워드 중 실제 카페에서 판매할 수 있는 디저트 이름 혹은 조합만 골라주세요.\n"
            "- 단일 명사(예: '초코', '케이크', '우유')는 제외\n"
            "- 메뉴명, 조합명, 디저트 구성에 어울리는 키워드만 추려주세요.\n"
            "- 결과는 JSON 배열 형식으로 50개 이내로 반환하세요.\n"
            "예: [\"초코 크레페\", \"딸기 티라미수\", \"피스타치오 크림\"]"
        )
    )
    user_msg = HumanMessage(content=f"키워드 목록: {all_keywords}")
    response = llm.invoke([system_msg, user_msg])

    try:
        return json.loads(response.content)
    except json.JSONDecodeError:
        print("❌ JSON 파싱 실패:", response.content)
        return []

# ✅ 실행
if __name__ == "__main__":
    input_path = "data/keywords.jsonl"
    output_path = "data/filtered_keywords_with_count.jsonl"

    phrase_counter = extract_noun_phrases_with_counts(input_path)

    # ✅ 조합 키워드 우선 정렬
    def score_priority(k): return phrase_counter[k] * (1.5 if " " in k else 1)
    sorted_phrases = sorted(phrase_counter.keys(), key=score_priority, reverse=True)
    top_phrases = sorted_phrases[:500]

    print("\n🔍 추출된 후보 키워드:")
    print(top_phrases)

    print("\n🍽️ LLM 필터링 중...")
    food_keywords = filter_food_keywords(top_phrases)

    food_keyword_counts = [
        {"keyword": kw, "count": phrase_counter[kw]}
        for kw in food_keywords if kw in phrase_counter
    ]

    print("\n📦 최종 디저트 키워드 + 등장 횟수:")
    print(json.dumps(food_keyword_counts, ensure_ascii=False, indent=2))

    os.makedirs("data", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for item in food_keyword_counts:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"\n✅ 필터링 완료 → {output_path}")
