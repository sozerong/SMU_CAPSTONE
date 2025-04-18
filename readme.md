project-root/
├── docker/
│   ├── docker-compose.yml        # Kafka + Zookeeper + Spark 등 실행
│   └── init/                     # 초기 설정 스크립트 (옵션)
│
├── kafka/
│   └── youtube_crawler.py        # YouTube → Kafka 발행 스크립트
│
├── spark/
│   ├── spark_tfidf_keywords.py   # Kafka → Spark → TF-IDF 키워드 추출
│   ├── spark_to_neo4j.py         # Spark → Neo4j 관계 저장
│   └── spark_to_elasticsearch.py # Spark → Elasticsearch 저장
│
├── agent/
│   ├── keyword_agent.py          # LLM Agent로 키워드 정보 수집
│   └── crawler_cache.py          # (선택) Redis로 크롤링 결과 캐싱
│
├── elasticsearch/
│   ├── init_es_index.py          # ES 인덱스 생성 스크립트
│   └── embedding_utils.py        # 벡터 임베딩 생성 도구 (sentence-transformers 등)
│
├── neo4j/
│   └── neo4j_schema.py           # 노드/관계 생성용 초기 스크립트
│
├── api/
│   ├── main.py                   # FastAPI 서버
│   ├── chatgpt_response.py      # GPT 응답 생성
│   └── query_service.py         # ES/Neo4j 질의 + 통합
│
├── configs/
│   ├── es_config.json            # Elasticsearch 관련 설정
│   ├── neo4j_config.json         # Neo4j 연결 정보
│   └── .env                      # API KEY 등 환경 변수
│
├── data/
│   ├── raw/                      # 크롤링된 원시 JSON (optional)
│   ├── keywords.jsonl            # Spark에서 추출된 키워드
│   └── keyword_infos.jsonl       # LLM Agent 크롤링 결과
│
└── README.md


1. kafka/youtube_crawler.py
2. spark/spark_keyword.py
3. agent/food_fillter.py
4. food_labeler.py
5. 
6. agent/keyword_combined.py
7. agent/keyword_info_collector.py
agent/keyword_inf_collect.py