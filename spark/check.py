from pyspark.sql import SparkSession
from pyspark.sql.functions import col, from_json
from pyspark.sql.types import StructType, StringType

spark = SparkSession.builder \
    .appName("KafkaSchemaTest") \
    .config("spark.jars.packages", "org.apache.spark:spark-sql-kafka-0-10_2.12:3.3.0") \
    .getOrCreate()

# Kafka에서 데이터 불러오기
df_kafka = spark.read \
    .format("kafka") \
    .option("kafka.bootstrap.servers", "localhost:9092") \
    .option("subscribe", "raw-youtube") \
    .option("startingOffsets", "earliest") \
    .load()

# Kafka 메시지 구조 정의
schema = StructType() \
    .add("video_id", StringType()) \
    .add("title", StringType()) \
    .add("description", StringType())

# value를 문자열로 변환하고 JSON 파싱
df_json = df_kafka.selectExpr("CAST(value AS STRING) as json_str") \
    .withColumn("data", from_json(col("json_str"), schema)) \
    .select("data.*")

# 중복 제거 (video_id 기준)
df_dedup = df_json.dropDuplicates(["video_id"])

df_dedup.show(truncate=False)
