# 🍽️ Menu Trend Graph Project

키워드를 기반으로 트렌디한 조합 키워드를 생성하고, 이를 LLM을 통해 분류/설명한 뒤 Neo4j에 구조화하여 저장하는 데이터 파이프라인 프로젝트입니다.

---

## 📌 프로젝트 개요

- 실시간 크롤링된 YouTube 데이터를 기반으로 키워드 추출 (TF-IDF)
- 연속 명사 기반 키워드 후처리 + LLM을 통해 카페메뉴 관련 키워드만 필터링
- LLM이 키워드 조합을 생성 (예: "노오븐 초코 치즈케이크")
- 생성된 키워드를 LLM Agent가 요약 정보 구성 (설명, 카테고리, 예시, 계절 등)
- 최종 결과를 Neo4j에 관계형 그래프 구조로 저장

---

## 📂 폴더 구조

```bash
project/
├── agent/                     # LLM Agent 관련 코드
│   ├── keyword_agent.py       # SerpAPI/Playwright 기반 정보 수집 Agent
│   ├── food_filter.py         # 카페메뉴 키워드 필터링 LLM
│   ├── keyword_combined.py    # LLM이 키워드 조합 생성 + count 반영
│   └── keyword_info_collector.py # 조합 키워드에 대해 정보 수집 Agent
│
├── data/                      # 중간 생성 데이터
│   ├── keywords.jsonl
│   ├── filtered_keywords_with_count.jsonl
│   ├── generated_keywords_with_new_count.jsonl
│   └── neo4j_ready_keywords.jsonl
│
├── kafka/                     # Kafka 기반 YouTube 데이터 수집
│   └── youtube_crawler.py
│
├── spark/                     # Spark 기반 TF-IDF 추출
│   └── spark_keyword.py
│
├── neo4j/                     # Neo4j 구조 저장 관련 코드
│   ├── neo4j_schema.py        # 생성된 키워드와 정보, 관계 저장 코드
│   └── reset_graph.py         # 전체 그래프 삭제 (초기화)
│
├── configs/                   # 환경변수 파일 (.env)
│   └── .env
│
├── .gitignore
└── README.md



1. kafka/youtube_crawler.py
2. spark/spark_keyword.py
3. agent/food_fillter.py
4. agent/keyword_info_collector.py
5. neo4j/neo4j_schema.py
6. GragpRAG/graphrag_aq.py
7. Postgresql/save_PSQL.py
8. movefile.py