"""Unit tests for the dashboard publisher (tools/analysis/publisher.py): marking the chat URL, reading events."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools", "analysis"))
os.environ.setdefault("ACCOUNT_ID", "1")
os.environ.setdefault("CHAT_URL", "https://chat.example.com")
os.environ.setdefault("QS_REGION", "cn-north-1")

import publisher  # noqa: E402

CHAT = os.environ["CHAT_URL"] + "/?analysis=a1&p.pMetric=<<$pMetric>>"


def definition(url):
    return {"Sheets": [{"Visuals": [
        {"CustomContentVisual": {"VisualId": "qsca-chat", "ChartConfiguration": {"ContentUrl": url}}},
        {"CustomContentVisual": {"VisualId": "other", "ChartConfiguration": {"ContentUrl": "https://elsewhere/?x=1"}}},
        {"LineChartVisual": {"VisualId": "v1"}}]}]}


def urls(d):
    return [v["CustomContentVisual"]["ChartConfiguration"]["ContentUrl"] for v in d["Sheets"][0]["Visuals"]
            if "CustomContentVisual" in v]


class PublisherTest(unittest.TestCase):
    def test_marks_only_this_chat_and_only_once(self):
        d = definition(CHAT)
        self.assertEqual(publisher.mark(d), 1)
        self.assertEqual(urls(d), [CHAT + "&where=dashboard", "https://elsewhere/?x=1"])
        self.assertEqual(publisher.mark(d), 0)  # its own UpdateDashboard triggers it again: no new version

    def test_replaces_another_where_value(self):
        d = definition(CHAT + "&where=analysis")
        self.assertEqual(publisher.mark(d), 1)
        self.assertEqual(urls(d)[0], CHAT + "&where=dashboard")

    def test_ids_from_console_and_api_events(self):
        console = {"detail": {"requestParameters": None, "serviceEventDetails": {"eventRequestDetails": {
            "dashboardId": "arn:aws-cn:quicksight:cn-north-1:1:dashboard/d1",
            "analysisId": "arn:aws-cn:quicksight:cn-north-1:1:analysis/a1"}}}}
        self.assertEqual(publisher.ids_from(console), ("d1", "a1"))
        api = {"detail": {"requestParameters": {"dashboardId": "d2"}}}
        self.assertEqual(publisher.ids_from(api), ("d2", ""))


if __name__ == "__main__":
    unittest.main()
