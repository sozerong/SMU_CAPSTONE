import os
import json
from dotenv import load_dotenv
from elasticsearch import Elasticsearch

# ✅ 환경 변수 로드
load_dotenv("configs/.env")

# ✅ 설정
ES_HOST = os.getenv("ES_HOST", "http://localhost:9200")
ES_USER = os.getenv("ES_USER", "elastic")
ES_PASSWORD = os.getenv("ES_PASSWORD", "changeme")
INDEX_NAME = os.getenv("INDEX_NAME", "graphrag_answers")
DATA_PATH = os.getenv("DATA_PATH", "data/newnew_graphrag_answers.jsonl")

# ✅ Elasticsearch 클라이언트 연결
es = Elasticsearch(ES_HOST, basic_auth=(ES_USER, ES_PASSWORD))
print(es.info())

# ✅ 인덱스 매핑 정의
MAPPINGS = {
    "mappings": {
        "properties": {
            "question": {"type": "text"},
            "recommendations": {
                "type": "nested",
                "properties": {
                    "name": {"type": "text"},
                    "description": {"type": "text"}
                }
            },
            "keywords": {"type": "keyword"}
        }
    }
}

# ✅ 기존 인덱스 삭제 및 새로 생성
if es.indices.exists(index=INDEX_NAME):
    es.indices.delete(index=INDEX_NAME)
    print(f"🗑 기존 인덱스 삭제 완료: {INDEX_NAME}")

es.indices.create(index=INDEX_NAME, body=MAPPINGS)
print(f"✅ 새 인덱스 생성 완료: {INDEX_NAME}")

# ✅ 파일 존재 확인
if not os.path.exists(DATA_PATH):
    raise FileNotFoundError(f"{DATA_PATH} 경로에 파일이 없습니다.")

# ✅ JSONL 로드
with open(DATA_PATH, "r", encoding="utf-8") as f:
    raw_documents = [json.loads(line) for line in f if line.strip()]

# ✅ 문서 인덱싱 (기존 내용 무시하고 덮어쓰기)
for idx, doc in enumerate(raw_documents):
    doc_id = f"q_{idx + 1}"
    try:
        parsed_answer = doc["answer"] if isinstance(doc["answer"], list) else json.loads(doc["answer"])
    except Exception:
        print(f"⚠ JSON 파싱 실패: {doc['question']}")
        parsed_answer = [{"name": "[FORMAT ERROR]", "description": str(doc.get('answer'))}]

    new_doc = {
        "question": doc["question"],
        "recommendations": parsed_answer,
        "keywords": doc.get("keywords", [])
    }

    es.index(index=INDEX_NAME, id=doc_id, document=new_doc)
    print(f"✅ 저장 완료: {doc['question']}")


print("\n✅ 모든 문서 Elasticsearch에 새로 저장 완료 (초기화 포함)")
