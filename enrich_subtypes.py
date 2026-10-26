import argparse
import glob
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
try:
    from curl_cffi import requests
except ImportError:
    requests = None

_thread_local = threading.local()


def get_thread_session():
    if requests is None:
        raise RuntimeError("curl_cffi is required for HTTP enrichment")
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session(impersonate="chrome")
    return _thread_local.session

ROYAL_SUITE_CLASSES = {"Icon", "Oasis", "Quantum"}

STAR_CODES = {"UL", "RL", "IL", "SL", "TL", "A1", "A2", "UP", "U1"}
SKY_CODES = {"JY", "VP", "SS", "IG", "GS", "SN", "OW", "OS", "CL", "A3", "A4", "GT", "1S", "2S"}
SEA_CODES = {"SF", "JS", "JT", "J1", "J3", "J4"}

SUITE_TIER_AMENITIES = {
    "Sea": "Coastal Kitchen (Dinner), Luxury Pillowtop Mattress, Lavazza Espresso Machine, Priority Boarding",
    "Sky": "All-Day Coastal Kitchen, Suite Lounge & Sun Deck, Complimentary VOOM Wi-Fi, Concierge Service, Priority Boarding",
    "Star": "Royal Genie Service, Complimentary Specialty Dining, Complimentary Deluxe Beverage Package, Gratuities Included, VOOM Wi-Fi",
    "Standard": "Priority Boarding, Dedicated Stateroom Attendant, Luxury Robes & Toiletries, Double Crown & Anchor Points",
    "Guarantee": "We Choose Your Suite (Assigned close to sailing • Minimum Sea Class / Junior Suite • Does NOT include Sky/Star Class perks)",
}


def find_stateroom_types(obj):
    if isinstance(obj, dict):
        if "stateroomTypes" in obj and isinstance(obj["stateroomTypes"], list):
            return obj["stateroomTypes"]
        for v in obj.values():
            res = find_stateroom_types(v)
            if res is not None:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = find_stateroom_types(item)
            if res is not None:
                return res
    return None


def classify_subtype_tier(sub: dict, ship_cls: str) -> str:
    code = str(sub.get("code") or "").strip().upper()
    name = str(sub.get("name") or "").strip()
    is_guar = bool(sub.get("guarantee")) or code in ("WS", "W", "WD")
    if is_guar:
        return "Guarantee"

    if ship_cls not in ROYAL_SUITE_CLASSES:
        return "Standard"

    text_blobs = list(sub.get("description") or [])
    for feat in sub.get("features") or []:
        if isinstance(feat, dict):
            text_blobs.append(str(feat.get("title") or ""))
            text_blobs.extend(feat.get("lines") or [])
    combined = " ".join(str(x) for x in text_blobs).lower()

    if "star class" in combined or code in STAR_CODES:
        return "Star"
    if "sky class" in combined or "sky junior" in name.lower():
        return "Sky"
    if "sea class" in combined:
        return "Sea"
    if code in SKY_CODES:
        return "Sky"
    if code in SEA_CODES:
        return "Sea"
    return "Sea"


def parse_rsc_deluxe_subtypes(rsc_text: str, ship_cls: str, guests: int) -> list[dict]:
    rooms = []
    for line in rsc_text.splitlines():
        if '"stateroomTypes":[' not in line:
            continue
        try:
            _, payload = line.split(":", 1)
            data = json.loads(payload)
        except Exception:
            continue

        st_types = find_stateroom_types(data)
        if not st_types:
            continue

        for st in st_types:
            if not isinstance(st, dict) or st.get("code") != "DELUXE":
                continue
            for sub in st.get("stateroomSubtypes") or []:
                if not isinstance(sub, dict):
                    continue
                pricing = sub.get("pricing") or {}
                inv = pricing.get("invoice") or {}
                total = inv.get("total")
                if total is None:
                    continue
                total_f = round(float(total), 2)
                subtotal_f = round(float(inv.get("subtotal") or 0.0), 2)
                taxes_f = round(float(inv.get("taxesAndFees") or 0.0), 2)
                disc_obj = inv.get("discount") or {}
                disc_f = round(abs(float(disc_obj.get("amount") or 0.0)), 2)
                pp_f = round(float(pricing.get("perPersonAmount") or (total_f / max(1, guests))), 2)
                base_pp_f = round(subtotal_f / max(1, guests), 2)

                code = str(sub.get("code") or "SUITE").strip().upper()
                raw_name = str(sub.get("name") or "Suite").strip()
                tier = classify_subtype_tier(sub, ship_cls)
                if tier == "Guarantee":
                    display_name = f"We Choose Your Suite - Guarantee ({code})"
                else:
                    display_name = f"{raw_name} ({code})"

                rooms.append({
                    "code": code,
                    "name": raw_name,
                    "display_name": display_name,
                    "guarantee": tier == "Guarantee",
                    "tier": tier,
                    "pricing": {
                        "guests": guests,
                        "base_pp": base_pp_f,
                        "total_pp": pp_f,
                        "cabin_base": subtotal_f,
                        "cabin_taxes": taxes_f,
                        "cabin_total": total_f,
                        "cabin_discount": disc_f,
                    },
                })
        break
    return rooms


def fetch_sailing_subtypes(idx_sailing):
    idx, s = idx_sailing
    session = get_thread_session()
    q = s["booking_link"].replace("/booking/landing?", "")
    ship_cls = s.get("ship_class", "Other")
    has_quad = s.get("pricing_quad") is not None

    rooms_4 = []
    rooms_2 = []
    got_403 = False

    if has_quad:
        url_4 = f"https://www.royalcaribbean.com/room-selection/type-and-subtype?{q}&r0a=2&r0c=2&r0d=DELUXE"
        try:
            r4 = session.get(url_4, headers={"RSC": "1"}, timeout=18)
            if r4.status_code == 200:
                rooms_4 = parse_rsc_deluxe_subtypes(r4.text, ship_cls, 4)
            elif r4.status_code == 403:
                got_403 = True
        except Exception:
            pass

    # Always fetch the real 2-adult price (unless already 403-blocked)
    fetched_2a = True
    if not got_403:
        url_2 = f"https://www.royalcaribbean.com/room-selection/type-and-subtype?{q}&r0a=2&r0c=0&r0d=DELUXE"
        try:
            r2 = session.get(url_2, headers={"RSC": "1"}, timeout=18)
            if r2.status_code == 200:
                rooms_2 = parse_rsc_deluxe_subtypes(r2.text, ship_cls, 2)
            elif r2.status_code == 403:
                got_403 = True
        except Exception:
            pass

    return idx, rooms_4, rooms_2, fetched_2a, got_403


CLASS_PRIORITY = {
    "icon": 0,
    "oasis": 1,
    "quantum": 2,
    "freedom": 3,
    "voyager": 4,
    "radiance": 5,
    "vision": 6,
}


def rebuild_itineraries_and_save(data: dict, json_path: str):
    sailings = data["sailings"]
    itin_map = {}
    for d in sailings:
        code = d["itinerary_code"]
        if code not in itin_map:
            itin_map[code] = {
                "itinerary_code": code,
                "itinerary_name": d["itinerary_name"],
                "ship": d["ship"],
                "ship_code": d.get("ship_code", ""),
                "ship_class": d["ship_class"],
                "nights": d["nights"],
                "port": d["port"],
                "port_code": d.get("port_code", ""),
                "suite_tier": d.get("suite_tier", "Standard"),
                "available_tiers": d.get("available_tiers", []),
                "sailing_count": 0,
                "promo_count": 0,
                "lowest_cabin_total": d["cabin_total"],
                "lowest_total_pp": d["total_pp"],
                "lowest_sail_date": d["date"],
                "sail_dates": [],
            }
        it = itin_map[code]
        it["sailing_count"] += 1
        if d.get("is_ksf_promo"):
            it["promo_count"] += 1
        it["sail_dates"].append(d["date"])
        if d["cabin_total"] < it["lowest_cabin_total"]:
            it["lowest_cabin_total"] = d["cabin_total"]
            it["lowest_total_pp"] = d["total_pp"]
            it["lowest_sail_date"] = d["date"]
            it["suite_tier"] = d.get("suite_tier", "Standard")
            it["available_tiers"] = d.get("available_tiers", [])

    for it in itin_map.values():
        it["sail_dates"].sort()

    data["itineraries"] = sorted(itin_map.values(), key=lambda x: (x["lowest_cabin_total"], x["ship"]))
    if "summary" in data and isinstance(data["summary"], dict):
        data["summary"]["itineraries_with_suites"] = len(data["itineraries"])
        data["summary"]["total_suite_sailings"] = len(sailings)
        data["summary"]["promo_sailings"] = sum(1 for d in sailings if d.get("is_ksf_promo"))
        data["summary"]["holiday_sailings"] = sum(1 for d in sailings if d.get("is_holiday_rate"))

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def merge_shard_files(json_path: str, shard_glob: str):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sailings = data["sailings"]
    shard_files = sorted(glob.glob(shard_glob))
    merged_count = 0
    for sf in shard_files:
        with open(sf, "r", encoding="utf-8") as f:
            shard_map = json.load(f)
        for idx_str, updated_sailing in shard_map.items():
            idx = int(idx_str)
            if 0 <= idx < len(sailings):
                sailings[idx] = updated_sailing
                merged_count += 1

    rebuild_itineraries_and_save(data, json_path)
    total_live = sum(
        1 for s in sailings if any(v.get("is_live_exact") for v in (s.get("tiers") or {}).values())
    )
    print(f"Merged {merged_count} enriched sailings from {len(shard_files)} shards into {json_path} (Total live: {total_live}/{len(sailings)}).")


def enrich_suites_json(
    json_path: str = "data/suites.json",
    ship_classes: list[str] | None = None,
    workers: int = 6,
    force: bool = False,
    max_sailings: int = 550,
    shard_index: int | None = None,
    shard_count: int | None = None,
    output_shard: str | None = None,
):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sailings = data["sailings"]
    cls_lower = {c.lower() for c in ship_classes} if ship_classes else None

    if shard_index is not None and shard_count is not None and shard_count > 0:
        targets = [
            (i, s)
            for i, s in enumerate(sailings)
            if (i % shard_count == shard_index)
            and (not cls_lower or (s.get("ship_class") or "").lower() in cls_lower)
            and (force or not any(v.get("is_live_exact") for v in (s.get("tiers") or {}).values()))
        ]
    else:
        candidates = []
        for i, s in enumerate(sailings):
            s_cls = (s.get("ship_class") or "").lower()
            if cls_lower and s_cls not in cls_lower:
                continue
            has_live = any(v.get("is_live_exact") for v in (s.get("tiers") or {}).values())
            if not force and has_live:
                continue
            enriched_at = s.get("enriched_at") or ""
            cls_prio = CLASS_PRIORITY.get(s_cls, 9)
            candidates.append((0 if not has_live else 1, cls_prio, enriched_at, i, s))

        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        targets = [(item[3], item[4]) for item in candidates[:max_sailings]]

    print(
        f"Enriching {len(targets)} sailings from {json_path} "
        f"(shard={shard_index}/{shard_count}, workers={workers})..."
    )
    t0 = time.time()
    updated = 0
    consecutive_403 = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    shard_results = {}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fetch_sailing_subtypes, item) for item in targets]
        for count, fut in enumerate(as_completed(futures), 1):
            idx, rooms_4, rooms_2, fetched_2a, got_403 = fut.result()
            if got_403 and not rooms_4 and not rooms_2:
                consecutive_403 += 1
                if consecutive_403 >= 15:
                    print("  Notice: Akamai rate limit reached (403); stopping batch early and saving progress.", flush=True)
                    for pending in futures:
                        pending.cancel()
                    break
            else:
                consecutive_403 = 0
            s = sailings[idx]
            if not rooms_4 and not rooms_2:
                continue

            map_2 = {r["code"]: r["pricing"] for r in rooms_2}
            map_4 = {r["code"]: r["pricing"] for r in rooms_4}

            all_rooms_by_code = {}
            for r in rooms_4:
                all_rooms_by_code[r["code"]] = r
            for r in rooms_2:
                if r["code"] not in all_rooms_by_code:
                    all_rooms_by_code[r["code"]] = r

            tiers_dict = {}
            tier_order = ["Sea", "Sky", "Star", "Standard", "Guarantee"]
            grouped = {t: [] for t in tier_order}

            for code, r_info in all_rooms_by_code.items():
                t = r_info["tier"]
                p4 = map_4.get(code)
                p2 = map_2.get(code)

                # Fallback only: the 2-adult request failed or omitted this room code
                if p4 and not p2:
                    tax_2 = round(p4["cabin_taxes"] / 2.0, 2)
                    extra_pp = float(s.get("extra_guest_fare_pp") or 0.0)
                    base_2 = round(max(0.0, p4["cabin_base"] - 2.0 * extra_pp), 2)
                    tot_2 = round(base_2 + tax_2, 2)
                    p2 = {
                        "guests": 2,
                        "base_pp": round(base_2 / 2.0, 2),
                        "total_pp": round(tot_2 / 2.0, 2),
                        "cabin_base": base_2,
                        "cabin_taxes": tax_2,
                        "cabin_total": tot_2,
                        "cabin_discount": p4["cabin_discount"],
                    }

                grouped.setdefault(t, []).append({
                    "code": code,
                    "display_name": r_info["display_name"],
                    "pricing_2a": p2,
                    "pricing_quad": p4,
                    "sort_price": (p4 or p2)["cabin_total"],
                })

            for t in tier_order:
                t_rooms = grouped.get(t) or []
                if not t_rooms:
                    continue

                rooms_2a = sorted(
                    [r for r in t_rooms if r["pricing_2a"] is not None],
                    key=lambda x: x["pricing_2a"]["cabin_total"],
                )
                rooms_quad = sorted(
                    [r for r in t_rooms if r["pricing_quad"] is not None],
                    key=lambda x: x["pricing_quad"]["cabin_total"],
                )

                best_2a = rooms_2a[0] if rooms_2a else None
                best_quad = rooms_quad[0] if rooms_quad else None
                lead = best_quad or best_2a
                if not lead:
                    continue

                others_quad = [
                    f"{o['display_name']}: ${int(round(o['pricing_quad']['cabin_total'])):,}"
                    for o in rooms_quad[1:6]
                ]
                others_2a = [
                    f"{o['display_name']}: ${int(round(o['pricing_2a']['cabin_total'])):,}"
                    for o in rooms_2a[1:6]
                ]
                other_str_quad = " • ".join(others_quad)
                other_str_2a = " • ".join(others_2a)

                tiers_dict[t] = {
                    "tier": t,
                    "label": "We Choose Your Suite (Guarantee)" if t == "Guarantee" else f"{t} Class",
                    "room_name": (best_quad or best_2a)["display_name"],
                    "room_name_2a": best_2a["display_name"] if best_2a else (best_quad["display_name"] if best_quad else ""),
                    "room_name_quad": best_quad["display_name"] if best_quad else (best_2a["display_name"] if best_2a else ""),
                    "other_rooms": other_str_quad or other_str_2a,
                    "other_rooms_2a": other_str_2a,
                    "other_rooms_quad": other_str_quad,
                    "amenities": SUITE_TIER_AMENITIES.get(t, ""),
                    "pricing_2a": best_2a["pricing_2a"] if best_2a else None,
                    "pricing_quad": best_quad["pricing_quad"] if best_quad else None,
                    "is_live_exact": True,
                }

            if tiers_dict:
                avail_tiers = [t for t in tier_order if t in tiers_dict]
                non_guar = [t for t in avail_tiers if t != "Guarantee"]
                lead_tier = non_guar[0] if non_guar else avail_tiers[0]
                lead_obj = tiers_dict[lead_tier]

                s["suite_tier"] = lead_tier
                s["available_tiers"] = avail_tiers
                s["tiers"] = tiers_dict
                s["pricing_2a"] = lead_obj["pricing_2a"]
                s["pricing_quad"] = lead_obj["pricing_quad"]
                s["enriched_at"] = now_iso

                ref_main = lead_obj["pricing_quad"] or lead_obj["pricing_2a"]
                s["base_pp"] = ref_main["base_pp"]
                s["total_pp"] = ref_main["total_pp"]
                s["cabin_base"] = ref_main["cabin_base"]
                s["cabin_taxes"] = ref_main["cabin_taxes"]
                s["cabin_total"] = ref_main["cabin_total"]
                s["cabin_discount"] = ref_main["cabin_discount"]
                s["price_per_night"] = round(ref_main["cabin_total"] / max(1, s.get("nights", 1)), 2)

                if lead_obj["pricing_2a"] and lead_obj["pricing_quad"]:
                    diff = lead_obj["pricing_quad"]["cabin_base"] - lead_obj["pricing_2a"]["cabin_base"]
                    extra_pp = round(max(0.0, diff / 2.0), 2)
                    s["extra_guest_fare_pp"] = extra_pp
                    s["is_ksf_promo"] = extra_pp <= 1.0
                    s["is_holiday_rate"] = extra_pp > 1.0
                    disc = int(round(ref_main["cabin_discount"]))
                    if s["is_ksf_promo"]:
                        s["rate_tag"] = f"PROMO: Kids/3rd&4th Free (-${disc} off)"
                    else:
                        s["rate_tag"] = f"HOLIDAY/BLACKOUT: 3rd/4th +${extra_pp:.2f}/ea (-${disc} off)"

                shard_results[str(idx)] = s
                updated += 1

            if count % 25 == 0 or count == len(targets):
                print(f"  Progress: {count}/{len(targets)} ({updated} enriched) in {time.time()-t0:.1f}s", flush=True)

    if output_shard:
        out_dir = os.path.dirname(output_shard)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_shard, "w", encoding="utf-8") as f:
            json.dump(shard_results, f)
        print(f"Saved shard {shard_index}/{shard_count} ({updated}/{len(targets)} sailings) to {output_shard} in {time.time()-t0:.1f}s.")
        return

    rebuild_itineraries_and_save(data, json_path)
    print(f"Done! Enriched {updated}/{len(targets)} sailings in {time.time()-t0:.1f}s.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enrich suites.json with live per-subtype room-selection prices from Royal Caribbean")
    parser.add_argument("--json", default="data/suites.json", help="Path to suites.json")
    parser.add_argument("--ship-class", default="all", help="Comma-separated ship classes to enrich (e.g. Icon,Oasis) or 'all' (default: all)")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent workers (default: 6)")
    parser.add_argument("--max-sailings", type=int, default=550, help="Max sailings to enrich per single-node run (default: 550)")
    parser.add_argument("--shard-index", type=int, default=None, help="0-based shard index for parallel CI jobs")
    parser.add_argument("--shard-count", type=int, default=None, help="Total number of parallel shards")
    parser.add_argument("--output-shard", type=str, default=None, help="Output file path for shard JSON")
    parser.add_argument("--merge-shards", type=str, default=None, help="Glob pattern of shard JSON files to merge into --json")
    parser.add_argument("--force", action="store_true", help="Re-enrich sailings even if already marked is_live_exact")
    args = parser.parse_args()
    if args.merge_shards:
        merge_shard_files(args.json, args.merge_shards)
    else:
        classes = None if args.ship_class.lower() == "all" else [c.strip() for c in args.ship_class.split(",") if c.strip()]
        enrich_suites_json(
            args.json,
            classes,
            args.workers,
            force=args.force,
            max_sailings=args.max_sailings,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            output_shard=args.output_shard,
        )

