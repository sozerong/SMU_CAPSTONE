from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta
import os

default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

dag = DAG(
    dag_id='trend_insight_pipeline',
    default_args=default_args,
    description='유튜브 기반 트렌디 디저트 메뉴 추천 자동화 DAG',
    schedule='@weekly',
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=['graphRAG', 'trend', 'dessert']
)

# ✅ 경로는 Airflow 컨테이너 내 기준임 (docker-compose에 ./:/opt/airflow 매핑 가정)
def run_youtube_kafka():
    os.system("python /opt/airflow/kafka/youtube_crawler.py")

def run_spark_keyword_extract():
    os.system("python /opt/airflow/spark/spark_keyword.py")

def run_keyword_filter():
    os.system("python /opt/airflow/agent/food_filter.py")

def run_graphrag_answer():
    os.system("python /opt/airflow/GragpRAG/graphrag_aq.py")

def run_elasticsearch_index():
    os.system("python /opt/airflow/ElasticSearch/es_indexer.py")

# ✅ 태스크 설정
t1 = PythonOperator(task_id='kafka_consume_youtube', python_callable=run_youtube_kafka, dag=dag)
t2 = PythonOperator(task_id='extract_keywords_with_spark', python_callable=run_spark_keyword_extract, dag=dag)
t3 = PythonOperator(task_id='filter_dessert_keywords', python_callable=run_keyword_filter, dag=dag)
t4 = PythonOperator(task_id='generate_graphrag_answer', python_callable=run_graphrag_answer, dag=dag)
t5 = PythonOperator(task_id='index_to_elasticsearch', python_callable=run_elasticsearch_index, dag=dag)

# ✅ 태스크 순서 연결
t1 >> t2 >> t3 >> t4 >> t5
