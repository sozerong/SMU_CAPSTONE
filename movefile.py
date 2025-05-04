import os
import shutil
from datetime import datetime

# ✅ 기준 디렉토리
BASE_DIR = "data"

# ✅ 오늘 날짜로 된 하위 폴더명 (예: 2025-05-03)
today_str = datetime.now().strftime("%Y-%m-%d")
target_dir = os.path.join(BASE_DIR, today_str)

# ✅ 폴더가 없으면 생성
os.makedirs(target_dir, exist_ok=True)

# ✅ 이동 대상 확장자
target_extensions = [".json", ".jsonl"]

# ✅ data 폴더 내 파일들을 탐색
for filename in os.listdir(BASE_DIR):
    filepath = os.path.join(BASE_DIR, filename)
    
    # 파일이고 확장자가 대상이면 이동
    if os.path.isfile(filepath) and os.path.splitext(filename)[1] in target_extensions:
        print(f"📦 이동: {filename} → {today_str}/")
        shutil.move(filepath, os.path.join(target_dir, filename))
