"""
Diverse training sessions to populate metrics with multiple task types.
Uses port 28800 (FreePalp default).
"""
import sys, json, time, urllib.request, urllib.error

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://localhost:28800"

SESSIONS = [
    # coding_small (x6)
    ("Write a Python function that reverses a string without using slicing [::-1]. Show 3 approaches.", None),
    ("Write a Python decorator @retry(times=3, delay=1.0) that retries a function on exception.", None),
    ("Write a Python function that flattens a nested list of any depth using recursion.", None),
    ("Write a Python class for a simple stack with push, pop, peek and is_empty methods with type hints.", None),
    ("Write a Python function that finds all duplicates in a list and returns them as a set.", None),
    ("Write a Python context manager that temporarily changes the current working directory.", None),

    # coding_large (x4)
    ("Write a complete Python class for a thread-safe LRU cache with max_size parameter, get() and put() methods, eviction logic.", None),
    ("Write a Python async context manager for database connection pooling with configurable pool size, acquire timeout, and health checks.", None),
    ("Write a complete Python implementation of a binary search tree with insert, search, delete, and in-order traversal methods.", None),
    ("Write a Python class that implements a rate limiter using the token bucket algorithm with async support.", None),

    # architecture (x4)
    ("Design the architecture of a URL shortener service like bit.ly. Describe components, database schema, and scaling strategy.", None),
    ("What is the difference between microservices and monolithic architecture? When would you choose each?", None),
    ("Design a real-time chat system supporting 10 million concurrent users. What tech stack and architecture would you use?", None),
    ("Explain the CQRS pattern and event sourcing. When should you use them together?", None),

    # text (x4)
    ("Write a concise technical README for a Python library called 'fastcache' that provides in-memory caching with TTL support.", None),
    ("Explain the concept of eventual consistency in distributed systems in simple terms with a practical example.", None),
    ("Write a short blog post explaining WebSockets vs HTTP long-polling for real-time apps, with pros and cons of each.", None),
    ("Explain the difference between concurrency and parallelism with Python code examples.", None),

    # review (x4)
    ("Review this Python code for bugs and improvements:\ndef get_user(id):\n  users = load_all_users()\n  for u in users:\n    if u['id'] == id: return u", None),
    ("Review this code:\nimport subprocess\ndef run_cmd(user_input):\n    result = subprocess.run(f'ls {user_input}', shell=True, capture_output=True)\n    return result.stdout", None),
    ("Review this async code:\nasync def fetch_all(urls):\n    results = []\n    for url in urls:\n        r = await fetch(url)\n        results.append(r)\n    return results", None),
    ("Review this Python class:\nclass DB:\n    conn = sqlite3.connect('app.db')\n    def query(self, sql):\n        return self.conn.execute(sql).fetchall()", None),

    # shell (x3)
    ("What Linux commands would you use to find all Python files modified in the last 7 days and count lines of code in each?", None),
    ("How do you monitor CPU and memory usage of a specific process in Linux and log it every 5 seconds to a file?", None),
    ("Write a bash one-liner to find the top 10 largest files in a directory recursively.", None),

    # general (x3)
    ("What is the CAP theorem and how does it apply when choosing a database for a distributed system?", None),
    ("Explain the difference between a process and a thread. When would you use one over the other in Python?", None),
    ("What are the most common causes of memory leaks in Python and how do you detect them?", None),
]


def send(msg):
    payload = json.dumps({"message": msg}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
            score = data.get("critic_score", "?")
            task_type = data.get("task_type", "?")
            model = data.get("model_used", "?")
            elapsed = data.get("elapsed", 0)
            print(f"  ✅ [{task_type}] score={score} model={model} ({elapsed:.1f}s)")
            return True
    except urllib.error.HTTPError as e:
        body = e.read()[:200]
        print(f"  ❌ HTTP {e.code}: {body}")
        return False
    except Exception as ex:
        print(f"  ❌ Error: {ex}")
        return False


print(f"Running {len(SESSIONS)} diverse training sessions on {BASE}...\n")
ok_count = 0
for i, (msg, _) in enumerate(SESSIONS, 1):
    print(f"[{i}/{len(SESSIONS)}] {msg[:70]}...")
    t0 = time.time()
    ok = send(msg)
    elapsed = time.time() - t0
    if ok:
        ok_count += 1
    print(f"  → total {elapsed:.1f}s\n")
    time.sleep(1)

print(f"\n✅ Done! {ok_count}/{len(SESSIONS)} succeeded")
print("Checking updated metrics...")
try:
    with urllib.request.urlopen(f"{BASE}/api/metrics", timeout=10) as r:
        m = json.loads(r.read())
    print(f"Total tasks: {m['total']}")
    print(f"Avg score:   {m.get('avg_score', 0):.2f}")
    print("By type:")
    for tt, d in m.get('by_type_detail', {}).items():
        bar = "█" * int(d['avg_score'] * 10) + "░" * (10 - int(d['avg_score'] * 10))
        print(f"  {tt:15} [{bar}] {d['avg_score']:.2f}  count={d['count']}")
except Exception as e:
    print(f"Could not fetch metrics: {e}")
