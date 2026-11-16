import json
import re

with open('session_history_payload.json', encoding='utf-8') as f:
    data = json.load(f)

history = data.get('history', [])
print(f"Agent: {data.get('agent_name')} | Total turns: {data.get('total_turns')}")
print('=' * 60)
for i, t in enumerate(history, 1):
    u = t.get('user_input', '')
    a = t.get('assistant_response', '')
    a_clean = re.sub(r'<[^>]+>', '', a).strip()
    print(f"[Turn {i}]")
    if u:
        print(f"  User:      {u}")
    if a_clean:
        print(f"  Assistant: {a_clean[:500]}")
    print()
