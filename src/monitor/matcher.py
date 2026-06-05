from datetime import datetime

CITY_ALIASES = {
    "MUMBAI": "MUMBAI",
    "MUMBAI VAC": "MUMBAI",
    "CHENNAI": "CHENNAI",
    "CHENNAI VAC": "CHENNAI",
    "HYDERABAD": "HYDERABAD",
    "HYDERABAD VAC": "HYDERABAD",
    "DELHI": "DELHI",
    "DELHI VAC": "DELHI",
    "NEW DELHI": "DELHI",
    "NEW DELHI VAC": "DELHI",
    "KOLKATA": "KOLKATA",
    "KOLKATA VAC": "KOLKATA",
}

ALL_CONSULATES = ["CHENNAI", "HYDERABAD", "MUMBAI", "DELHI", "KOLKATA"]

DATE_FORMATS = [
    "%d %b %Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
]

def normalize_city(value: str) -> str:
    raw = str(value or "").strip().upper()
    if raw == "ANY":
        return "ANY"
    return CITY_ALIASES.get(raw, raw)

def parse_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        if hasattr(value, "to_pydatetime"):
            return value.to_pydatetime()
    except Exception:
        pass
    s = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default

def is_ofc_location(visa_location: str) -> bool:
    return "VAC" in str(visa_location or "").upper()

def build_buckets(rows):
    ofc_buckets = {}
    consular_buckets = {}

    for row in rows:
        loc_raw = row.get("visa_location", "")
        city = normalize_city(loc_raw)
        count = safe_int(row.get("slots"), 0)
        display_date = row.get("start_date", "")
        date_obj = parse_date(display_date)

        if not city or count < 1 or not date_obj:
            continue

        item = {
            "date": date_obj,
            "display_date": str(display_date),
            "count": count,
            "city": city,
        }

        if is_ofc_location(loc_raw):
            ofc_buckets.setdefault(city, []).append(item)
        else:
            consular_buckets.setdefault(city, []).append(item)

    for bucket in (ofc_buckets, consular_buckets):
        for city in bucket:
            bucket[city].sort(key=lambda x: x["date"])

    return ofc_buckets, consular_buckets

def eligible_rows(rows, need_after, need_before):
    return [
        row for row in rows
        if need_after <= row["date"] <= need_before
    ]

def find_valid_pair(ofc_buckets, consular_buckets, ofc_candidate_cities, consular_candidate_cities, ofc_need_after, ofc_need_before, consular_need_after, consular_need_before):
    best_pair = None
    best_ofc_matches = []
    best_consular_matches = []
    best_ofc_city = None
    best_consular_city = None

    for ofc_city in ofc_candidate_cities:
        ofc_rows = eligible_rows(ofc_buckets.get(ofc_city, []), ofc_need_after, ofc_need_before)
        if not ofc_rows:
            continue

        for consular_city in consular_candidate_cities:
            consular_rows = eligible_rows(consular_buckets.get(consular_city, []), consular_need_after, consular_need_before)
            if not consular_rows:
                continue

            for ofc in ofc_rows:
                for consular in consular_rows:
                    if ofc["date"] <= consular["date"]:
                        if best_pair is None:
                            best_pair = (ofc, consular)
                            best_ofc_matches = ofc_rows
                            best_consular_matches = consular_rows
                            best_ofc_city = ofc_city
                            best_consular_city = consular_city
                        else:
                            current_ofc, current_consular = best_pair
                            if (
                                consular["date"] < current_consular["date"]
                                or (
                                    consular["date"] == current_consular["date"]
                                    and ofc["date"] < current_ofc["date"]
                                )
                            ):
                                best_pair = (ofc, consular)
                                best_ofc_matches = ofc_rows
                                best_consular_matches = consular_rows
                                best_ofc_city = ofc_city
                                best_consular_city = consular_city

    return best_pair, best_ofc_matches, best_consular_matches, best_ofc_city, best_consular_city
