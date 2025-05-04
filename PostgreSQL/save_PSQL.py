import os
import json
import psycopg2
import urllib.parse as urlparse
from dotenv import load_dotenv

# ✅ 환경 변수 로드
load_dotenv("configs/.env")
DATA_PATH = os.getenv("DATA_PATH", "data/newnew_graphrag_answers.jsonl")

# ✅ DATABASE_URL 파싱
db_url = os.getenv("DATABASE_URL")
parsed_url = urlparse.urlparse(db_url)

PG_DB = parsed_url.path[1:]
PG_USER = parsed_url.username
PG_PASSWORD = parsed_url.password
PG_HOST = parsed_url.hostname
PG_PORT = parsed_url.port

# ✅ PostgreSQL 연결
conn = psycopg2.connect(
    dbname=PG_DB,
    user=PG_USER,
    password=PG_PASSWORD,
    host=PG_HOST,
    port=PG_PORT
)
cursor = conn.cursor()
print("✅ PostgreSQL 연결 성공")

# ✅ 테이블 생성
cursor.execute("DROP TABLE IF EXISTS graphrag_answers;")
cursor.execute("""
    CREATE TABLE graphrag_answers (
        id SERIAL PRIMARY KEY,
        question TEXT,
        recommendations JSONB,
        keywords TEXT[]
    );
""")
conn.commit()
print("✅ 테이블 생성 완료")

# ✅ JSONL 파일 로드
if not os.path.exists(DATA_PATH):
    raise FileNotFoundError(f"{DATA_PATH} 경로에 파일이 없습니다.")

with open(DATA_PATH, "r", encoding="utf-8") as f:
    raw_documents = [json.loads(line) for line in f if line.strip()]

# ✅ 데이터 저장
for doc in raw_documents:
    question = doc.get("question", "")
    try:
        parsed_answer = doc["answer"] if isinstance(doc["answer"], list) else json.loads(doc["answer"])
    except Exception:
        print(f"⚠ JSON 파싱 실패: {question}")
        parsed_answer = [{"name": "[FORMAT ERROR]", "description": str(doc.get('answer'))}]

    keywords = doc.get("keywords", [])
    cursor.execute(
        "INSERT INTO graphrag_answers (question, recommendations, keywords) VALUES (%s, %s, %s);",
        (question, json.dumps(parsed_answer), keywords)
    )

conn.commit()
print("✅ 모든 데이터 PostgreSQL에 저장 완료")

# ✅ 연결 종료
cursor.close()
conn.close()
print("✅ 연결 종료")
