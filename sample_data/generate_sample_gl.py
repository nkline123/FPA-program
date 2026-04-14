"""
Generates a realistic sample General Ledger CSV for testing.

Account structure:
  4000-4999  Revenue
  5000-5999  Cost of Goods Sold (COGS)
  6000-6999  Operating Expenses (Opex)
  7000-7999  Interest & Other

Sign convention: credits are negative (standard accounting).
  Revenue accounts → negative amounts (credit balances)
  Expense accounts → positive amounts (debit balances)

Run:
  python sample_data/generate_sample_gl.py
"""

import csv
import random
from datetime import date, timedelta

random.seed(42)

SCENARIOS = {
    "Actual": {"start": date(2024, 1, 1), "end": date(2024, 12, 31)},
    "Budget": {"start": date(2024, 1, 1), "end": date(2025, 12, 31)},
}

ENTITIES = ["North", "South", "West"]

# account_id → (description, sign, monthly_base, monthly_stddev)
# sign: -1 for credit accounts (revenue), +1 for debit accounts (expense)
ACCOUNTS = {
    "4000": ("Product Revenue",        -1, 500_000, 50_000),
    "4010": ("Service Revenue",        -1, 150_000, 20_000),
    "5000": ("Cost of Goods Sold",     +1, 200_000, 25_000),
    "5010": ("Direct Labor",           +1,  80_000, 10_000),
    "6000": ("Salaries & Wages",       +1, 120_000,  8_000),
    "6010": ("Rent & Facilities",      +1,  30_000,  1_000),
    "6020": ("Marketing",              +1,  40_000, 15_000),
    "6030": ("Software & Subscriptions",+1, 15_000,  2_000),
    "6040": ("Travel & Entertainment", +1,  10_000,  5_000),
    "7000": ("Interest Expense",       +1,  12_000,    500),
}

TRANSACTIONS_PER_ACCOUNT_PER_MONTH = 8


def random_dates_in_month(year: int, month: int, n: int):
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    days = (end - start).days + 1
    return sorted(start + timedelta(days=random.randint(0, days - 1)) for _ in range(n))


def generate_rows():
    rows = []
    for scenario, bounds in SCENARIOS.items():
        # Walk month by month
        current = bounds["start"].replace(day=1)
        end = bounds["end"]
        while current <= end:
            y, m = current.year, current.month
            for account_id, (description, sign, base, stddev) in ACCOUNTS.items():
                # Spread the monthly total across multiple transactions per entity
                for entity in ENTITIES:
                    entity_base = base / len(ENTITIES)
                    monthly_total = max(0, random.gauss(entity_base, stddev / len(ENTITIES)))
                    dates = random_dates_in_month(y, m, TRANSACTIONS_PER_ACCOUNT_PER_MONTH)
                    # Distribute total across transactions (last one gets remainder)
                    splits = sorted(random.random() for _ in range(len(dates) - 1))
                    splits = [0] + splits + [1]
                    for i, txn_date in enumerate(dates):
                        txn_amount = (splits[i + 1] - splits[i]) * monthly_total * sign
                        rows.append({
                            "scenario": scenario,
                            "account_id": account_id,
                            "date": txn_date.isoformat(),
                            "amount": round(txn_amount, 2),
                            "entity": entity,
                            "description": description,
                            "source": "sample_gl",
                        })
            # Advance one month
            if m == 12:
                current = date(y + 1, 1, 1)
            else:
                current = date(y, m + 1, 1)
    return rows


def main():
    output_path = "sample_data/sample_gl.csv"
    rows = generate_rows()
    fieldnames = ["scenario", "account_id", "date", "amount", "entity", "description", "source"]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Generated {len(rows):,} rows → {output_path}")


if __name__ == "__main__":
    main()
