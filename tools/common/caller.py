"""Who is calling, and what may they do in QuickSight. Shared by the analysis and data tool Lambdas.

Every tool call carries `_caller` (the user's signed chat session token) and `_analysis` (the analysis the chat panel
is embedded in). The agent Runtime adds both in code; the model can neither see nor set them. This module verifies
the token with the web Lambda's HMAC key, maps the email to QuickSight users, and answers permission questions
using QuickSight's own sharing settings, so the assistant never lets anyone do more than QuickSight would.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time

import boto3

ACCOUNT = os.environ["ACCOUNT_ID"]
QS_REGION = os.environ.get("QS_REGION") or os.environ.get("AWS_REGION")
NAMESPACE = os.environ.get("QS_NAMESPACE", "default")
SESSION_SECRET_ID = os.environ["SESSION_SECRET_ID"]
ALLOWED_ANALYSES = [a for a in os.environ.get("ALLOWED_ANALYSIS_IDS", "*").split(",") if a]
CACHE_SECONDS = 60
ANALYSIS_ID = re.compile(r"^[\w-]{1,512}$")

qs = boto3.client("quicksight", region_name=QS_REGION)
_cache = {}


class ToolError(Exception):
    """A refusal or a user-facing problem; the message is returned to the model as {"error": ...}."""


def cached(key, loader, seconds=CACHE_SECONDS):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    value = loader()
    _cache[key] = (time.time(), value)
    return value


def _session_key():
    def load():
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=SESSION_SECRET_ID)["SecretString"]
        return hashlib.sha256(raw.encode("utf-8")).digest()  # same derivation as web/auth.py
    return cached("session-key", load, 3600)


def verify_token(token):
    """Same check as web/auth.py verify_session: HMAC-SHA256 over the body, then expiry. Returns the email."""
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 2048:
        raise ToolError("缺少已登录用户的身份")
    body, signature = token.split(".")
    expected = base64.urlsafe_b64encode(hmac.new(_session_key(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected.rstrip(b"=").decode("ascii")):
        raise ToolError("用户身份校验失败")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if int(payload.get("exp", 0)) <= time.time():
        raise ToolError("登录已过期，请重新登录后再试")
    return payload["sub"]


def analysis_id(args):
    value = str(args.get("_analysis") or "")
    if not ANALYSIS_ID.match(value):
        raise ToolError("聊天窗口没有绑定分析（嵌入地址缺少 analysis 参数），请联系管理员重新接入")
    if "*" not in ALLOWED_ANALYSES and value not in ALLOWED_ANALYSES:
        raise ToolError(f"分析 {value} 未开放给聊天助手")
    return value


def _paged(call, key, **kwargs):
    items, token = [], None
    while True:
        page = call(AwsAccountId=ACCOUNT, **kwargs, **({"NextToken": token} if token else {}))
        items += page.get(key, [])
        token = page.get("NextToken")
        if not token:
            return items


def principals(email):
    """ARNs of the active QuickSight users with this email and of their groups."""
    def load():
        users = [u for u in _paged(qs.list_users, "UserList", Namespace=NAMESPACE)
                 if (u.get("Email") or "").lower() == email.lower() and u.get("Active", True)]
        arns = set()
        for user in users:
            arns.add(user["Arn"])
            groups = _paged(qs.list_user_groups, "GroupList", Namespace=NAMESPACE, UserName=user["UserName"])
            arns.update(group["Arn"] for group in groups)
        return arns
    found = cached(f"principals:{email.lower()}", load)
    if not found:
        raise ToolError(f"{email} 在 QuickSight 里没有对应的用户")
    return found


def _has(permissions, arns, action=None):
    return any(p["Principal"] in arns and (action is None or action in p["Actions"]) for p in permissions)


def analysis_permissions(aid):
    return cached(f"aperm:{aid}", lambda: qs.describe_analysis_permissions(
        AwsAccountId=ACCOUNT, AnalysisId=aid).get("Permissions", []))


SOURCE_TAG = "qsca:source-analysis"  # set by `qsca publish`, whose dashboards are built from a definition


def dashboards_of(aid):
    """Dashboards whose current version was published from this analysis (console publish: SourceEntityArn;
    `qsca publish`: the SOURCE_TAG tag)."""
    def load():
        partition = "aws-cn" if QS_REGION.startswith("cn-") else "aws"
        arn = f"arn:{partition}:quicksight:{QS_REGION}:{ACCOUNT}:analysis/{aid}"
        found = []
        for summary in _paged(qs.list_dashboards, "DashboardSummaryList"):
            try:
                version = qs.describe_dashboard(AwsAccountId=ACCOUNT, DashboardId=summary["DashboardId"])["Dashboard"]["Version"]
            except qs.exceptions.ResourceNotFoundException:
                continue
            if version.get("SourceEntityArn") == arn:
                found.append(summary["DashboardId"])
                continue
            try:
                tags = qs.list_tags_for_resource(ResourceArn=summary["Arn"]).get("Tags", [])
            except Exception:  # noqa: BLE001 - tags are an optional link
                tags = []
            if any(t["Key"] == SOURCE_TAG and t["Value"] == aid for t in tags):
                found.append(summary["DashboardId"])
        return found
    return cached(f"dashboards:{aid}", load, 300)


def require_viewer(args):
    """The caller may view the analysis, or a dashboard published from it. Returns (email, analysis_id)."""
    email, aid = verify_token(args.get("_caller")), analysis_id(args)
    arns = principals(email)
    if _has(analysis_permissions(aid), arns):
        return email, aid
    for dashboard_id in dashboards_of(aid):
        perms = cached(f"dperm:{dashboard_id}", lambda d=dashboard_id: qs.describe_dashboard_permissions(
            AwsAccountId=ACCOUNT, DashboardId=d).get("Permissions", []))
        if _has(perms, arns):
            return email, aid
    raise ToolError(f"{email} 没有查看这个分析或其仪表盘的权限")


def require_editor(args):
    """The caller holds quicksight:UpdateAnalysis on the analysis (directly or through a group)."""
    email, aid = verify_token(args.get("_caller")), analysis_id(args)
    if not _has(analysis_permissions(aid), principals(email), "quicksight:UpdateAnalysis"):
        raise ToolError(f"{email} 没有编辑这个分析的权限，未做任何修改")
    return email, aid


def log(**fields):
    print(json.dumps(fields, ensure_ascii=False, default=str))
