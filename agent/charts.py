"""Build the chart specs the chat page renders with ECharts, only from rows that tools returned.

Spec: {"type": "line"|"bar"|"none", "title", "x": [label], "series": [{"name", "data": [number|None]}], "notes": [str],
       "pending_change": {...} (previews only)}
"""

MAX_SERIES = 10
MAX_POINTS = 500
ROW_KEYS = ("rows", "data", "items", "results", "records", "result")


def find_rows(value):
    """The first list of objects in a tool result (MCP tools differ in where they put rows)."""
    if isinstance(value, list) and value and all(isinstance(r, dict) for r in value[:5]):
        return value
    if isinstance(value, dict):
        for key in ROW_KEYS:
            rows = find_rows(value.get(key))
            if rows:
                return rows
        for item in value.values():
            if isinstance(item, (list, dict)):
                rows = find_rows(item)
                if rows:
                    return rows
    return None


def label(value):
    text = "" if value is None else str(value)
    if len(text) >= 19 and text[10] == "T":  # ISO timestamps from date buckets: drop a zero time part
        return text[:10] if text[11:19] == "00:00:00" else text[:16].replace("T", " ")
    return text


def number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    try:
        return round(float(str(value).replace(",", "")), 4)
    except (TypeError, ValueError):
        return None


def from_rows(rows, x, ys, series_by=None):
    """Return (labels, series, notes). Raises ValueError for unknown fields."""
    if not rows:
        raise ValueError("结果没有数据行")
    fields = set().union(*(r.keys() for r in rows[:50]))
    for name in [x, *ys] + ([series_by] if series_by else []):
        if name not in fields:
            raise ValueError(f"结果里没有字段 {name}；可用字段：{', '.join(sorted(fields))}")
    notes = []
    labels = list(dict.fromkeys(label(r.get(x)) for r in rows))
    if len(labels) > MAX_POINTS:
        notes.append(f"只画了前 {MAX_POINTS} 个分类")
        labels = labels[:MAX_POINTS]
    index = {name: i for i, name in enumerate(labels)}
    series = []
    if series_by:
        if len(ys) > 1:
            notes.append(f"按 {series_by} 分组时只画第一个数值字段 {ys[0]}")
        groups = list(dict.fromkeys(label(r.get(series_by)) for r in rows))
        if len(groups) > MAX_SERIES:
            notes.append(f"{series_by} 有 {len(groups)} 个值，只画前 {MAX_SERIES} 个")
            groups = groups[:MAX_SERIES]
        for group in groups:
            data = [None] * len(labels)
            for r in rows:
                position = index.get(label(r.get(x)))
                if position is not None and label(r.get(series_by)) == group:
                    data[position] = number(r.get(ys[0]))
            series.append({"name": group, "data": data})
    else:
        for y in ys[:MAX_SERIES]:
            data = [None] * len(labels)
            for r in rows:
                position = index.get(label(r.get(x)))
                if position is not None:
                    data[position] = number(r.get(y))
            series.append({"name": y, "data": data})
    return labels, series, notes


def chart(kind, title, labels, series, notes=None, **extra):
    return {"type": kind if kind in ("line", "bar", "none") else "line", "title": str(title or "")[:80],
            "x": labels, "series": series, "notes": notes or [], **extra}
