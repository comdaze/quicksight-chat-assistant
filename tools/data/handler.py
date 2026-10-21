"""Gateway Lambda target: read-only, aggregate queries on the datasets of the analysis the chat is embedded in.

Tools (the Runtime adds `_caller` and `_analysis` to every call):
- describe_dataset(dataset_id)       columns, types, whether direct querying is supported and why not
- query_dataset(dataset_id, ...)     structured aggregate query compiled to one parameterized SELECT
- distinct_values(dataset_id, column, search?)   up to 50 values, e.g. to check a control value exists

Access rules: the caller must be allowed to view the analysis (or a dashboard published from it), the dataset must
be one the analysis uses, and the dataset must pass dataset.plan() (no RLS, no dataset filters, no joins).
Database credentials come from Secrets Manager: `<DATASOURCE_SECRET_PREFIX><DataSourceId>` holding
{"username", "password"}, or the secret the QuickSight data source itself uses.
"""

import datetime as dt
import decimal
import json
import os
import ssl
import time

import boto3

import caller
import dataset as ds
import sqlcompile
from caller import ToolError, log

SECRET_PREFIX = os.environ.get("DATASOURCE_SECRET_PREFIX", "quicksight-chat-assistant/datasource/")
STATEMENT_TIMEOUT_MS = int(os.environ.get("STATEMENT_TIMEOUT_MS", "20000"))
CA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rds-ca.pem")
qs = caller.qs
_connections = {}


def _dataset(args):
    """Check access and return (dataset description, plan) for args['dataset_id']."""
    email, aid = caller.require_viewer(args)
    dataset_id = str(args.get("dataset_id") or "")
    definition = caller.cached(f"def:{aid}", lambda: qs.describe_analysis_definition(
        AwsAccountId=caller.ACCOUNT, AnalysisId=aid)["Definition"])
    used = {d["DataSetArn"].rsplit("/", 1)[-1] for d in definition.get("DataSetIdentifierDeclarations", [])}
    if dataset_id not in used:
        raise ToolError(f"数据集 {dataset_id} 不属于当前分析；可用的数据集：{', '.join(sorted(used))}")
    description = caller.cached(f"ds:{dataset_id}", lambda: qs.describe_data_set(
        AwsAccountId=caller.ACCOUNT, DataSetId=dataset_id)["DataSet"])
    try:
        return description, ds.plan(description, caller.principals(email))
    except ds.Unsupported as error:
        raise ToolError(f"内置查询不支持这个数据集：{error}。如已接入其他数据工具（如 MCP），请改用它们。") from error


def _secret(data_source):
    names = [SECRET_PREFIX + data_source["DataSourceId"]]
    if data_source.get("SecretArn"):
        names.append(data_source["SecretArn"])
    for name in names:
        region = name.split(":")[3] if name.startswith("arn:") else None
        try:
            raw = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=name)["SecretString"]
            secret = json.loads(raw)
            return secret["username"], secret["password"]
        except Exception as error:  # noqa: BLE001 - try the next source, report below
            last = error
    raise ToolError(f"找不到数据源 {data_source['DataSourceId']} 的数据库凭证。请管理员在 Secrets Manager 创建 "
                    f"{names[0]}（内容 {{\"username\":..., \"password\":...}}）。({type(last).__name__})")


def _connect(data_source_arn):
    data_source_id = data_source_arn.rsplit("/", 1)[-1]
    cached = _connections.get(data_source_id)
    if cached:
        try:
            cached["ping"]()
            return cached
        except Exception:  # noqa: BLE001 - stale connection, reconnect
            _connections.pop(data_source_id, None)
    source = qs.describe_data_source(AwsAccountId=caller.ACCOUNT, DataSourceId=data_source_id)["DataSource"]
    try:
        info = ds.connection(source)
    except ds.Unsupported as error:
        raise ToolError(f"内置查询不支持这个数据源：{error}") from error
    if "rds_instance" in info:
        instance = boto3.client("rds").describe_db_instances(DBInstanceIdentifier=info["rds_instance"])["DBInstances"][0]
        info["host"], info["port"] = instance["Endpoint"]["Address"], instance["Endpoint"]["Port"]
        info["engine"] = "postgresql" if "postgres" in instance["Engine"] else "mysql"
    user, password = _secret(source)
    context = None
    if info["ssl"]:
        context = ssl.create_default_context()
        if os.path.exists(CA_FILE):
            context.load_verify_locations(CA_FILE)
    if info["engine"] == "postgresql":
        import pg8000.native
        conn = pg8000.native.Connection(user=user, password=password, host=info["host"], port=info["port"],
                                        database=info["database"], ssl_context=context, timeout=30,
                                        application_name="quicksight-chat-assistant")
        conn.run("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        conn.run(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")

        def run(sql, params):
            rows = conn.run(sql, **params)
            return [c["name"] for c in conn.columns], rows
        entry = {"engine": "postgresql", "run": run, "ping": lambda: conn.run("SELECT 1"), "close": conn.close}
    else:
        import pymysql
        conn = pymysql.connect(host=info["host"], port=info["port"], user=user, password=password,
                               database=info["database"], ssl=context, connect_timeout=15, read_timeout=60,
                               autocommit=False, charset="utf8mb4")
        with conn.cursor() as cursor:
            cursor.execute(f"SET SESSION max_execution_time = {STATEMENT_TIMEOUT_MS}")
            cursor.execute("SET SESSION TRANSACTION READ ONLY")

        def run(sql, params):
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                names = [d[0] for d in cursor.description]
            conn.rollback()
            return names, rows
        entry = {"engine": "mysql", "run": run, "ping": conn.ping, "close": conn.close}
    _connections[data_source_id] = entry
    return entry


def _plain(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def describe_dataset(args):
    description, plan = _dataset(args)
    return {"dataset_id": description["DataSetId"], "name": description.get("Name"),
            "import_mode": description.get("ImportMode"),
            "columns": [{"name": n, "type": c["type"]} for n, c in sorted(plan["columns"].items())],
            "hidden_columns": plan["hidden"], "notes": plan["notes"],
            "note": "SPICE 数据集查询的是数据源里的当前数据，可能比 SPICE 上次刷新更新。" if description.get("ImportMode") == "SPICE" else ""}


def _execute(plan, spec):
    conn = _connect(plan["data_source_arn"])
    dialect = sqlcompile.DIALECTS[conn["engine"]]
    try:
        sql, params, out, limit = sqlcompile.compile_query(spec, plan["columns"], plan["source"], dialect)
    except sqlcompile.QueryError as error:
        raise ToolError(str(error)) from error
    started = time.time()
    try:
        _names, rows = conn["run"](sql, params)
    except Exception:
        _connections.pop(plan["data_source_arn"].rsplit("/", 1)[-1], None)
        raise
    truncated = len(rows) > limit
    rows = [{o["name"]: _plain(v) for o, v in zip(out, row)} for row in rows[:limit]]
    return {"columns": out, "rows": rows, "row_count": len(rows), "truncated": truncated,
            "elapsed_ms": int((time.time() - started) * 1000)}


def query_dataset(args):
    description, plan = _dataset(args)
    spec = {k: args.get(k) for k in ("metrics", "dimensions", "time", "filters", "order_by", "limit")}
    result = _execute(plan, spec)
    log(event="query", dataset=description["DataSetId"], rows=result["row_count"], ms=result["elapsed_ms"])
    if description.get("ImportMode") == "SPICE":
        result["note"] = "数据直接取自数据源，可能比仪表盘的 SPICE 数据更新"
    return result


def distinct_values(args):
    _description, plan = _dataset(args)
    column = args.get("column")
    filters = [{"column": column, "op": "contains", "value": args["search"]}] if args.get("search") else []
    result = _execute(plan, {"metrics": [{"agg": "count"}], "dimensions": [column], "filters": filters,
                             "order_by": [{"name": "count", "desc": True}], "limit": 50})
    return {"column": column, "values": [r[column] for r in result["rows"]], "truncated": result["truncated"]}


TOOLS = {"describe_dataset": describe_dataset, "query_dataset": query_dataset, "distinct_values": distinct_values}


def handler(event, context):
    event = dict(event or {})
    name = event.pop("__tool", "")
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    if custom.get("bedrockAgentCoreToolName"):
        name = custom["bedrockAgentCoreToolName"].split("___", 1)[-1]
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        # QuickSight definitions contain datetimes (e.g. date parameter defaults); Lambda only returns plain JSON.
        return json.loads(json.dumps(tool(event), ensure_ascii=False, default=str))
    except ToolError as error:
        log(event="tool_refused", tool=name, reason=str(error))
        return {"error": str(error)}
    except Exception as error:  # noqa: BLE001 - never leak stack traces to the model
        log(event="tool_failed", tool=name, error=repr(error)[:500])
        return {"error": f"查询失败：{type(error).__name__}"}
