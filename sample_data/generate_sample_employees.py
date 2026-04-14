"""
Generates a sample employee table CSV for testing headcount measures.

Each row is an employee with a start date and optional end date (NULL if active).
Headcount for a period = employees where start_date <= period_end
                         AND (end_date IS NULL OR end_date >= period_start)

Run:
  python sample_data/generate_sample_employees.py
"""

import csv
import random
from datetime import date, timedelta

random.seed(99)

ENTITIES = ["North", "South", "West"]

# Target headcount per entity at start of 2024
STARTING_HEADCOUNT = {"North": 40, "South": 30, "West": 25}

# Probability of a termination happening in any given month
MONTHLY_TERM_RATE = 0.02

# Probability of a new hire happening in any given month per entity
MONTHLY_HIRE_RATE = 0.03


def random_date(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))


def generate_employees():
    rows = []
    employee_id = 1

    # Seed employees active before 2024
    for entity, count in STARTING_HEADCOUNT.items():
        for _ in range(count):
            start = random_date(date(2021, 1, 1), date(2023, 12, 31))
            rows.append({
                "employee_id": f"E{employee_id:04d}",
                "entity": entity,
                "start_date": start.isoformat(),
                "end_date": "",
            })
            employee_id += 1

    # Simulate terminations and hires month by month through 2024
    sim_start = date(2024, 1, 1)
    sim_end = date(2024, 12, 31)
    current = sim_start

    while current <= sim_end:
        month_end = (current.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

        for row in rows:
            # Only terminate active employees
            if row["end_date"] == "" and random.random() < MONTHLY_TERM_RATE:
                term_date = random_date(current, month_end)
                row["end_date"] = term_date.isoformat()

        # New hires
        for entity in ENTITIES:
            if random.random() < MONTHLY_HIRE_RATE:
                hire_date = random_date(current, month_end)
                rows.append({
                    "employee_id": f"E{employee_id:04d}",
                    "entity": entity,
                    "start_date": hire_date.isoformat(),
                    "end_date": "",
                })
                employee_id += 1

        # Advance one month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    return rows


def main():
    output_path = "sample_data/sample_employees.csv"
    rows = generate_employees()
    fieldnames = ["employee_id", "entity", "start_date", "end_date"]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Generated {len(rows):,} employees → {output_path}")


if __name__ == "__main__":
    main()
