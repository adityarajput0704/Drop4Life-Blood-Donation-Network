#!/bin/bash

set -e

DB="drop4life_benchmark"
PGUSER="postgres"
DBURL="postgresql://postgres:1011@localhost:5432/$DB"

for SIZE in 1000 10000 50000 100000
do
    echo ""
    echo "======================================"
    echo " BENCHMARK: $SIZE DONORS"
    echo "======================================"

    # Reset donor/user data
    psql -U "$PGUSER" -d "$DB" -c \
      "TRUNCATE TABLE donors, users RESTART IDENTITY CASCADE;"

    # Seed exact dataset size
    python benchmarks/seed_data.py --count "$SIZE"

    # Verify
    COUNT=$(psql -U "$PGUSER" -d "$DB" -t -c \
      "SELECT COUNT(*) FROM donors;" | xargs)

    echo "Verified donors: $COUNT"

    if [ "$COUNT" -ne "$SIZE" ]; then
        echo "ERROR: Expected $SIZE donors, got $COUNT"
        exit 1
    fi

    # Run Locust
    locust -f benchmarks/locustfile.py \
      --host http://127.0.0.1:8000 \
      --headless \
      -u 50 \
      -r 10 \
      -t 60s \
      --csv="benchmarks/results/${SIZE}_donors"
done

echo ""
echo "======================================"
echo " ALL BENCHMARKS COMPLETE"
echo "======================================"
