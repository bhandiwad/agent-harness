"""Server-owned source ledger survives delegated frames and context compaction."""


def record(state, name, result):
    if name != "datafusion_search":
        return
    ledger = state.setdefault("evidence", {"version": 1, "complete": False, "sources": []})
    sources = {(s["reference"], s["version"]): s for s in ledger["sources"]}
    rows = result.get("sources") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        ledger["complete"] = False
        return
    for item in rows:
        if not isinstance(item, dict) or any(
            not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > limit
            for key, limit in (("reference", 2048), ("version", 256))
        ):
            ledger["complete"] = False
            continue
        key = (item["reference"], item["version"])
        if len(sources) >= 100 and key not in sources:
            ledger["complete"] = False
            continue
        sources[key] = {"reference": key[0], "version": key[1]}
    ledger["sources"] = list(sources.values())
