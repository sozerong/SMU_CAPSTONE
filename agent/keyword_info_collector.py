import json
import os
import re
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.schema import SystemMessage, HumanMessage
from langsmith import traceable

# ✅ 환경 변수 로드
load_dotenv("configs/.env")

llm = ChatOpenAI(temperature=0.2, model="gpt-3.5-turbo")

# ✅ 입력 파일 (위 키워드 목록 기반)
input_path = "data/filtered_keywords_with_count.jsonl"
with open(input_path, "r", encoding="utf-8") as f:
    keyword_items = [json.loads(line) for line in f if line.strip()]

max_count = max(item["count"] for item in keyword_items)
results = []

# ✅ LLM 호출 함수
@traceable(run_type="chain", name="FormatKeywordMetadata")
def format_keyword_with_llm(keyword: str) -> str:
    system_msg = SystemMessage(
        content=(
            "당신은 디저트/카페 메뉴 기획 전문가입니다.\n"
            f"키워드 '{keyword}' 에 대한 정보를 아래 JSON 형식으로 응답해주세요.\n"
            "- 반드시 파싱 가능한 JSON 문자열이어야 합니다 (큰따옴표 사용).\n"
            "- 필드:\n"
            "  keyword (str): 키워드 자체\n"
            "  category (str): '디저트', '음료', '베이커리' 중 하나\n"
            "  example (list): 해당 키워드를 실제 사용하는 예시 문장 최대 2개\n"
            "  season (str): 봄/여름/가을/겨울 중 해당하는 계절, 없으면 null\n"
            "  tag (list): 맛, 분위기 등을 설명하는 형용사형 키워드 예: ['달콤함', '부드러움']\n"
            "  combo (list): 해당 키워드를 구성하는 조합 예: ['초코+크림']\n"
            "  related (list): 연관된 다른 디저트 키워드 최대 5개\n"
            "- combo는 반드시 '+'로 재료를 연결하세요.\n"
            "- example은 문장 형식으로 주세요.\n"
            "- 예시:\n"
            "{\n"
            "  \"keyword\": \"딸기 티라미수\",\n"
            "  \"category\": \"디저트\",\n"
            "  \"example\": [\"상큼한 딸기와 부드러운 크림이 어우러진 티라미수입니다.\"],\n"
            "  \"season\": \"봄\",\n"
            "  \"tag\": [\"상큼함\", \"비주얼\", \"달콤함\"],\n"
            "  \"combo\": [\"딸기+크림\", \"티라미수+딸기\"],\n"
            "  \"related\": [\"딸기 케이크\", \"딸기라떼\"]\n"
            "}"
        )
    )
    user_msg = HumanMessage(content=f"키워드: {keyword}")
    response = llm.invoke([system_msg, user_msg])
    return response.content

# ✅ 키워드별 LLM 호출 및 정제
for item in keyword_items:
    kw = item["keyword"]
    count = item["count"]
    response_content = ""

    try:
        response_content = format_keyword_with_llm(kw)
        try:
            parsed = json.loads(response_content)
        except json.JSONDecodeError:
            print(f"⚠️ JSON 파싱 실패, 복구 시도: {kw}")
            match = re.search(r'{.*}', response_content, re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
            else:
                raise ValueError("복구 실패")

        # ✅ 필수 필드 추가
        parsed["keyword"] = kw
        parsed["count"] = count
        parsed["importance"] = round(count / max_count, 4)

        # ✅ combo 분해
        if "combo" in parsed:
            parsed["combo"] = [
                {"items": [part.strip() for part in combo.split("+")]}
                for combo in parsed["combo"]
                if "+" in combo
            ]

        # ✅ example 정제 (문장 분리 최대 2개)
        if "example" in parsed:
            ex = parsed["example"]
            if isinstance(ex, str):
                ex = re.split(r"[.!?]", ex)
            parsed["example"] = [e.strip() for e in ex if e.strip()][:2]

        # ✅ tag 정제
        if "tag" in parsed:
            parsed["tag"] = list(set([
                t.strip() for t in parsed["tag"]
                if isinstance(t, str) and len(t.strip()) > 1
            ]))

        # ✅ season 정제
        if parsed.get("season") not in {"봄", "여름", "가을", "겨울"}:
            parsed["season"] = None

        # ✅ related 정제
        if "related" in parsed:
            parsed["related"] = list(set([
                r.strip() for r in parsed["related"]
                if r.strip() and r != kw
            ]))[:5]

        results.append(parsed)
        print(f"✅ '{kw}' 처리 완료")

    except Exception as e:
        print(f"❌ '{kw}' 실패:", e)
        print("⛔ 응답 내용:", response_content)

# ✅ 저장
output_path = "data/neo4j_ready_keywords.jsonl"
os.makedirs(os.path.dirname(output_path), exist_ok=True)
with open(output_path, "w", encoding="utf-8") as f_out:
    for item in results:
        f_out.write(json.dumps(item, ensure_ascii=False) + "\n")

print(f"\n✅ 저장 완료: {output_path}")
