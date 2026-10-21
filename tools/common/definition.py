"""Read a QuickSight analysis definition generically: visuals, parameters, controls, parameter-driven filters, and
what a visual plots in terms the data tool understands. Pure functions, no AWS calls."""

import copy
import re

# ifelse(${pX} = 'label', {column}, ..., default): the usual "pick a metric with a control" calculated field.
SWITCH_BRANCH = re.compile(r"\$\{(\w+)\}\s*=\s*'((?:[^'\\]|\\.)*)'\s*,\s*\{?([A-Za-z_]\w*)\}?")

CHART_KINDS = {"bar": "BarChartVisual", "line": "LineChartVisual"}
KIND_NAMES = {"BarChartVisual": "柱状图", "LineChartVisual": "折线图", "PieChartVisual": "饼图", "TableVisual": "表格",
              "PivotTableVisual": "透视表", "KPIVisual": "KPI", "ComboChartVisual": "组合图", "ScatterPlotVisual": "散点图",
              "CustomContentVisual": "自定义内容", "InsightVisual": "洞察", "HeatMapVisual": "热力图",
              "GaugeChartVisual": "仪表", "FilledMapVisual": "填充地图", "GeospatialMapVisual": "地图",
              "TreeMapVisual": "树状图", "FunnelChartVisual": "漏斗图", "WaterfallVisual": "瀑布图",
              "HistogramVisual": "直方图", "BoxPlotVisual": "箱线图", "SankeyDiagramVisual": "桑基图",
              "WordCloudVisual": "词云", "RadarChartVisual": "雷达图"}
GRAINS = {"YEAR": "year", "QUARTER": "quarter", "MONTH": "month", "WEEK": "week", "DAY": "day", "HOUR": "hour",
          "MINUTE": "minute"}
AGGS = {"SUM": "sum", "AVERAGE": "avg", "MIN": "min", "MAX": "max", "COUNT": "count", "DISTINCT_COUNT": "count_distinct",
        "MEDIAN": None, "STDEV": None, "STDEVP": None, "VAR": None, "VARP": None}
SHARED_CHART_KEYS = ("SortConfiguration", "Legend", "DataLabels", "Tooltip", "VisualPalette", "Interactions")
KEEP_VISUAL_KEYS = ("VisualId", "Title", "Subtitle", "Actions", "ColumnHierarchies", "VisualContentAltText")
ALL_SENTINELS = {"", "all", "[all]", "all values", "select all", "null", "undefined", "全部", "全选", "所有", "全部值"}


def visuals(definition):
    """Yield (sheet, visual, kind, body) for every visual in every sheet."""
    for sheet in definition.get("Sheets", []):
        for visual in sheet.get("Visuals", []):
            kind, body = next(iter(visual.items()))
            yield sheet, visual, kind, body


def text(title):
    fmt = (title or {}).get("FormatText") or {}
    return fmt.get("PlainText") or fmt.get("RichText") or ""


def dataset_ids(definition):
    """Dataset identifier (as used in field wells) -> dataset id."""
    return {d["Identifier"]: d["DataSetArn"].rsplit("/", 1)[-1] for d in definition.get("DataSetIdentifierDeclarations", [])}


def calculated_fields(definition):
    return {(c.get("DataSetIdentifier"), c["Name"]): c.get("Expression", "") for c in definition.get("CalculatedFields", [])}


def parameters(definition):
    """Parameter name -> {type, default, control title, linked dataset column}."""
    out = {}
    for declaration in definition.get("ParameterDeclarations", []):
        kind, body = next(iter(declaration.items()))
        defaults = body.get("DefaultValues") or {}
        static = defaults.get("StaticValues") or []
        out[body["Name"]] = {"type": kind.replace("ParameterDeclaration", "").lower(),
                             "default": static[0] if static else None,
                             "multi": body.get("ParameterValueType") == "MULTI_VALUED"}
    for sheet in definition.get("Sheets", []):
        for control in sheet.get("ParameterControls", []):
            kind, body = next(iter(control.items()))
            info = out.get(body.get("SourceParameterName"))
            if info is None:
                continue
            info["control"] = text({"FormatText": {"PlainText": body.get("Title", "")}}) or body.get("Title")
            info["control_type"] = kind
            link = (body.get("SelectableValues") or {}).get("LinkToDataSetColumn")
            if link:
                info["values_from"] = {"dataset": link["DataSetIdentifier"], "column": link["ColumnName"]}
            listed = (body.get("SelectableValues") or {}).get("Values")
            if listed:
                info["choices"] = listed[:50]
    return out


def _filter_bindings(flt):
    """(column, [(role, parameter)]) for one filter, or None when no parameter drives it."""
    kind, body = next(iter(flt.items()))
    column = body.get("Column") or {}
    roles = []
    if kind == "CategoryFilter":
        configuration = body.get("Configuration") or {}
        custom = configuration.get("CustomFilterConfiguration") or {}
        if custom.get("ParameterName"):
            roles.append(("eq" if custom.get("MatchOperator", "EQUALS") == "EQUALS" else "ne", custom["ParameterName"]))
        listed = configuration.get("FilterListConfiguration") or {}
        if listed.get("ParameterName"):
            roles.append(("in", listed["ParameterName"]))
    elif kind in ("TimeRangeFilter", "NumericRangeFilter"):
        for key, role in (("RangeMinimumValue", "gte"), ("RangeMaximumValue", "lte")):
            if (body.get(key) or {}).get("Parameter"):
                roles.append((role, body[key]["Parameter"]))
    elif kind in ("NumericEqualityFilter", "TimeEqualityFilter"):
        if body.get("ParameterName"):
            roles.append(("eq", body["ParameterName"]))
    if not roles or not column.get("ColumnName"):
        return None
    return {"dataset": column.get("DataSetIdentifier"), "column": column["ColumnName"],
            "roles": [{"op": op, "parameter": p} for op, p in roles],
            "inclusive": {"min": body.get("IncludeMinimum", True), "max": body.get("IncludeMaximum", True)}}


def parameter_filters(definition):
    """Filters whose values come from parameters (what a control changes), with the visuals they apply to."""
    out = []
    for group in definition.get("FilterGroups", []):
        if group.get("Status", "ENABLED") != "ENABLED":
            continue
        scope = group.get("ScopeConfiguration", {})
        visual_ids = None  # None = all visuals
        selected = scope.get("SelectedSheets", {}).get("SheetVisualScopingConfigurations", [])
        if selected:
            visual_ids = set()
            for item in selected:
                if item.get("Scope") == "SELECTED_VISUALS":
                    visual_ids.update(item.get("VisualIds", []))
                else:
                    visual_ids = None
                    break
        for flt in group.get("Filters", []):
            binding = _filter_bindings(flt)
            if binding:
                binding["visuals"] = sorted(visual_ids) if visual_ids is not None else "all"
                out.append(binding)
    return out


def _field(field):
    """One field-well entry -> {"column", "dataset", "kind", ...} in data-tool terms."""
    kind, body = next(iter(field.items()))
    column = body.get("Column") or {}
    out = {"column": column.get("ColumnName"), "dataset": column.get("DataSetIdentifier"), "field": kind}
    if kind == "DateDimensionField":
        out["grain"] = GRAINS.get(body.get("DateGranularity", "DAY"), "day")
    if kind == "NumericalMeasureField":
        name = (body.get("AggregationFunction") or {}).get("SimpleNumericalAggregation", "SUM")
        out["agg"] = AGGS.get(name)
    elif kind in ("CategoricalMeasureField", "DateMeasureField"):
        out["agg"] = "count_distinct" if body.get("AggregationFunction") == "DISTINCT_COUNT" else "count"
    return out


def visual_summary(kind, body, definition, full=False):
    """What a visual shows: title, dimensions, measures. Used by describe_analysis (expressions shortened for the
    model) and the preview (full=True: metric-switch fields need every branch)."""
    configuration = body.get("ChartConfiguration") or {}
    wells = next(iter((configuration.get("FieldWells") or {}).values()), {}) or {}
    dims, measures, colors = [], [], []
    for key, entries in wells.items():
        if not isinstance(entries, list):
            continue
        parsed = [_field(e) for e in entries if isinstance(e, dict) and e]
        if key in ("Values", "Measures", "Value", "TargetValues", "Size", "Weight"):
            measures += parsed
        elif key in ("Colors", "Color", "SmallMultiples"):
            colors += parsed
        else:
            dims += parsed
    calculated = calculated_fields(definition)
    for field in dims + measures + colors:
        expression = calculated.get((field["dataset"], field["column"]))
        if expression is not None:
            field["calculated"] = expression if full else expression[:300]
    return {"visual_id": body.get("VisualId"), "type": kind, "type_name": KIND_NAMES.get(kind, kind),
            "title": text(body.get("Title")), "dimensions": dims, "measures": measures, "colors": colors,
            "can_switch_type": kind in CHART_KINDS.values()}


def convert(body, to_kind):
    """Rebuild a line or bar chart visual as the other type, keeping id, title, actions and field wells."""
    old = body.get("ChartConfiguration", {})
    wells = next(iter(old.get("FieldWells", {}).values()), {})
    picked = {k: wells[k] for k in ("Category", "Values", "Colors") if k in wells}
    common = {k: v for k, v in old.items() if k in SHARED_CHART_KEYS}
    keep = {k: body[k] for k in KEEP_VISUAL_KEYS if k in body}
    if to_kind == "BarChartVisual":
        config = {"FieldWells": {"BarChartAggregatedFieldWells": picked}, "Orientation": "VERTICAL",
                  "BarsArrangement": "CLUSTERED", **common}
    elif to_kind == "LineChartVisual":
        config = {"FieldWells": {"LineChartAggregatedFieldWells": picked}, "Type": "LINE", **common}
    else:
        raise ValueError(f"unsupported chart kind {to_kind}")
    return {to_kind: {**keep, "ChartConfiguration": config}}


def replace_chart_type(definition, visual_id, to_kind):
    """Change one visual in place; returns the old kind. Raises ValueError when not possible."""
    for sheet, visual, kind, body in visuals(definition):
        if body["VisualId"] != visual_id:
            continue
        if kind == to_kind:
            raise ValueError(f"{visual_id} 已经是{KIND_NAMES[to_kind]}")
        if kind not in CHART_KINDS.values():
            raise ValueError(f"{visual_id} 是{KIND_NAMES.get(kind, kind)}，目前只支持折线图和柱状图互换")
        sheet["Visuals"][sheet["Visuals"].index(visual)] = convert(body, to_kind)
        return kind
    raise ValueError(f"分析里没有图表 {visual_id}；先调用 describe_analysis 查看可用的 visual_id")


def is_all(value):
    return value is None or (isinstance(value, str) and value.strip().lower() in ALL_SENTINELS) or \
        (isinstance(value, str) and value.startswith("<<"))


def resolve_switch(expression, parameter_values):
    """Column chosen by a parameter-switch calculated field for the current values, or None if it is not one."""
    body = expression.strip()
    if not body.lower().startswith("ifelse("):
        return None
    branches = SWITCH_BRANCH.findall(body)
    if not branches or len({p for p, _l, _c in branches}) != 1:
        return None
    parameter = branches[0][0]
    value = (parameter_values or {}).get(parameter)
    for _p, label, column in branches:
        if value == label:
            return column
    return None


def preview_query(definition, visual_id, parameter_values):
    """Turn a visual plus the current control values into a data-tool query, or explain why not.

    Returns {"query": {...}, "notes": [...]} or {"unsupported": reason}.
    """
    found = next(((kind, body) for _s, _v, kind, body in visuals(definition) if body["VisualId"] == visual_id), None)
    if not found:
        return {"unsupported": f"没有图表 {visual_id}"}
    # A parameter without a value from the embed URL uses its declared default, as QuickSight does.
    declared = parameters(definition)
    parameter_values = {**{n: str(i["default"])[:10] if i["type"] == "datetime" and i["default"] is not None else i["default"]
                           for n, i in declared.items() if i.get("default") is not None},
                        **{n: v for n, v in (parameter_values or {}).items() if v not in (None, "")}}
    summary = visual_summary(*found, definition, full=True)
    fields = summary["dimensions"] + summary["measures"] + summary["colors"]
    notes = []
    for f in fields:  # a metric-switch field reads one real column for the current control value
        if "calculated" in f:
            column = resolve_switch(f["calculated"], parameter_values)
            if column:
                notes.append(f"{f['column']} 按当前控件取 {column}")
                f["column"] = column
                del f["calculated"]
    calculated = [f["column"] for f in fields if "calculated" in f]
    if calculated:
        return {"unsupported": f"图表用了计算字段 {', '.join(calculated)}，聊天里无法按同样口径取数"}
    datasets = {f["dataset"] for f in fields}
    if len(datasets) != 1:
        return {"unsupported": "图表的字段来自多个数据集"}
    dataset = datasets.pop()
    if len(summary["dimensions"]) != 1 or not summary["measures"]:
        return {"unsupported": "只支持一个分类字段加若干数值字段的图表"}
    if any(m.get("agg") is None for m in summary["measures"]):
        return {"unsupported": "图表用了聊天里不支持的聚合方式（如中位数、标准差）"}
    dim = summary["dimensions"][0]
    query = {"dataset": dataset, "metrics": [{"column": m["column"], "agg": m["agg"]} for m in summary["measures"]],
             "dimensions": [], "filters": [], "limit": 500}
    if dim["field"] == "DateDimensionField":
        query["time"] = {"column": dim["column"], "grain": dim.get("grain", "day")}
    else:
        query["dimensions"].append(dim["column"])
    if summary["colors"]:
        query["dimensions"].append(summary["colors"][0]["column"])
    for binding in parameter_filters(definition):
        if binding["dataset"] != dataset or (binding["visuals"] != "all" and visual_id not in binding["visuals"]):
            continue
        for role in binding["roles"]:
            value = (parameter_values or {}).get(role["parameter"])
            if is_all(value):
                continue
            op = role["op"]
            if op == "in" and not isinstance(value, list):
                value = [value]
            query["filters"].append({"column": binding["column"], "op": op, "value": value})
    if any(f["dataset"] != dataset for f in fields):
        notes.append("部分字段口径无法确认")
    return {"query": query, "notes": notes, "summary": summary}


def strip_for_model(definition):
    """A compact view of the analysis for the model (no layout, no formatting)."""
    out = copy.deepcopy({"sheets": [], "parameters": parameters(definition), "filters": parameter_filters(definition),
                         "datasets": dataset_ids(definition)})
    for sheet in definition.get("Sheets", []):
        out["sheets"].append({"name": sheet.get("Name"), "sheet_id": sheet.get("SheetId"),
                              "visuals": [visual_summary(k, b, definition) for s, _v, k, b in visuals({"Sheets": [sheet]})]})
    return out
