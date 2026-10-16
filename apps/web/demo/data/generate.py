"""Demo dataset with a designed story.

Real enough to exercise every module, and shaped so each one has something to
find rather than something to decline:

  * 12 months of history          → Holt-Winters is still out of reach at 12,
                                     but linear trend has plenty to work with
  * 28 customers                  → above the churn model's 20-customer floor
  * 6 of them stop buying early   → churn has actual signal, not noise
  * a March collapse              → the sentinel has an anomaly to catch
  * North collapses as West surges → the investigator's offsetting case
  * deliberate mess               → the quality tab has something to report
"""

import csv
import random
from datetime import date

random.seed(7)

REGIONS = ["North", "South", "East", "West"]
CATEGORIES = {
    "P-1": ("Laptop Pro 14", "Electronics", 1450),
    "P-2": ("Desk Chair Ergo", "Furniture", 380),
    "P-3": ("Monitor 4K 27\"", "Electronics", 520),
    "P-4": ("Standing Desk", "Furniture", 690),
    "P-5": ("Noise-Cancel Headset", "Electronics", 240),
    "P-6": ("Filing Cabinet", "Furniture", 310),
}

NAMES = [
    "Ada Lovelace", "Grace Hopper", "Alan Turing", "Katherine Johnson",
    "Joan Clarke", "Barbara Liskov", "Margaret Hamilton", "Edsger Dijkstra",
    "Donald Knuth", "Frances Allen", "Adele Goldberg", "Tim Berners-Lee",
    "Radia Perlman", "Leslie Lamport", "Shafi Goldwasser", "Ken Thompson",
    "Dennis Ritchie", "Anita Borg", "Jean Bartik", "Karen Sparck Jones",
    "Sophie Wilson", "Lynn Conway", "Carol Shaw", "Evelyn Boyd Granville",
    "Mary Kenneth Keller", "Erna Hoover", "Annie Easley", "Dorothy Vaughan",
]
COUNTRIES = ["United Kingdom", "United States", "Ireland", "Germany", "Canada"]
SEGMENTS = ["Enterprise", "SMB", "Mid-market"]

customers = []
for index, name in enumerate(NAMES, start=1):
    customers.append(
        {
            "customer_id": f"C-{index:03d}",
            "Customer Name": name,
            "Country": COUNTRIES[index % len(COUNTRIES)],
            "Segment": SEGMENTS[index % len(SEGMENTS)],
            # Home region; most orders come from it.
            "_region": REGIONS[index % len(REGIONS)],
        }
    )

# Six customers churn: they buy for the first few months and then stop.
lapsed = {c["customer_id"] for c in customers[:6]}

MONTHS = [(2025, m) for m in range(6, 13)] + [(2026, m) for m in range(1, 6)]

orders = []
order_id = 1000
for year, month in MONTHS:
    index = MONTHS.index((year, month))

    # Baseline grows steadily; March 2026 collapses; the last month is the
    # North/West swap.
    volume = 14 + index
    if (year, month) == (2026, 3):
        volume = 6           # the anomaly
    if (year, month) == (2026, 5):
        volume = 26

    for _ in range(volume):
        customer = random.choice(customers)
        if customer["customer_id"] in lapsed and index > 3:
            continue         # these stopped buying after month 4

        region = customer["_region"]
        # The last month: North collapses, West absorbs it.
        if (year, month) == (2026, 5):
            if region == "North" and random.random() < 0.85:
                continue
            if random.random() < 0.45:
                region = "West"

        product_id = random.choice(list(CATEGORIES))
        _, _, base = CATEGORIES[product_id]
        units = random.randint(1, 5)
        revenue = round(base * units * random.uniform(0.85, 1.15), 2)

        order_id += 1
        orders.append(
            {
                "order_id": order_id,
                "customer_id": customer["customer_id"],
                "product_id": product_id,
                "Order Date": date(year, month, random.randint(1, 28)).isoformat(),
                # Currency as text, the way a real export arrives.
                "Revenue": f'"${revenue:,.2f}"' if revenue >= 1000 else f"${revenue:.2f}",
                "Units": units,
                "Unit Cost": round(base * 0.62, 2),
                "Region": region,
            }
        )

# Deliberate mess, so the cleaning report is not empty.
orders[12]["Revenue"] = "N/A"                       # a missing value
orders[30]["Region"] = "  West"                     # padded whitespace
orders.append(dict(orders[-1]))                     # an exact duplicate

with open("orders.csv", "w", newline="", encoding="utf-8") as handle:
    handle.write("order_id,customer_id,product_id,Order Date,Revenue,Units,Unit Cost,Region\n")
    for row in orders:
        handle.write(
            f'{row["order_id"]},{row["customer_id"]},{row["product_id"]},'
            f'{row["Order Date"]},{row["Revenue"]},{row["Units"]},'
            f'{row["Unit Cost"]},{row["Region"]}\n'
        )

with open("customers.csv", "w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(
        handle, fieldnames=["customer_id", "Customer Name", "Country", "Segment"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(customers)

with open("products.csv", "w", newline="", encoding="utf-8") as handle:
    writer = csv.writer(handle)
    writer.writerow(["product_id", "Product Name", "Category"])
    for pid, (name, category, _) in CATEGORIES.items():
        writer.writerow([pid, name, category])

print(f"orders.csv    {len(orders)} rows")
print(f"customers.csv {len(customers)} rows ({len(lapsed)} lapse early)")
print(f"products.csv  {len(CATEGORIES)} rows")
