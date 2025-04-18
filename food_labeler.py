import json
import os

input_path = "data/filtered_keywords_with_count.jsonl"
output_path = "data/food_keywords_labeled.jsonl"

# 이미 저장된 키워드는 다시 묻지 않도록
already_labeled = set()
if os.path.exists(output_path):
    with open(output_path, "r", encoding="utf-8") as f_out:
        for line in f_out:
            obj = json.loads(line)
            already_labeled.add(obj["keyword"])

with open(input_path, "r", encoding="utf-8") as f_in, \
     open(output_path, "a", encoding="utf-8") as f_out:

    for line in f_in:
        obj = json.loads(line)
        keyword = obj["keyword"]

        if keyword in already_labeled:
            continue

        while True:
            answer = input(f"👉 '{keyword}' 는 음식인가요? (y/n): ").strip().lower()
            if answer == 'y':
                f_out.write(json.dumps(obj, ensure_ascii=False) + "\n")
                print("✅ 저장됨")
                break
            elif answer == 'n':
                print("❌ 저장 안 함")
                break
            else:
                print("⚠️ y 또는 n으로 입력해주세요.")
