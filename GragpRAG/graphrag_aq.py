import json
import os
from neo4j import GraphDatabase
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.schema import SystemMessage, HumanMessage

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

# ✅ Cypher 기반 키워드 추출
def get_keywords_by_question(tx, question):
    tags, season, category = [], None, None
    use_ingredient = "재료" in question

    if "달콤" in question:
        tags.append("단맛")
    if "비주얼" in question or "예쁜" in question:
        tags.append("비주얼")
    if "SNS" in question or "핫한" in question:
        tags.append("트렌디")
    if "계절" in question:
        season = "봄"
    if "신메뉴" in question or "카페" in question:
        category = "디저트"

    if use_ingredient:
        cypher = """
            MATCH (i:Ingredient)
            OPTIONAL MATCH (e:Example)-[:CONTAINS_INGREDIENT]->(i)
            WITH i, count(e) AS usage_count
            RETURN i.name AS keyword
            ORDER BY usage_count DESC
            LIMIT 10
        """
        return [record["keyword"] for record in tx.run(cypher)]

    cypher = "WITH 1 AS _\nMATCH (k:Keyword)\n"
    where_clauses = []
    params = {}

    if tags:
        cypher += "MATCH (k)-[:HAS_TAG]->(t:Tag)\n"
        where_clauses.append("t.name IN $tags")
        params["tags"] = tags

    if season:
        cypher += "MATCH (k)-[:HAS_SEASON]->(s:Season)\n"
        where_clauses.append("s.value = $season")
        params["season"] = season

    if category:
        cypher += "MATCH (k)-[:HAS_CATEGORY]->(c:Category)\n"
        where_clauses.append("c.value = $category")
        params["category"] = category

    if where_clauses:
        cypher += "WHERE " + " AND ".join(where_clauses) + "\n"

    cypher += "RETURN k.name AS keyword, k.count AS count ORDER BY count DESC LIMIT 10"

    keywords = [record["keyword"] for record in tx.run(cypher, **params)]

    if not keywords:
        fallback = tx.run("""
            MATCH (k:Keyword)
            RETURN k.name AS keyword
            ORDER BY k.count DESC
            LIMIT 10
        """)
        keywords = [record["keyword"] for record in fallback]

    return keywords

# ✅ 질문별 LLM 응답 생성
results = []
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

        # ✅ 파싱 검증
        try:
            parsed = json.loads(response.content)
            results.append({
                "question": question,
                "answer": parsed,
                "keywords": keywords
            })
            print(f"✅ 생성 완료: {question}")
        except Exception as e:
            print(f"❌ JSON 파싱 실패: {question}")
            print("응답 내용:", response.content)
            continue

# ✅ 저장
os.makedirs("data", exist_ok=True)
output_path = "data/newnew_graphrag_answers.jsonl"
with open(output_path, "w", encoding="utf-8") as f:
    for item in results:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

print(f"\n✅ GraphRAG 응답 생성 및 저장 완료 → {output_path}")
