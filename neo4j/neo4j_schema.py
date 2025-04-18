from neo4j import GraphDatabase
import json
import os
from dotenv import load_dotenv

# ✅ .env 로드
load_dotenv(dotenv_path="configs/.env")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password123")

# ✅ 드라이버 설정
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# ✅ JSONL 로딩
with open("data/neo4j_ready_keywords.jsonl", "r", encoding="utf-8") as f:
    data = [json.loads(line) for line in f if line.strip()]

# ✅ 트랜잭션 함수 정의
def create_keyword_graph(tx, item):
    name = item["keyword"]

    print(f"⏳ 키워드 저장 시도 중: {name}")

    # 🔹 키워드 노드 생성
    tx.run("MERGE (k:Keyword {name: $name})", name=name)

    # 🔹 카테고리
    if "category" in item:
        tx.run("""
            MERGE (c:Category {value: $value})
            WITH c
            MATCH (k:Keyword {name: $name})
            MERGE (k)-[:HAS_CATEGORY]->(c)
        """, name=name, value=item["category"])

    # 🔹 설명
    if "description" in item:
        tx.run("""
            MERGE (d:Description {value: $value})
            WITH d
            MATCH (k:Keyword {name: $name})
            MERGE (k)-[:HAS_DESCRIPTION]->(d)
        """, name=name, value=item["description"])


    if "season" in item and item["season"]:
        tx.run("""
            MERGE (s:Season {value: $season})
            MERGE (k:Keyword {name: $keyword})
            MERGE (k)-[:HAS_SEASON]->(s)
        """, keyword=name, season=item["season"])

    # 🔹 예시 (여러 개일 수 있음)
    if "example" in item:
        examples = [ex.strip() for ex in item["example"].split(",")]
        for example in examples:
            if example:
                tx.run("""
                    MERGE (e:Example {value: $example})
                    WITH e
                    MATCH (k:Keyword {name: $name})
                    MERGE (k)-[:HAS_EXAMPLE]->(e)
                """, name=name, example=example)

    # 🔹 조합에 사용된 기존 키워드 노드 및 연결
    if "components" in item:
        for comp in item["components"]:
            tx.run("""
                MERGE (c:Keyword {name: $component})
                WITH c
                MATCH (k:Keyword {name: $keyword})
                MERGE (k)-[:COMPOSED_FROM]->(c)
            """, keyword=name, component=comp)

    print(f"✅ 키워드 저장 완료: {name}")

# ✅ 실행
with driver.session() as session:
    for item in data:
        session.execute_write(create_keyword_graph, item)

print("✅ 키워드 및 속성/예시 노드 분리 생성 완료")
