"""Unit tests for conversational analysis edits (tools/common/edits.py) and table/sort previews."""

import copy
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for sub in ("tools/common", "agent"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import definition as defs  # noqa: E402
import edits  # noqa: E402
import sandbox  # noqa: E402
from test_core import analysis_definition  # noqa: E402

COLUMNS = {"trade": {"t": "DATETIME", "station": "STRING", "price": "DECIMAL", "elec": "DECIMAL", "picked": "CALCULATED"}}


def base():
    d = analysis_definition()
    d["Sheets"][0]["Layouts"] = [{"Configuration": {"GridLayout": {"Elements": [
        {"ElementId": v, "ElementType": "VISUAL", "ColumnSpan": 18, "RowSpan": 12} for v in ("v1", "v2", "v3")]}}}]
    return d


def visual(d, visual_id):
    return next((k, b) for _s, _v, k, b in defs.visuals(d) if b["VisualId"] == visual_id)


class EditTest(unittest.TestCase):
    def test_change_type_to_table_and_back(self):
        d = base()
        vid, summary = edits.apply_edit(d, {"operation": "change_chart_type", "visual_id": "v1", "chart_type": "table"}, COLUMNS)
        kind, body = visual(d, "v1")
        self.assertEqual(kind, "TableVisual")
        self.assertEqual(len(body["ChartConfiguration"]["FieldWells"]["TableAggregatedFieldWells"]["GroupBy"]), 1)
        self.assertIn("表格", summary)
        edits.apply_edit(d, {"operation": "change_chart_type", "visual_id": "v1", "chart_type": "bar"}, COLUMNS)
        self.assertEqual(visual(d, "v1")[0], "BarChartVisual")

    def test_set_measures_category_color_title_sort(self):
        d = base()
        edits.apply_edit(d, {"operation": "set_measures", "visual_id": "v1",
                             "measures": [{"column": "price", "agg": "sum"}, {"column": "station", "agg": "count_distinct"}]}, COLUMNS)
        wells = visual(d, "v1")[1]["ChartConfiguration"]["FieldWells"]["LineChartAggregatedFieldWells"]
        self.assertEqual(wells["Values"][0]["NumericalMeasureField"]["AggregationFunction"]["SimpleNumericalAggregation"], "SUM")
        self.assertEqual(wells["Values"][1]["CategoricalMeasureField"]["AggregationFunction"], "DISTINCT_COUNT")
        edits.apply_edit(d, {"operation": "set_category", "visual_id": "v1", "category": {"column": "t", "grain": "month"}}, COLUMNS)
        self.assertEqual(wells["Category"][0]["DateDimensionField"]["DateGranularity"], "MONTH")
        edits.apply_edit(d, {"operation": "set_color", "visual_id": "v1", "color": {"column": "station"}}, COLUMNS)
        self.assertIn("CategoricalDimensionField", wells["Colors"][0])
        edits.apply_edit(d, {"operation": "set_title", "visual_id": "v1", "title": "月度价格"}, COLUMNS)
        self.assertEqual(defs.text(visual(d, "v1")[1]["Title"]), "月度价格")
        edits.apply_edit(d, {"operation": "sort", "visual_id": "v1", "sort": {"by": "measure", "direction": "desc"}}, COLUMNS)
        sort = visual(d, "v1")[1]["ChartConfiguration"]["SortConfiguration"]["CategorySort"][0]["FieldSort"]
        self.assertEqual((sort["FieldId"], sort["Direction"]), (wells["Values"][0]["NumericalMeasureField"]["FieldId"], "DESC"))
        edits.apply_edit(d, {"operation": "set_color", "visual_id": "v1", "color": None}, COLUMNS)
        self.assertNotIn("Colors", wells)

    def test_add_and_delete_visual_updates_layout(self):
        d = base()
        vid, summary = edits.apply_edit(d, {"operation": "add_visual", "chart_type": "bar", "title": "各场站电量",
                                            "category": {"column": "station"},
                                            "measures": [{"column": "elec", "agg": "sum"}]}, COLUMNS)
        self.assertTrue(vid.startswith("qsca-"))
        self.assertIn(vid, [e["ElementId"] for e in d["Sheets"][0]["Layouts"][0]["Configuration"]["GridLayout"]["Elements"]])
        query = defs.preview_query(d, vid, {})["query"]
        self.assertEqual((query["dimensions"], query["metrics"]), (["station"], [{"column": "elec", "agg": "sum"}]))
        edits.apply_edit(d, {"operation": "delete_visual", "visual_id": vid}, COLUMNS)
        self.assertNotIn(vid, [b["VisualId"] for _s, _v, _k, b in defs.visuals(d)])
        self.assertNotIn(vid, [e["ElementId"] for e in d["Sheets"][0]["Layouts"][0]["Configuration"]["GridLayout"]["Elements"]])

    def test_table_preview_with_two_group_bys_and_sort(self):
        d = base()
        vid, _ = edits.apply_edit(d, {"operation": "add_visual", "chart_type": "table", "category": {"column": "station"},
                                      "measures": [{"column": "price", "agg": "avg"}]}, COLUMNS)
        _k, body = visual(d, vid)
        body["ChartConfiguration"]["FieldWells"]["TableAggregatedFieldWells"]["GroupBy"].append(
            {"DateDimensionField": {"FieldId": "x", "Column": {"DataSetIdentifier": "trade", "ColumnName": "t"}, "DateGranularity": "MONTH"}})
        edits.apply_edit(d, {"operation": "sort", "visual_id": vid, "sort": {"by": "measure", "direction": "desc"}}, COLUMNS)
        query = defs.preview_query(d, vid, {})["query"]
        self.assertEqual(query["time"], {"column": "t", "grain": "month"})
        self.assertEqual(query["dimensions"], ["station"])
        self.assertEqual(query["order_by"], [{"name": "price", "desc": True}])

    def test_refusals(self):
        d = base()
        cases = [
            {"operation": "drop_table"},
            {"operation": "set_measures", "visual_id": "v1", "measures": [{"column": "nope", "agg": "sum"}]},
            {"operation": "set_measures", "visual_id": "v1", "measures": [{"column": "station", "agg": "sum"}]},
            {"operation": "set_category", "visual_id": "v1", "category": {"column": "t", "grain": "decade"}},
            {"operation": "set_title", "visual_id": "v3", "title": "x"} | {"operation": "set_measures", "measures": []},
            {"operation": "change_chart_type", "visual_id": "v3", "chart_type": "bar"},  # KPI
            {"operation": "delete_visual", "visual_id": "qsca-chat"},
            {"operation": "add_visual", "chart_type": "pie", "category": {"column": "station"}, "measures": [{"column": "elec", "agg": "sum"}]},
        ]
        for case in cases:
            before = copy.deepcopy(d)
            with self.assertRaises(edits.EditError, msg=case):
                edits.apply_edit(d, case, COLUMNS)
            self.assertEqual(d, before, msg=f"definition changed by refused edit {case}")

    def test_delete_refused_when_referenced(self):
        d = base()
        d["FilterGroups"][0]["ScopeConfiguration"]["SelectedSheets"]["SheetVisualScopingConfigurations"] = [
            {"SheetId": "s", "Scope": "SELECTED_VISUALS", "VisualIds": ["v2"]}]
        with self.assertRaises(edits.EditError):
            edits.apply_edit(d, {"operation": "delete_visual", "visual_id": "v2"}, COLUMNS)


class SandboxCsvTest(unittest.TestCase):
    def test_csv_union_of_keys_and_nulls(self):
        text = sandbox.to_csv([{"a": 1, "b": None}, {"a": 2, "c": "x"}])
        self.assertEqual(text.splitlines(), ["a,b,c", "1,,", "2,,x"])


if __name__ == "__main__":
    unittest.main()
