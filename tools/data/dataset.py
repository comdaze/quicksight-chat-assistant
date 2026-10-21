"""Work out, from QuickSight metadata only, how to query a dataset directly at its source, or why that is unsafe.

Supported in this version: a dataset with exactly one physical table (a relational table or custom SQL) on a
PostgreSQL / Aurora PostgreSQL / MySQL / Aurora MySQL / MariaDB / RDS (PostgreSQL or MySQL) data source, whose logical
table only projects, renames, casts or tags columns. Anything that would change which rows a user sees in QuickSight
- joins, dataset filters, row-level security, column-level security the caller is not granted - is refused, because
querying the source directly would bypass it. Calculated columns are hidden (QuickSight expressions are not SQL).
"""

ENGINES = {"POSTGRESQL": "postgresql", "AURORA_POSTGRESQL": "postgresql", "MYSQL": "mysql", "AURORA": "mysql",
           "MARIADB": "mysql"}
PARAM_KEYS = {"PostgreSqlParameters": "postgresql", "AuroraPostgreSqlParameters": "postgresql",
              "MySqlParameters": "mysql", "AuroraParameters": "mysql", "MariaDbParameters": "mysql",
              "RdsParameters": None}
IGNORED_TRANSFORMS = ("CastColumnTypeOperation", "TagColumnOperation", "UntagColumnOperation",
                      "OverrideDatasetParameterOperation")


class Unsupported(Exception):
    """The dataset cannot be queried safely by the built-in tool; the message says why."""


def plan(dataset, caller_arns):
    """dataset: DescribeDataSet()["DataSet"]. Returns {"data_source_arn", "source", "columns", "hidden", "notes"}."""
    if (dataset.get("RowLevelPermissionDataSet") or {}).get("Status", "ENABLED") == "ENABLED" and \
            dataset.get("RowLevelPermissionDataSet"):
        raise Unsupported("数据集启用了行级权限，直接查询数据源会绕过它")
    if (dataset.get("RowLevelPermissionTagConfiguration") or {}).get("Status") == "ENABLED":
        raise Unsupported("数据集启用了基于标签的行级权限，直接查询数据源会绕过它")
    physical = dataset.get("PhysicalTableMap") or {}
    if len(physical) != 1:
        raise Unsupported(f"数据集有 {len(physical)} 个物理表（含联接），暂不支持直接查询")
    physical_id, table = next(iter(physical.items()))
    kind, body = next(iter(table.items()))
    if kind == "RelationalTable":
        source = {"table": (body.get("Schema"), body["Name"])}
        input_columns = {c["Name"]: c["Type"] for c in body.get("InputColumns", [])}
    elif kind == "CustomSql":
        source = {"sql": body["SqlQuery"].strip().rstrip(";")}
        input_columns = {c["Name"]: c["Type"] for c in body.get("Columns", [])}
    else:
        raise Unsupported(f"物理表类型 {kind} 暂不支持（如 S3 文件）")

    logical = dataset.get("LogicalTableMap") or {}
    if len(logical) > 1:
        raise Unsupported("数据集有多个逻辑表（含联接），暂不支持直接查询")
    names = {name: name for name in input_columns}  # current name -> physical column
    types = dict(input_columns)
    hidden, notes = [], []
    for table_body in logical.values():
        if table_body.get("Source", {}).get("PhysicalTableId") != physical_id:
            raise Unsupported("逻辑表不是直接来自物理表（含联接），暂不支持直接查询")
        for transform in table_body.get("DataTransforms", []):
            op, args = next(iter(transform.items()))
            if op == "RenameColumnOperation":
                old, new = args["ColumnName"], args["NewColumnName"]
                if old in names:
                    names[new] = names.pop(old)
                    types[new] = types.pop(old)
            elif op == "ProjectOperation":
                keep = set(args.get("ProjectedColumns", []))
                hidden += [n for n in names if n not in keep]
                names = {n: p for n, p in names.items() if n in keep}
            elif op == "CreateColumnsOperation":
                hidden += [c["ColumnName"] for c in args.get("Columns", [])]
            elif op == "FilterOperation":
                raise Unsupported("数据集定义了过滤条件，直接查询数据源会绕过它")
            elif op not in IGNORED_TRANSFORMS:
                raise Unsupported(f"数据集里有暂不支持的转换 {op}")
    output_types = {c["Name"]: c["Type"] for c in dataset.get("OutputColumns", [])}
    for rule in dataset.get("ColumnLevelPermissionRules") or []:
        if not set(rule.get("Principals", [])) & set(caller_arns):
            for name in rule.get("ColumnNames", []):
                if name in names:
                    names.pop(name)
                    hidden.append(name)
                    notes.append(f"列 {name} 受列级权限限制，已隐藏")
    columns = {name: {"expr": physical_name, "type": output_types.get(name, _qs_type(types.get(name)))}
               for name, physical_name in names.items()}
    return {"data_source_arn": body["DataSourceArn"], "source": source, "columns": columns,
            "hidden": sorted(set(hidden)), "notes": notes}


def _qs_type(input_type):
    return {"STRING": "STRING", "INTEGER": "INTEGER", "DECIMAL": "DECIMAL", "DATETIME": "DATETIME",
            "BIT": "INTEGER", "BOOLEAN": "INTEGER", "JSON": "STRING"}.get(input_type or "", "STRING")


def connection(data_source):
    """data_source: DescribeDataSource()["DataSource"]. Returns {"engine", "host"|"rds_instance", "port", "database", "ssl"}."""
    parameters = data_source.get("DataSourceParameters") or {}
    if not parameters:
        raise Unsupported(f"数据源类型 {data_source.get('Type')} 暂不支持")
    key, values = next(iter(parameters.items()))
    if key not in PARAM_KEYS:
        raise Unsupported(f"数据源类型 {data_source.get('Type')} 暂不支持（第一版支持 PostgreSQL 和 MySQL 系）")
    engine = PARAM_KEYS[key] or ENGINES.get(data_source.get("Type", ""))
    out = {"engine": engine, "database": values.get("Database"),
           "ssl": not (data_source.get("SslProperties") or {}).get("DisableSsl", False)}
    if key == "RdsParameters":
        out["rds_instance"] = values["InstanceId"]
    else:
        out["host"], out["port"] = values["Host"], int(values.get("Port") or (5432 if engine == "postgresql" else 3306))
    return out
