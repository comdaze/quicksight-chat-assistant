"""Compile a structured aggregate query into one parameterized, read-only SELECT. Pure functions, no I/O.

The model never writes SQL. It sends {metrics, dimensions, time, filters, order_by, limit}; every identifier must be a
known dataset column (mapped to its physical expression), every aggregate and operator comes from a fixed list, and
every value is a bound parameter.
"""

import re

AGGS = {"sum": "SUM({})", "avg": "AVG({})", "min": "MIN({})", "max": "MAX({})", "count": "COUNT({})",
        "count_distinct": "COUNT(DISTINCT {})"}
GRAINS = ("hour", "day", "week", "month", "quarter", "year")
OPS = ("eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "between", "contains", "is_null", "not_null")
MAX_METRICS, MAX_DIMENSIONS, MAX_FILTERS, MAX_IN_VALUES, MAX_LIMIT = 10, 3, 12, 200, 500
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class QueryError(ValueError):
    pass


class Dialect:
    """Identifier quoting, time truncation and parameter style for one engine."""

    name = "base"

    def quote(self, identifier):
        raise NotImplementedError

    def trunc(self, expression, grain):
        raise NotImplementedError

    def param(self, name):
        raise NotImplementedError

    def contains(self, expression, placeholder):
        raise NotImplementedError

    def next_day(self, placeholder):
        raise NotImplementedError


class Postgres(Dialect):
    name = "postgresql"

    def quote(self, identifier):
        return '"' + identifier.replace('"', '""') + '"'

    def trunc(self, expression, grain):
        return f"date_trunc('{grain}', {expression})"

    def param(self, name):
        return f":{name}"  # pg8000.native named parameters

    def contains(self, expression, placeholder):
        return f"CAST({expression} AS TEXT) ILIKE {placeholder}"

    def next_day(self, placeholder):
        return f"(CAST({placeholder} AS DATE) + 1)"


class MySQL(Dialect):
    name = "mysql"

    def quote(self, identifier):
        return "`" + identifier.replace("`", "``") + "`"

    def trunc(self, expression, grain):
        # Literal % must be doubled because PyMySQL formats the statement with pyformat parameters.
        return {
            "hour": f"DATE_FORMAT({expression}, '%%Y-%%m-%%d %%H:00:00')",
            "day": f"DATE({expression})",
            "week": f"DATE_SUB(DATE({expression}), INTERVAL WEEKDAY({expression}) DAY)",
            "month": f"DATE_FORMAT({expression}, '%%Y-%%m-01')",
            "quarter": f"MAKEDATE(YEAR({expression}), 1) + INTERVAL (QUARTER({expression}) - 1) QUARTER",
            "year": f"DATE_FORMAT({expression}, '%%Y-01-01')",
        }[grain]

    def param(self, name):
        return f"%({name})s"

    def contains(self, expression, placeholder):
        return f"CAST({expression} AS CHAR) LIKE {placeholder}"

    def next_day(self, placeholder):
        return f"(DATE({placeholder}) + INTERVAL 1 DAY)"


DIALECTS = {"postgresql": Postgres(), "mysql": MySQL()}


def _escape_like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


INDEXED = re.compile(r"^values\s*\[\s*(\d)\s*\]$")


def _list_value(flt):
    """The list operand of in / not_in / between. Some models garble the key name ("values[]", "values 范围内",
    "values[0]" + "values[1]"); accept those rather than burning a model round trip on a retry."""
    if flt.get("values") is not None:
        return flt["values"]
    garbled = {k.strip(): v for k, v in flt.items() if isinstance(k, str) and k.strip().startswith("values")}
    lists = [v for v in garbled.values() if isinstance(v, list)]
    if lists:
        return lists[0]
    indexed = sorted((int(m.group(1)), v) for k, v in garbled.items() if (m := INDEXED.match(k)))
    if indexed and [i for i, _ in indexed] == list(range(len(indexed))):
        return [v for _, v in indexed]
    return flt.get("value")


def compile_query(spec, columns, source, dialect):
    """Return (sql, params, output_columns).

    columns: {output column name: {"expr": physical column name, "type": QuickSight type}}
    source:  {"table": (schema, name)} or {"sql": custom SQL}
    """
    if not isinstance(spec, dict):
        raise QueryError("查询参数必须是对象")
    params, counter = {}, [0]

    def bind(value):
        name = f"p{counter[0]}"
        counter[0] += 1
        params[name] = value
        return dialect.param(name)

    def column(name, purpose):
        info = columns.get(name) if isinstance(name, str) else None
        if info is None:
            raise QueryError(f"{purpose}引用了不存在或不可用的列：{name}")
        return dialect.quote(info["expr"]), info

    metrics = spec.get("metrics") or []
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= MAX_METRICS:
        raise QueryError(f"metrics 需要 1 到 {MAX_METRICS} 个")
    dimensions = spec.get("dimensions") or []
    if not isinstance(dimensions, list) or len(dimensions) > MAX_DIMENSIONS:
        raise QueryError(f"dimensions 最多 {MAX_DIMENSIONS} 个")
    filters = spec.get("filters") or []
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        raise QueryError(f"filters 最多 {MAX_FILTERS} 个")
    try:
        limit = max(1, min(MAX_LIMIT, int(spec.get("limit") or 200)))
    except (TypeError, ValueError) as error:
        raise QueryError("limit 必须是整数") from error

    select, group, out = [], [], []
    time = spec.get("time")
    if time:
        if not isinstance(time, dict) or time.get("grain") not in GRAINS:
            raise QueryError(f"time.grain 只能是 {', '.join(GRAINS)}")
        expression, info = column(time.get("column"), "time ")
        if info["type"] not in ("DATETIME", "DATE"):
            raise QueryError(f"{time['column']} 不是日期时间列")
        select.append(f"{dialect.trunc(expression, time['grain'])} AS {dialect.quote('__time')}")
        group.append("1")
        out.append({"name": time["column"], "role": "time", "grain": time["grain"]})
    for name in dimensions:
        expression, _info = column(name, "dimensions ")
        select.append(f"{expression} AS {dialect.quote('__d' + str(len(out)))}")
        group.append(str(len(select)))
        out.append({"name": name, "role": "dimension"})
    used = {}
    for metric in metrics:
        if not isinstance(metric, dict) or metric.get("agg") not in AGGS:
            raise QueryError(f"metrics[].agg 只能是 {', '.join(AGGS)}")
        agg, name = metric["agg"], metric.get("column")
        if agg == "count" and name in (None, "", "*"):
            expression, label = "*", "count"
        else:
            expression, info = column(name, "metrics ")
            if agg in ("sum", "avg") and info["type"] not in ("INTEGER", "DECIMAL"):
                raise QueryError(f"{name} 不是数值列，不能做 {agg}")
            label = name if name not in used else f"{agg}_{name}"
        used[name] = True
        if label in [o["name"] for o in out]:
            label = f"{agg}_{label}"
        select.append(f"{AGGS[agg].format(expression)} AS {dialect.quote('__m' + str(len(out)))}")
        out.append({"name": label, "role": "metric", "column": name, "agg": agg})

    where = []
    for flt in filters:
        if not isinstance(flt, dict) or flt.get("op") not in OPS:
            raise QueryError(f"filters[].op 只能是 {', '.join(OPS)}")
        expression, info = column(flt.get("column"), "filters ")
        op, value = flt["op"], flt.get("value")
        if op in ("in", "not_in", "between"):
            value = _list_value(flt)  # schema: "values" is the list form
        elif isinstance(value, list) and len(value) == 1:
            value = value[0]
        is_time = info["type"] in ("DATETIME", "DATE")
        if op in ("is_null", "not_null"):
            where.append(f"{expression} IS {'NOT ' if op == 'not_null' else ''}NULL")
            continue
        if op in ("in", "not_in"):
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES:
                raise QueryError(f'{op} 需要 "values": [...]（键名正好是 values，1 到 {MAX_IN_VALUES} 个值）')
            placeholders = ", ".join(bind(v) for v in value)
            where.append(f"{expression} {'NOT ' if op == 'not_in' else ''}IN ({placeholders})")
            continue
        if op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise QueryError('between 需要 "values": ["最小值", "最大值"]（键名正好是 values，两个元素）；'
                                 '也可以改用一个 gte 加一个 lte 条件')
            low, high = value
            if is_time and isinstance(high, str) and DATE_ONLY.match(high):
                where.append(f"{expression} >= {bind(low)} AND {expression} < {dialect.next_day(bind(high))}")
            else:
                where.append(f"{expression} BETWEEN {bind(low)} AND {bind(high)}")
            continue
        if isinstance(value, (list, dict)) or value is None:
            raise QueryError(f"{op} 需要单个值")
        if op == "contains":
            where.append(dialect.contains(expression, bind("%" + _escape_like(str(value)) + "%")))
            continue
        if is_time and op == "lte" and isinstance(value, str) and DATE_ONLY.match(value):
            # A date-only upper bound on a timestamp includes that whole day, as QuickSight's date filters do.
            where.append(f"{expression} < {dialect.next_day(bind(value))}")
            continue
        symbol = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
        where.append(f"{expression} {symbol} {bind(value)}")

    if "table" in source:
        schema, name = source["table"]
        relation = (dialect.quote(schema) + "." if schema else "") + dialect.quote(name)
    else:
        relation = f"({source['sql']}) AS {dialect.quote('__src')}"

    order = []
    for item in spec.get("order_by") or []:
        if not isinstance(item, dict):
            raise QueryError("order_by 每项需要 {name, desc}")
        names = [o["name"] for o in out]
        if item.get("name") not in names:
            raise QueryError(f"order_by 只能用输出列：{', '.join(names)}")
        order.append(f"{names.index(item['name']) + 1} {'DESC' if item.get('desc') else 'ASC'}")
    if not order:
        order = [str(i + 1) for i, o in enumerate(out) if o["role"] != "metric"] or ["1"]

    sql = f"SELECT {', '.join(select)} FROM {relation}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        sql += " GROUP BY " + ", ".join(group)
    sql += " ORDER BY " + ", ".join(order) + f" LIMIT {limit + 1}"  # one extra row tells whether it was truncated
    return sql, params, out, limit
