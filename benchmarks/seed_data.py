"""
Synthetic benchmark data generator for Drop4Life.

This script creates users + donor profiles directly through SQLAlchemy.
It is intended ONLY for benchmark/test databases.

Usage:
    python benchmarks/seed_data.py --count 1000
    python benchmarks/seed_data.py --count 10000
    python benchmarks/seed_data.py --count 50000
    python benchmarks/seed_data.py --count 100000
"""

import argparse
import random
import sys
import uuid
from pathlib import Path

from faker import Faker
from sqlalchemy import func


PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from backend.models.user import User
from backend.models.donor import Donor, BloodGroupEnum, AvailabilityEnum
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BENCHMARK_DATABASE_URL = "postgresql://postgres:1011@localhost:5432/drop4life_benchmark"

engine = create_engine(BENCHMARK_DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

fake = Faker()

MUMBAI_LAT = 19.0760
MUMBAI_LNG = 72.8777

# Approximate bounding box around the Mumbai metropolitan area.
LAT_MIN = 18.85
LAT_MAX = 19.35

LNG_MIN = 72.75
LNG_MAX = 73.10


# ---------------------------------------------------------------------------
# Blood-group distribution
#
# Weights do not need to represent exact real-world epidemiological data.
# They simply prevent the benchmark dataset from being artificially uniform.
# ---------------------------------------------------------------------------

BLOOD_GROUP_WEIGHTS = {
    BloodGroupEnum.O_POS: 0.35,
    BloodGroupEnum.A_POS: 0.30,
    BloodGroupEnum.B_POS: 0.15,
    BloodGroupEnum.AB_POS: 0.05,
    BloodGroupEnum.O_NEG: 0.05,
    BloodGroupEnum.A_NEG: 0.05,
    BloodGroupEnum.B_NEG: 0.04,
    BloodGroupEnum.AB_NEG: 0.01,
}


def random_blood_group() -> BloodGroupEnum:
    """Return a blood group using the configured weighted distribution."""

    groups = list(BLOOD_GROUP_WEIGHTS.keys())
    weights = list(BLOOD_GROUP_WEIGHTS.values())

    return random.choices(groups, weights=weights, k=1)[0]


def random_location() -> tuple[float, float]:
    """Generate a random latitude/longitude inside the benchmark area."""

    latitude = random.uniform(LAT_MIN, LAT_MAX)
    longitude = random.uniform(LNG_MIN, LNG_MAX)

    return latitude, longitude


def create_users_and_donors(count: int, batch_size: int = 1000) -> None:
    """
    Generate `count` synthetic users and donor profiles.

    Inserts are committed in batches to avoid keeping the entire dataset
    inside one SQLAlchemy transaction.
    """

    db = SessionLocal()

    try:
        existing_donors = db.query(func.count(Donor.id)).scalar() or 0

        print(f"Existing donors in database: {existing_donors}")
        print(f"Creating {count:,} synthetic donors...")
        print(f"Batch size: {batch_size:,}")
        print()

        created = 0

        while created < count:
            current_batch_size = min(batch_size, count - created)

            users = []
            donors = []

            for _ in range(current_batch_size):
                user_id = f"benchmark-{uuid.uuid4()}"

                user = User(
                    id=user_id,
                    firebase_uid=user_id,
                    email=f"{user_id}@benchmark.drop4life.test",
                    full_name=fake.name(),
                    phone=fake.numerify(text="9#########"),
                    role="donor",
                    is_active=True,
                )

                latitude, longitude = random_location()

                donor = Donor(
                    user_id=user_id,
                    blood_group=random_blood_group(),
                    city="Mumbai",
                    age=random.randint(18, 60),
                    availability=AvailabilityEnum.AVAILABLE,
                    is_active=True,
                    last_donation_date=None,
                    cooldown_until=None,
                    latitude=latitude,
                    longitude=longitude,
                )

                users.append(user)
                donors.append(donor)

            # Add users first because donors reference users.id.
            db.add_all(users)
            db.flush()

            db.add_all(donors)
            db.commit()

            created += current_batch_size

            print(
                f"Created {created:,}/{count:,} donors "
                f"({created / count:.1%})"
            )

        print()
        print("Seed completed successfully.")

        total_donors = db.query(func.count(Donor.id)).scalar() or 0
        total_users = db.query(func.count(User.id)).scalar() or 0

        print(f"Total users in database:  {total_users:,}")
        print(f"Total donors in database: {total_donors:,}")

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed synthetic Drop4Life donor data."
    )

    parser.add_argument(
        "--count",
        type=int,
        required=True,
        help="Number of synthetic donors to create.",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of users/donors inserted per transaction batch.",
    )

    args = parser.parse_args()

    if args.count <= 0:
        raise ValueError("--count must be greater than 0.")

    if args.batch_size <= 0:
        raise ValueError("--batch-size must be greater than 0.")

    create_users_and_donors(
        count=args.count,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()