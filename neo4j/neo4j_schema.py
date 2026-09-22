import json
import sys
from neo4j import GraphDatabase
from dotenv import load_dotenv
import os
from datetime import datetime

# Windows 기본 콘솔은 cp949 라 진행 로그의 이모지에서 UnicodeEncodeError 로 죽는다.
# 저장소의 다른 스크립트와 같은 처리를 둔다.
sys.stdout.reconfigure(encoding="utf-8")

# ✅ 환경 변수 로드
load_dotenv(dotenv_path="configs/.env")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password123")
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# ✅ JSONL 로딩
with open("data/neo4j_ready_keywords.jsonl", "r", encoding="utf-8") as f:
    data = [json.loads(line) for line in f if line.strip()]

# ✅ 날짜 노드용
today_str = datetime.now().strftime("%Y-%m-%d")

# ✅ 노드 생성 함수
def create_keyword_graph(tx, item):
    name = item["keyword"]
    count = item.get("count", 1)
    importance = item.get("importance", 0.0)

    print(f"⏳ 처리 중: {name}")

    # ✅ Keyword 노드
    # coalesce 를 쓰는 이유: 이 키워드가 이전 회차에 RELATED 대상으로만 먼저 생겼다면
    # count 속성이 없다(NULL). NULL + 숫자 = NULL 이라 한 번 그렇게 되면 영구히 NULL 이고,
    # count 로 정렬하는 후보 쿼리에서 이 키워드가 계속 뒤로 밀린다.
    tx.run("""
        MERGE (k:Keyword {name: $name})
        ON CREATE SET k.count = $count, k.importance = $importance
        ON MATCH SET k.count = coalesce(k.count, 0) + $count,
                     k.importance = coalesce($importance, k.importance)
    """, name=name, count=count, importance=importance)

    # ✅ 날짜 노드
    tx.run("""
        MERGE (d:Date {value: $today})
        WITH d
        MATCH (k:Keyword {name: $name})
        MERGE (k)-[:RECORDED_ON]->(d)
    """, name=name, today=today_str)

    # ✅ Category
    if cat := item.get("category"):
        tx.run("""
            MERGE (c:Category {value: $cat})
            WITH c
            MATCH (k:Keyword {name: $name})
            MERGE (k)-[:HAS_CATEGORY]->(c)
        """, name=name, cat=cat)

    # ✅ Season
    if season := item.get("season"):
        tx.run("""
            MERGE (s:Season {value: $season})
            WITH s
            MATCH (k:Keyword {name: $name})
            MERGE (k)-[:HAS_SEASON]->(s)
        """, name=name, season=season)

    # ✅ Tag
    for tag in item.get("tag", []):
        tx.run("""
            MERGE (t:Tag {name: $tag})
            WITH t
            MATCH (k:Keyword {name: $name})
            MERGE (k)-[:HAS_TAG]->(t)
        """, name=name, tag=tag)

    # ✅ Combo → 재료 노드 생성 및 연결
    for combo in item.get("combo", []):
        ingredients = combo.get("items", [])
        if not ingredients:
            continue
        combo_name = "+".join(ingredients)

        tx.run("""
            MERGE (c:Combo {name: $combo_name})
            WITH c
            MATCH (k:Keyword {name: $name})
            MERGE (k)-[:IS_COMBO_WITH]->(c)
        """, name=name, combo_name=combo_name)

        for ing in ingredients:
            tx.run("""
                MERGE (i:Ingredient {name: $ing})
                WITH i
                MATCH (c:Combo {name: $combo_name})
                MERGE (c)-[:INCLUDES]->(i)
            """, combo_name=combo_name, ing=ing)

    # ✅ Example 연결
    for ex in item.get("example", [])[:2]:
        if ex.strip():
            tx.run("""
                MERGE (e:Example {value: $example})
                WITH e
                MATCH (k:Keyword {name: $name})
                MERGE (k)-[:HAS_EXAMPLE]->(e)
            """, name=name, example=ex.strip())

    # ✅ Related 관계
    for rel_kw in item.get("related", []):
        if rel_kw.strip() and rel_kw != name:
            # RELATED 대상도 회차(Date)에 묶는다.
            # 안 묶으면 RECORDED_ON 이 없어 회차 단위 정리(neo4j/cleanup.py)로
            # 영원히 지울 수 없는 노드가 쌓인다 (실측: 337개 중 251개가 이 상태였다).
            # count=0 으로 만들어 두는 것도 같은 이유다 — 나중에 진짜 키워드로
            # 들어올 때 NULL 산술에 걸리지 않게.
            tx.run("""
                MERGE (b:Keyword {name: $b})
                ON CREATE SET b.count = 0
                WITH b
                MATCH (a:Keyword {name: $a})
                MERGE (a)-[:RELATED {score: 0.9}]->(b)
                WITH b
                MATCH (d:Date {value: $today})
                MERGE (b)-[:RECORDED_ON]->(d)
            """, a=name, b=rel_kw, today=today_str)

# ✅ 실행
with driver.session() as session:
    for item in data:
        session.execute_write(create_keyword_graph, item)

print("\n✅ 그래프 저장 완료")

# ✅ 인기 키워드 출력
with driver.session() as session:
    result = session.run("""
        MATCH (k:Keyword)
        RETURN k.name AS name, k.count AS count
        ORDER BY k.count DESC
        LIMIT 10
    """)
    print("\n📊 인기 키워드 Top 10:")
    for record in result:
        print(f"{record['name']} (언급 수: {record['count']})")
