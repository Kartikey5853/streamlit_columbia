import json

COLUMBIA_FILE = "columbia.json"
AMAZON_FILE = "amazon.json"


def load_json(filename):
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


# -------------------------------------------------
# Load files
# -------------------------------------------------

columbia = load_json(COLUMBIA_FILE)
amazon = load_json(AMAZON_FILE)

columbia_products = columbia["products"]
amazon_products = amazon["products"]

print(f"Loaded Columbia products : {len(columbia_products)}")
print(f"Loaded Amazon products   : {len(amazon_products)}")


# -------------------------------------------------
# Build Amazon UPC lookup
# -------------------------------------------------

amazon_lookup = {}

for product in amazon_products.values():

    upc = str(product.get("upc", "")).strip()

    if upc:
        amazon_lookup[upc] = product


# -------------------------------------------------
# Compare
# -------------------------------------------------

matches = []
missing = []

for product in columbia_products:

    for variant in product.get("variant_mapping", []):

        ean = str(variant.get("ean", "")).strip()

        if not ean:
            continue

        if ean in amazon_lookup:

            amazon_product = amazon_lookup[ean]

            matches.append({
                "sku": variant["sku"],
                "size": variant["size"],
                "ean": ean,
                "amazon_title": amazon_product["title"]
            })

        else:

            missing.append({
                "sku": variant["sku"],
                "size": variant["size"],
                "ean": ean
            })


# -------------------------------------------------
# Results
# -------------------------------------------------

print("\n==============================")
print("RESULTS")
print("==============================")
print("Amazon UPCs      :", len(amazon_lookup))
print("Matched          :", len(matches))
print("Missing          :", len(missing))

print("\nFirst 20 Matches")
for item in matches[:20]:
    print(
        f'{item["sku"]} ({item["size"]}) '
        f'-> {item["ean"]}'
    )

print("\nFirst 20 Missing")
for item in missing[:20]:
    print(
        f'{item["sku"]} ({item["size"]}) '
        f'-> {item["ean"]}'
    )


# -------------------------------------------------
# Unique barcode statistics
# -------------------------------------------------

columbia_eans = {
    str(v["ean"]).strip()
    for p in columbia_products
    for v in p.get("variant_mapping", [])
    if v.get("ean")
}

amazon_upcs = set(amazon_lookup.keys())

intersection = columbia_eans & amazon_upcs

print("\n==============================")
print("UNIQUE BARCODE STATS")
print("==============================")
print("Unique Columbia EANs :", len(columbia_eans))
print("Unique Amazon UPCs   :", len(amazon_upcs))
print("Common Barcodes      :", len(intersection))
print("Coverage             : {:.2f}%".format(
    len(intersection) / len(columbia_eans) * 100
))