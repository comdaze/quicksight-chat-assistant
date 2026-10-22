"""Marks this solution's chat in published dashboards, triggered by EventBridge on CreateDashboard / UpdateDashboard.

The browser does not tell the chat page which QuickSight page embeds it, and publishing in the console copies the
analysis' chat URL unchanged. After every publish this function adds where=dashboard to the chat URL in the new
dashboard version (and publishes that), so the chat in a dashboard knows it cannot change what the viewer sees. It
also tags the dashboard with its source analysis, which keeps dashboard-only viewers allowed (caller.dashboards_of).
Its own UpdateDashboard call triggers it again; the URL is then already marked and nothing happens.
"""

import json
import os
import re
import time

import boto3

ACCOUNT = os.environ["ACCOUNT_ID"]
CHAT_URL = os.environ["CHAT_URL"].rstrip("/")
SOURCE_TAG = "qsca:source-analysis"
qs = boto3.client("quicksight", region_name=os.environ.get("QS_REGION"))


def log(**fields):
    print(json.dumps(fields, ensure_ascii=False, default=str))


def mark(definition):
    """Add where=dashboard to every chat URL of this deployment; returns how many were changed."""
    changed = 0
    for sheet in definition.get("Sheets", []):
        for visual in sheet.get("Visuals", []):
            config = (visual.get("CustomContentVisual") or {}).get("ChartConfiguration") or {}
            url = config.get("ContentUrl") or ""
            if not url.startswith(CHAT_URL) or re.search(r"[?&]where=dashboard(&|$)", url):
                continue
            url = re.sub(r"[?&]where=[^&]*", "", url)
            config["ContentUrl"] = url + ("&" if "?" in url else "?") + "where=dashboard"
            changed += 1
    return changed


def _settled(dashboard_id, version=None):
    for _ in range(40):
        args = {"VersionNumber": version} if version else {}
        info = qs.describe_dashboard(AwsAccountId=ACCOUNT, DashboardId=dashboard_id, **args)["Dashboard"]
        if not info["Version"]["Status"].endswith("_IN_PROGRESS"):
            return info
        time.sleep(3)
    raise TimeoutError(f"dashboard {dashboard_id} still in progress")


def ids_from(event):
    """(dashboard id, analysis id or "") from an EventBridge CloudTrail event. Console publishes arrive as service
    events naming the analysis; API calls (e.g. qsca publish) carry only the dashboard id."""
    detail = event.get("detail") or {}
    request = detail.get("requestParameters") or {}
    service = (detail.get("serviceEventDetails") or {}).get("eventRequestDetails") or {}
    dashboard = request.get("dashboardId") or str(service.get("dashboardId") or "").rsplit("/", 1)[-1]
    return dashboard, str(service.get("analysisId") or "").rsplit("/", 1)[-1]


def handler(event, _context):
    dashboard_id, analysis_id = ids_from(event)
    if not dashboard_id:
        return {"skipped": "no dashboard id"}
    # The latest version, not the published one: our own UpdateDashboard triggers this again before its version is
    # published, and that newer version is already marked.
    versions = qs.list_dashboard_versions(AwsAccountId=ACCOUNT, DashboardId=dashboard_id)["DashboardVersionSummaryList"]
    latest = max(v["VersionNumber"] for v in versions)
    info = _settled(dashboard_id, latest)
    version = info["Version"]
    analysis_id = analysis_id or str(version.get("SourceEntityArn") or "").rsplit("/", 1)[-1]
    out = qs.describe_dashboard_definition(AwsAccountId=ACCOUNT, DashboardId=dashboard_id,
                                           VersionNumber=version["VersionNumber"])
    definition = out["Definition"]
    if not mark(definition):
        return {"dashboard": dashboard_id, "changed": False}
    new = qs.update_dashboard(AwsAccountId=ACCOUNT, DashboardId=dashboard_id, Name=out["Name"], Definition=definition,
                              DashboardPublishOptions=out.get("DashboardPublishOptions") or {},
                              VersionDescription=f"chat marked as dashboard (from version {version['VersionNumber']})")
    number = int(new["VersionArn"].rsplit("/", 1)[-1])
    status = _settled(dashboard_id, number)["Version"]
    if not status["Status"].endswith("SUCCESSFUL"):
        log(event="dashboard_mark_failed", dashboard=dashboard_id, version=number, status=status["Status"],
            errors=status.get("Errors"))
        return {"dashboard": dashboard_id, "changed": False}
    qs.update_dashboard_published_version(AwsAccountId=ACCOUNT, DashboardId=dashboard_id, VersionNumber=number)
    if analysis_id:
        arn = info["Arn"].split("/version/")[0]  # describe_dashboard with a version number returns a version ARN
        qs.tag_resource(ResourceArn=arn, Tags=[{"Key": SOURCE_TAG, "Value": analysis_id}])
    log(event="dashboard_marked", dashboard=dashboard_id, analysis=analysis_id, from_version=version["VersionNumber"],
        version=number)
    return {"dashboard": dashboard_id, "changed": True, "version": number}
