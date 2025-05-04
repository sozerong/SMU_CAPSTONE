import json, requests

with open("data/newnew_graphrag_answers.jsonl", "r", encoding="utf-8") as f:
    data = [json.loads(line) for line in f if line.strip()]

res = requests.post("https://port-0-fastapi-ma7qi2cl823545d5.sel4.cloudtype.app//save", json=data)
print(res.json())
