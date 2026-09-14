import sys
import time
import statistics
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DATABASE_URL = "postgresql://postgres:1011@localhost:5432/drop4life_benchmark"

engine = create_engine(DATABASE_URL)

QUERY = text("""
    SELECT id, user_id, blood_group, city, latitude, longitude
    FROM donors
    WHERE blood_group = 'O_POS'
      AND availability = 'AVAILABLE'
      AND is_active = true
""")

WARMUP = 20
ITERATIONS = 200


def percentile(values, p):
    values = sorted(values)
    index = int((p / 100) * (len(values) - 1))
    return values[index]


with engine.connect() as db:

    for _ in range(WARMUP):
        db.execute(QUERY).fetchall()

    times = []

    for _ in range(ITERATIONS):
        start = time.perf_counter()

        rows = db.execute(QUERY).fetchall()

        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)

print(f"Rows returned: {len(rows)}")
print(f"Iterations:    {ITERATIONS}")
print()
print(f"Min:     {min(times):.3f} ms")
print(f"Average: {statistics.mean(times):.3f} ms")
print(f"p50:     {percentile(times, 50):.3f} ms")
print(f"p95:     {percentile(times, 95):.3f} ms")
print(f"p99:     {percentile(times, 99):.3f} ms")
print(f"Max:     {max(times):.3f} ms")