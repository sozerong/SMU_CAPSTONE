import os
import json
from kafka import KafkaProducer
from googleapiclient.discovery import build
from dotenv import load_dotenv
import time

# ✅ 환경 변수 로드
load_dotenv(dotenv_path="C:/GitHub/project/configs/.env")  
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

# ✅ 크롤링할 유튜브 채널 리스트
CHANNEL_IDS = [
    "UCyar0OYt0LoPzkkWcQAo6OA",  # 젼언니
    "UCrJ0RPeCjQcwvJJHtpKHHAA",  # 곰쓰 쉬운 베이킹
    "UCNYE6N9YZmsUyReBLzE9x9Q"   # 이상한 과자가게
]

# ✅ Kafka Producer 설정
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

    while total_count < 80:  # 최대 100개까지 반복 조회
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
            title = item["snippet"]["title"]
            description = item["snippet"].get("description", "")

            data = {
                "video_id": video_id,
                "title": title,
                "description": description
            }

            producer.send("raw-youtube", value=data)
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