"""More diverse training sessions targeting specific task types."""
import sys, json, time, urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://localhost:8000"

SESSIONS = [
    "Implement a production-ready Python binary search tree with insert, delete, search, and in-order traversal methods.",
    "Describe the system design of Netflix's video streaming architecture. Focus on CDN usage, video encoding pipeline, and recommendation system.",
    "Write unit tests for a Python function `parse_date(s: str) -> datetime` that parses dates in formats: YYYY-MM-DD, DD/MM/YYYY, MM-DD-YYYY.",
    "Реализуй на Python класс Stack с методами push, pop, peek, is_empty и size. Добавь type hints и docstrings.",
    "Объясни что такое индексы в базах данных: B-tree, Hash, Composite индексы. Когда использовать каждый?",
]

def send(msg):
    payload = json.dumps({"message": msg}).encode()
    req = urllib.request.Request(f"{BASE}/api/chat", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            d = json.loads(resp.read())
            print(f"  task_type={d.get('task_type','?')} score={d.get('critic_score','?')} model={d.get('model_used','?')}")
            return True
    except Exception as ex:
        print(f"  Error: {ex}")
        return False

for i, msg in enumerate(SESSIONS, 1):
    print(f"[{i}/{len(SESSIONS)}] {msg[:70]}...")
    t0 = time.time()
    send(msg)
    print(f"  {time.time()-t0:.1f}s\n")
    time.sleep(2)

# Check final stats via port 8001 (new server with by_type_detail)
print("\nFinal stats (new server):")
try:
    with urllib.request.urlopen("http://localhost:8001/api/metrics", timeout=10) as r:
        m = json.loads(r.read())
    print(f"  Total: {m['total']}, Avg score: {m['avg_score']:.2f}")
    for tt, d in sorted(m.get('by_type_detail', {}).items()):
        print(f"  {tt:15s}: n={d['count']:2d}  score={d['avg_score']*100:5.1f}%  time={d['avg_elapsed']:5.1f}s  iter={d['avg_iter']:.1f}")
except Exception as e:
    print(f"  Error: {e}")
