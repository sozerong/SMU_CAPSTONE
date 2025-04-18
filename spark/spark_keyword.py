from pyspark.sql import SparkSession
from pyspark.sql.functions import col, concat_ws
from pyspark.ml.feature import Tokenizer, CountVectorizer, IDF
import pandas as pd
import os
import json

# ✅ Spark 세션 생성
spark = SparkSession.builder \
    .appName("TFIDFKeywordExtract") \
    .config("spark.jars.packages", "org.apache.spark:spark-sql-kafka-0-10_2.12:3.3.0") \
    .getOrCreate()

# ✅ Kafka에서 데이터 읽기
kafka_df = spark.read \
    .format("kafka") \
    .option("kafka.bootstrap.servers", "localhost:9092") \
    .option("subscribe", "raw-youtube") \
    .option("startingOffsets", "earliest") \
    .load()

# ✅ JSON 파싱 및 텍스트 결합
df = kafka_df.selectExpr("CAST(value AS STRING) as json") \
    .selectExpr("from_json(json, 'video_id STRING, title STRING, description STRING') as data") \
    .select("data.*") \
    .dropDuplicates(["video_id"])  # 중복 제거
df = df.withColumn("text", concat_ws(" ", "title", "description"))

# ✅ 토큰화
tokenizer = Tokenizer(inputCol="text", outputCol="words")
df_words = tokenizer.transform(df)

# ✅ CountVectorizer + IDF
cv = CountVectorizer(inputCol="words", outputCol="raw_features", vocabSize=1000)
cv_model = cv.fit(df_words)
df_featurized = cv_model.transform(df_words)

idf = IDF(inputCol="raw_features", outputCol="features")
idf_model = idf.fit(df_featurized)
df_tfidf = idf_model.transform(df_featurized)

# ✅ Pandas로 변환 후 상위 키워드 추출
vocab = cv_model.vocabulary
rows = df_tfidf.select("video_id", "features").toPandas()

results = []
for _, row in rows.iterrows():
    feature = row["features"]
    indices = feature.indices
    values = feature.values
    tfidf_scores = list(zip(indices, values))
    top_keywords = sorted(tfidf_scores, key=lambda x: -x[1])[:5]
    keywords = [vocab[idx] for idx, _ in top_keywords]
    results.append({
        "video_id": row["video_id"],
        "keywords": keywords
    })

# ✅ JSONL 파일 저장 (video_id + keywords 배열 형태로)
os.makedirs("data", exist_ok=True)
with open("data/keywords.jsonl", "w", encoding="utf-8") as f:
    for item in results:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

print("✅ TF-IDF 기반 키워드 추출 완료 → data/keywords.jsonl")
