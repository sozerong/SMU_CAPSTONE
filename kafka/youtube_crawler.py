import os
import json
import time
from kafka import KafkaProducer
from googleapiclient.discovery import build
from dotenv import load_dotenv

# ✅ 환경 변수 로드
load_dotenv(dotenv_path="C:/GitHub/project/configs/.env")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

# ✅ 크롤링할 유튜브 채널 리스트
CHANNEL_IDS = [
    "UCyar0OYt0LoPzkkWcQAo6OA",  # 젼언니
    "UCPY1I65kTjzcbh6NLeI1pYw",  # 해언
    "UCNYE6N9YZmsUyReBLzE9x9Q",  # 이상한 과자가게
    "UCJ66AvaHJ2pHD_-AyUailuQ",  # 가오니의 메뉴판
    "UC-pXaRbTkhOnOUjsv96QHMg",  # 코저트
    "UCcfKn5ex1g8zgK4eYtbReoA",  # 코지
    "UCrI2RYBoPoar4wY3WsuX-oQ",  # 잡식공룡
    "UCnLeqvS4Rdbl8Mv-AjEWH_w",  # 아누누누
    "UCf9sl-IcwNXDqWwWwp4vEwg",  # 나도
]

# ✅ Kafka Producer 설정
KAFKA_TOPIC = "raw-youtube"
producer = KafkaProducer(
    bootstrap_servers='localhost:9092',
    value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode('utf-8')
)

# ✅ YouTube API 클라이언트
youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def fetch_and_publish(channel_id):
    print(f"\n🚀 Fetching from channel: {channel_id}")

    next_page_token = None
    total_count = 0

    while total_count < 60:
        req = youtube.search().list(
            part="snippet",
            channelId=channel_id,
            maxResults=50,
            pageToken=next_page_token,
            order="date"
        )
        res = req.execute()

        for item in res["items"]:
            if item["id"]["kind"] != "youtube#video":
                continue

            video_id = item["id"]["videoId"]
            title = item["snippet"].get("title", "")
            description = item["snippet"].get("description", "")

            data = {
                "video_id": video_id,
                "title": title,
                "description": description
            }

            producer.send(KAFKA_TOPIC, value=data)
            print(f"✅ Sent to Kafka: {video_id} | {title}")
            total_count += 1

        next_page_token = res.get("nextPageToken")
        if not next_page_token:
            break

        time.sleep(0.5)  # 과도한 요청 방지


if __name__ == '__main__':
    for cid in CHANNEL_IDS:
        fetch_and_publish(cid)

    producer.flush()
