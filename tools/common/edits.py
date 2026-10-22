"""Conversational edits to a QuickSight analysis definition. Pure functions: no AWS calls.

One edit is a dict {"operation": ..., ...}. apply_edit(definition, edit, columns) changes the definition in place
and returns (visual_id, summary) - the visual to preview (None when it was deleted) and a one-line Chinese summary.
The same function runs for the preview (on a copy) and for the real change (on the definition current at apply
time), so applying never depends on a stale copy of the analysis.

Operations
  change_chart_type  visual_id, chart_type: line | bar | table
  set_title          visual_id, title
  set_measures       visual_id, measures: [{column, agg}]      replace the values of a line / bar / table
  set_category       visual_id, category: {column, grain?}     replace the x axis (line / bar) or first group-by (table)
  set_color          visual_id, color: {column} | null          series / group-by split (line / bar)
  sort               visual_id, sort: {by: category | measure, direction: asc | desc}
  add_visual         chart_type, title, category, measures, color?, dataset?, sheet_id?
  delete_visual      visual_id
columns: {dataset identifier: {column name: QuickSight type}} for validation (from DescribeDataSet + calculated fields).
"""

import copy
import uuid

from definition import KIND_NAMES, SHARED_CHART_KEYS, KEEP_VISUAL_KEYS, text, visuals

KINDS = {"line": "LineChartVisual", "bar": "BarChartVisual", "table": "TableVisual"}
AGG_TO_QS = {"sum": "SUM", "avg": "AVERAGE", "min": "MIN", "max": "MAX", "count": "COUNT", "count_distinct": "DISTINCT_COUNT"}
GRAIN_TO_QS = {"year": "YEAR", "quarter": "QUARTER", "month": "MONTH", "week": "WEEK", "day": "DAY", "hour": "HOUR"}
NUMERIC = ("INTEGER", "DECIMAL")
PROTECTED = ("qsca-chat",)  # the chat panel itself
OPERATIONS = ("change_chart_type", "set_title", "set_measures", "set_category", "set_color", "sort", "add_visual",
              "delete_visual")


class EditError(ValueError):
    pass


def _fid():
    return "qsca-" + uuid.uuid4().hex[:10]


def _find(definition, visual_id):
    for sheet, visual, kind, body in visuals(definition):
        if body.get("VisualId") == visual_id:
            return sheet, visual, kind, body
    raise EditError(f"分析里没有图表 {visual_id}；可用的 visual_id 见分析结构")


def _wells(kind, body):
    """(wells dict, wells key) of a line / bar / table visual."""
    if kind not in KINDS.values():
        raise EditError(f"{body.get('VisualId')} 是{KIND_NAMES.get(kind, kind)}，目前只能编辑折线图、柱状图和表格")
    config = body.setdefault("ChartConfiguration", {})
    field_wells = config.setdefault("FieldWells", {})
    key = {"LineChartVisual": "LineChartAggregatedFieldWells", "BarChartVisual": "BarChartAggregatedFieldWells",
           "TableVisual": "TableAggregatedFieldWells"}[kind]
    if field_wells and key not in field_wells:
        raise EditError(f"{body.get('VisualId')} 使用了非聚合字段井（{next(iter(field_wells))}），暂不支持编辑")
    return field_wells.setdefault(key, {}), key


def _dataset_of(body, kind, default):
    try:
        wells, _key = _wells(kind, copy.deepcopy(body))
    except EditError:
        return default
    for entries in wells.values():
        for entry in entries if isinstance(entries, list) else []:
            column = next(iter(entry.values())).get("Column") or {}
            if column.get("DataSetIdentifier"):
                return column["DataSetIdentifier"]
    return default


def _column_type(columns, dataset, name):
    known = columns.get(dataset) or {}
    if name not in known:
        sample = ", ".join(sorted(known)[:40])
        raise EditError(f"数据集 {dataset} 没有列 {name}；可用列：{sample}")
    return known[name]


def _dimension(columns, dataset, spec):
    if not isinstance(spec, dict) or not spec.get("column"):
        raise EditError("category / color 需要 {\"column\": 列名}")
    column = {"DataSetIdentifier": dataset, "ColumnName": spec["column"]}
    kind = _column_type(columns, dataset, spec["column"])
    if kind in ("DATETIME", "DATE"):
        grain = GRAIN_TO_QS.get(str(spec.get("grain") or "day").lower())
        if not grain:
            raise EditError(f"grain 只能是 {', '.join(GRAIN_TO_QS)}")
        return {"DateDimensionField": {"FieldId": _fid(), "Column": column, "DateGranularity": grain}}
    if kind in NUMERIC:
        return {"NumericalDimensionField": {"FieldId": _fid(), "Column": column}}
    return {"CategoricalDimensionField": {"FieldId": _fid(), "Column": column}}


def _measure(columns, dataset, spec):
    if not isinstance(spec, dict) or spec.get("agg") not in AGG_TO_QS:
        raise EditError(f"measures[].agg 只能是 {', '.join(AGG_TO_QS)}")
    if not spec.get("column"):
        raise EditError("measures[] 需要 column")
    column = {"DataSetIdentifier": dataset, "ColumnName": spec["column"]}
    kind = _column_type(columns, dataset, spec["column"])
    agg = AGG_TO_QS[spec["agg"]]
    if kind in NUMERIC or kind == "CALCULATED":
        return {"NumericalMeasureField": {"FieldId": _fid(), "Column": column,
                                          "AggregationFunction": {"SimpleNumericalAggregation": agg}}}
    if agg not in ("COUNT", "DISTINCT_COUNT"):
        raise EditError(f"{spec['column']} 不是数值列，只能 count 或 count_distinct")
    target = "DateMeasureField" if kind in ("DATETIME", "DATE") else "CategoricalMeasureField"
    return {target: {"FieldId": _fid(), "Column": column, "AggregationFunction": agg}}


def _rebuild(kind, body, to_kind):
    """Rebuild a line / bar / table visual as another of those types, keeping its fields, title and actions."""
    wells, _key = _wells(kind, body)
    category = wells.get("Category") or wells.get("GroupBy", [])[:1]
    colors = wells.get("Colors") or (wells.get("GroupBy", [])[1:2] if kind == "TableVisual" else [])
    values = wells.get("Values", [])
    old = body.get("ChartConfiguration", {})
    common = {k: v for k, v in old.items() if k in SHARED_CHART_KEYS and to_kind != "TableVisual" and kind != "TableVisual"}
    keep = {k: body[k] for k in KEEP_VISUAL_KEYS if k in body}
    if to_kind == "TableVisual":
        config = {"FieldWells": {"TableAggregatedFieldWells": {"GroupBy": category + colors, "Values": values}}}
    else:
        picked = {k: v for k, v in (("Category", category), ("Values", values), ("Colors", colors)) if v}
        if to_kind == "BarChartVisual":
            config = {"FieldWells": {"BarChartAggregatedFieldWells": picked}, "Orientation": "VERTICAL",
                      "BarsArrangement": "CLUSTERED", **common}
        else:
            config = {"FieldWells": {"LineChartAggregatedFieldWells": picked}, "Type": "LINE", **common}
    return {to_kind: {**keep, "ChartConfiguration": config}}


def _title(value):
    return {"Visibility": "VISIBLE", "FormatText": {"PlainText": str(value)[:200]}}


def _references(definition, visual_id):
    """Places other than the visual itself that name it (filter scopes, actions of other visuals)."""
    found = []
    for group in definition.get("FilterGroups", []):
        for item in group.get("ScopeConfiguration", {}).get("SelectedSheets", {}).get("SheetVisualScopingConfigurations", []):
            if visual_id in item.get("VisualIds", []):
                found.append(f"筛选组 {group.get('FilterGroupId')}")
    for _sheet, _visual, _kind, body in visuals(definition):
        if body.get("VisualId") == visual_id:
            continue
        for action in body.get("Actions", []):
            for operation in action.get("ActionOperations", []):
                targets = (operation.get("FilterOperation", {}).get("TargetVisualsConfiguration", {})
                           .get("SameSheetTargetVisualConfiguration", {}).get("TargetVisuals", []))
                if visual_id in targets:
                    found.append(f"图表 {body.get('VisualId')} 的动作 {action.get('Name')}")
    return found


def _fields(value):
    """Every field-well entry body ({"FieldId", "Column", ...}) under a visual's FieldWells, keyed by its type."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if key.endswith(("DimensionField", "MeasureField")) and isinstance(inner, dict) and "FieldId" in inner:
                yield key, inner
            else:
                yield from _fields(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _fields(inner)


def _sync_hierarchies(body):
    """QuickSight requires every date dimension in a visual to point at a DateTimeHierarchy of that visual
    ("Datetime field should have a datetime hierarchy"), and rejects hierarchies nobody uses. Edits replace fields,
    so rebuild the links: keep used hierarchies, add one for each date field that has none."""
    fields = list(_fields(body.get("ChartConfiguration", {}).get("FieldWells", {})))
    hierarchies = body.get("ColumnHierarchies") or []
    known = {next(iter(h.values())).get("HierarchyId") for h in hierarchies}
    for key, field in fields:
        if key == "DateDimensionField" and field.get("HierarchyId") not in known:
            field["HierarchyId"] = field["FieldId"]
            hierarchies.append({"DateTimeHierarchy": {"HierarchyId": field["FieldId"], "DrillDownFilters": []}})
            known.add(field["FieldId"])
    used = {f.get("HierarchyId") for _k, f in fields if f.get("HierarchyId")}
    hierarchies = [h for h in hierarchies if next(iter(h.values())).get("HierarchyId") in used]
    if hierarchies:
        body["ColumnHierarchies"] = hierarchies
    else:
        body.pop("ColumnHierarchies", None)


def apply_edit(definition, edit, columns):
    """Apply one edit in place; returns (visual id or None, summary)."""
    visual_id, summary = _apply_edit(definition, edit, columns)
    if visual_id:
        _s, _v, _kind, body = _find(definition, visual_id)
        _sync_hierarchies(body)
    return visual_id, summary


def _apply_edit(definition, edit, columns):
    if not isinstance(edit, dict) or edit.get("operation") not in OPERATIONS:
        raise EditError(f"operation 只能是 {', '.join(OPERATIONS)}")
    op = edit["operation"]
    datasets = [d["Identifier"] for d in definition.get("DataSetIdentifierDeclarations", [])]
    if not datasets:
        raise EditError("分析没有数据集")

    if op == "add_visual":
        to_kind = KINDS.get(str(edit.get("chart_type") or "").lower())
        if not to_kind:
            raise EditError("chart_type 只能是 line、bar 或 table")
        dataset = edit.get("dataset") or datasets[0]
        if dataset not in datasets:
            raise EditError(f"dataset 只能是 {', '.join(datasets)}（分析结构里 datasets 的键）")
        measures = [_measure(columns, dataset, m) for m in (edit.get("measures") or [])]
        if not 1 <= len(measures) <= 10:
            raise EditError("measures 需要 1 到 10 个")
        category = [_dimension(columns, dataset, edit.get("category"))]
        color = [_dimension(columns, dataset, edit["color"])] if edit.get("color") else []
        visual_id = _fid()
        title = _title(edit.get("title") or "新图表")
        if to_kind == "TableVisual":
            visual = {"TableVisual": {"VisualId": visual_id, "Title": title, "ChartConfiguration": {
                "FieldWells": {"TableAggregatedFieldWells": {"GroupBy": category + color, "Values": measures}}}}}
        else:
            wells = {"Category": category, "Values": measures, **({"Colors": color} if color else {})}
            key = "BarChartAggregatedFieldWells" if to_kind == "BarChartVisual" else "LineChartAggregatedFieldWells"
            extra = {"Orientation": "VERTICAL", "BarsArrangement": "CLUSTERED"} if to_kind == "BarChartVisual" else {"Type": "LINE"}
            visual = {to_kind: {"VisualId": visual_id, "Title": title,
                                "ChartConfiguration": {"FieldWells": {key: wells}, **extra}}}
        sheets = definition.get("Sheets", [])
        sheet = next((s for s in sheets if s.get("SheetId") == edit.get("sheet_id")), None) if edit.get("sheet_id") \
            else (sheets[0] if sheets else None)
        if sheet is None:
            raise EditError(f"没有工作表 {edit.get('sheet_id')}")
        layout = ((sheet.get("Layouts") or [{}])[0].get("Configuration") or {})
        if "GridLayout" in layout:
            layout["GridLayout"].setdefault("Elements", []).append(
                {"ElementId": visual_id, "ElementType": "VISUAL", "ColumnSpan": 18, "RowSpan": 12})
        elif "FreeFormLayout" in layout:
            layout["FreeFormLayout"].setdefault("Elements", []).append(
                {"ElementId": visual_id, "ElementType": "VISUAL", "XAxisLocation": "0px", "YAxisLocation": "0px",
                 "Width": "600px", "Height": "400px"})
        elif layout:
            raise EditError("这个工作表使用分节布局，暂不支持自动添加图表")
        sheet.setdefault("Visuals", []).append(visual)
        return visual_id, f"在工作表“{sheet.get('Name')}”新增{KIND_NAMES[to_kind]}“{text(title)}”"

    visual_id = str(edit.get("visual_id") or "")
    if visual_id in PROTECTED:
        raise EditError("不能修改聊天窗口本身")
    sheet, visual, kind, body = _find(definition, visual_id)
    name = text(body.get("Title")) or visual_id

    if op == "delete_visual":
        refs = _references(definition, visual_id)
        if refs:
            raise EditError(f"“{name}”被 {', '.join(refs)} 引用，请先在编辑器里解除引用再删除")
        sheet["Visuals"].remove(visual)
        for layout in sheet.get("Layouts", []):
            for kind_key in ("GridLayout", "FreeFormLayout"):
                elements = (layout.get("Configuration", {}).get(kind_key) or {}).get("Elements")
                if elements is not None:
                    elements[:] = [e for e in elements if e.get("ElementId") != visual_id]
        return None, f"删除图表“{name}”"

    if op == "set_title":
        if not edit.get("title"):
            raise EditError("set_title 需要 title")
        body["Title"] = _title(edit["title"])
        return visual_id, f"把“{name}”的标题改为“{edit['title']}”"

    if op == "change_chart_type":
        to_kind = KINDS.get(str(edit.get("chart_type") or "").lower())
        if not to_kind:
            raise EditError("chart_type 只能是 line、bar 或 table")
        if to_kind == kind:
            raise EditError(f"“{name}”已经是{KIND_NAMES[to_kind]}")
        _wells(kind, body)  # validates the source type
        sheet["Visuals"][sheet["Visuals"].index(visual)] = _rebuild(kind, body, to_kind)
        return visual_id, f"把“{name}”从{KIND_NAMES[kind]}改为{KIND_NAMES[to_kind]}"

    wells, _key = _wells(kind, body)
    dataset = edit.get("dataset") or _dataset_of(body, kind, datasets[0])
    if op == "set_measures":
        measures = [_measure(columns, dataset, m) for m in (edit.get("measures") or [])]
        if not 1 <= len(measures) <= 10:
            raise EditError("measures 需要 1 到 10 个")
        wells["Values"] = measures
        body.get("ChartConfiguration", {}).pop("SortConfiguration", None)  # may point at removed fields
        names = "、".join(f"{m['agg']}({m['column']})" for m in edit["measures"])
        return visual_id, f"把“{name}”的数值字段改为 {names}"
    if op == "set_category":
        dim = _dimension(columns, dataset, edit.get("category"))
        if kind == "TableVisual":
            wells["GroupBy"] = [dim] + wells.get("GroupBy", [])[1:]
        else:
            wells["Category"] = [dim]
        body.get("ChartConfiguration", {}).pop("SortConfiguration", None)
        grain = f"（按{edit['category'].get('grain')}）" if edit["category"].get("grain") else ""
        return visual_id, f"把“{name}”的分类字段改为 {edit['category']['column']}{grain}"
    if op == "set_color":
        if kind == "TableVisual":
            raise EditError("表格没有颜色分组；可以用 set_category 或新增表格")
        if edit.get("color"):
            wells["Colors"] = [_dimension(columns, dataset, edit["color"])]
            return visual_id, f"让“{name}”按 {edit['color']['column']} 分组显示"
        wells.pop("Colors", None)
        return visual_id, f"取消“{name}”的分组"
    if op == "sort":
        spec = edit.get("sort") or {}
        direction = "DESC" if str(spec.get("direction", "desc")).lower() == "desc" else "ASC"
        source = wells.get("Values", [])[:1] if spec.get("by") == "measure" else \
            (wells.get("Category") or wells.get("GroupBy", []))[:1]
        if not source:
            raise EditError("没有可以排序的字段")
        field_id = next(iter(source[0].values()))["FieldId"]
        sort_key = "RowSort" if kind == "TableVisual" else "CategorySort"
        body.setdefault("ChartConfiguration", {})["SortConfiguration"] = {
            sort_key: [{"FieldSort": {"FieldId": field_id, "Direction": direction}}]}
        by = "数值" if spec.get("by") == "measure" else "分类"
        return visual_id, f"让“{name}”按{by}{'降序' if direction == 'DESC' else '升序'}排列"
    raise EditError(f"未知操作 {op}")
