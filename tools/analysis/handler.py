"""Gateway Lambda target: understand and edit the QuickSight analysis the chat panel is embedded in.

Tools (the Runtime adds `_caller`, `_analysis` and `_parameters` - the current control values - to every call):
- describe_analysis          sheets, visuals (what each one plots), parameters/controls, parameter-driven filters,
                             datasets. Anyone who may view the analysis or a dashboard published from it.
- preview_visual_change      validate a line <-> bar switch and record a pending change (10 minutes) owned by the
                             caller; returns the query that reproduces the visual's data for the preview chart.
- undo_last_visual_change    restore the definition saved before the latest applied change.
- apply_pending_change       NOT a gateway tool: only the web Lambda calls it, when the user presses 应用到分析.
Writes need quicksight:UpdateAnalysis for the caller on that analysis; every write first saves the previous
definition to S3.
"""

import datetime as dt
import json
import os
import time
import uuid

import boto3

import caller
import definition as defs
from caller import ToolError, log

BUCKET = os.environ["BACKUP_BUCKET"]
PENDING_SECONDS = 600
qs = caller.qs
s3 = boto3.client("s3")
jobs = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])


def _prefix(aid):
    return f"analysis-backups/{aid}/"


def _analysis(aid):
    out = qs.describe_analysis_definition(AwsAccountId=caller.ACCOUNT, AnalysisId=aid)
    return out["Name"], out["Definition"]


def _save(aid, name, definition):
    qs.update_analysis(AwsAccountId=caller.ACCOUNT, AnalysisId=aid, Name=name, Definition=definition)
    for _ in range(30):
        info = qs.describe_analysis(AwsAccountId=caller.ACCOUNT, AnalysisId=aid)["Analysis"]
        if not info["Status"].endswith("_IN_PROGRESS"):
            break
        time.sleep(1)
    if info.get("Errors") or not info["Status"].endswith("SUCCESSFUL"):
        raise ToolError(f"QuickSight 更新失败：{info['Status']} {info.get('Errors')}")
    caller._cache.pop(f"def:{aid}", None)
    return info["LastUpdatedTime"]


def describe_analysis(args):
    _email, aid = caller.require_viewer(args)
    name, definition = _analysis(aid)
    summary = defs.strip_for_model(definition)
    return {"analysis_id": aid, "name": name, **summary,
            "note": "visual_id 用于 preview_visual_change；datasets 的值是 dataset_id，用于 query_dataset。"}


def preview_visual_change(args):
    email, aid = caller.require_editor(args)
    visual_id = str(args.get("visual_id") or "")
    to = str(args.get("chart_type") or "").lower()
    if to not in defs.CHART_KINDS:
        raise ToolError("chart_type 只能是 bar（柱状图）或 line（折线图）")
    to_kind = defs.CHART_KINDS[to]
    _name, definition = _analysis(aid)
    found = next(((k, b) for _s, _v, k, b in defs.visuals(definition) if b["VisualId"] == visual_id), None)
    if not found:
        raise ToolError(f"分析里没有图表 {visual_id}；先调用 describe_analysis 查看可用的 visual_id")
    try:  # dry run on a copy: same validation as the real change
        defs.replace_chart_type(json.loads(json.dumps(definition, default=str)), visual_id, to_kind)
    except ValueError as error:
        raise ToolError(str(error)) from error
    kind, body = found
    parameters = args.get("_parameters") if isinstance(args.get("_parameters"), dict) else {}
    preview = defs.preview_query(definition, visual_id, parameters)
    ids = defs.dataset_ids(definition)
    if "query" in preview:
        preview["query"]["dataset_id"] = ids.get(preview["query"].pop("dataset"))
    change_id, now = uuid.uuid4().hex, int(time.time())
    jobs.put_item(Item={"jobId": f"pending#{change_id}", "status": "PENDING", "owner": email, "analysisId": aid,
                        "visualId": visual_id, "toKind": to_kind, "fromKind": kind, "createdAt": now,
                        "expiresAt": now + PENDING_SECONDS})
    log(event="visual_change_previewed", by=email, analysis=aid, visual=visual_id, old=kind, new=to_kind, change=change_id)
    return {"pending_change_id": change_id, "expires_at": now + PENDING_SECONDS, "visual_id": visual_id,
            "title": defs.text(body.get("Title")), "from": kind, "to": to_kind,
            "preview": {k: v for k, v in preview.items() if k != "summary"},
            "message": "尚未修改分析。聊天框会显示预览，用户点“应用到分析”后才会修改。"}


def _apply(email, aid, visual_id, to_kind):
    name, definition = _analysis(aid)
    original = json.loads(json.dumps(definition, default=str))
    try:
        old_kind = defs.replace_chart_type(definition, visual_id, to_kind)
    except ValueError as error:
        raise ToolError(str(error)) from error
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    key = f"{_prefix(aid)}active/{stamp}.json"
    s3.put_object(Bucket=BUCKET, Key=key, ContentType="application/json", ServerSideEncryption="AES256",
                  Body=json.dumps({"Name": name, "Definition": original, "by": email, "visual_id": visual_id,
                                   "from": old_kind, "to": to_kind}, ensure_ascii=False).encode())
    updated = _save(aid, name, definition)
    s3.put_object_tagging(Bucket=BUCKET, Key=key, Tagging={"TagSet": [{"Key": "appliedAt", "Value": str(updated.timestamp())}]})
    log(event="visual_changed", by=email, analysis=aid, visual=visual_id, old=old_kind, new=to_kind, backup=key)
    return old_kind


def apply_pending_change(args):
    email, aid = caller.require_editor(args)
    change_id = str(args.get("pending_change_id") or "")
    if len(change_id) != 32:
        raise ToolError("无效的变更编号")
    key = {"jobId": f"pending#{change_id}"}
    try:  # claim it: only the requester, for this analysis, once, before it expires
        item = jobs.update_item(
            Key=key, UpdateExpression="SET #s = :applying",
            ConditionExpression="#s = :pending AND #o = :me AND analysisId = :aid AND expiresAt > :now",
            ExpressionAttributeNames={"#s": "status", "#o": "owner"},
            ExpressionAttributeValues={":applying": "APPLYING", ":pending": "PENDING", ":me": email, ":aid": aid,
                                       ":now": int(time.time())},
            ReturnValues="ALL_NEW")["Attributes"]
    except jobs.meta.client.exceptions.ConditionalCheckFailedException:
        current = jobs.get_item(Key=key).get("Item")
        if not current or current.get("owner") != email or current.get("analysisId") != aid:
            raise ToolError("找不到这个待确认的修改")
        if current.get("status") != "PENDING":
            raise ToolError(f"这个修改已经处理过（{current.get('status')}）")
        raise ToolError("预览已过期，请重新让助手生成预览")
    try:
        old_kind = _apply(email, aid, item["visualId"], item["toKind"])
    except Exception:
        jobs.update_item(Key=key, UpdateExpression="SET #s = :failed", ExpressionAttributeNames={"#s": "status"},
                         ExpressionAttributeValues={":failed": "FAILED"})
        raise
    jobs.update_item(Key=key, UpdateExpression="SET #s = :applied, appliedAt = :t", ExpressionAttributeNames={"#s": "status"},
                     ExpressionAttributeValues={":applied": "APPLIED", ":t": int(time.time())})
    return {"changed": True, "visual_id": item["visualId"], "from": old_kind, "to": item["toKind"],
            "message": "已修改，请手动刷新页面查看。只改了分析；仪表盘要重新发布才会变化。在聊天里说“撤销”可以恢复。"}


def undo_last_visual_change(args):
    email, aid = caller.require_editor(args)
    prefix = f"{_prefix(aid)}active/"
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))
    if not keys:
        raise ToolError("没有可以撤销的图表修改")
    key = keys[-1]
    tags = {t["Key"]: t["Value"] for t in s3.get_object_tagging(Bucket=BUCKET, Key=key)["TagSet"]}
    current = qs.describe_analysis(AwsAccountId=caller.ACCOUNT, AnalysisId=aid)["Analysis"]["LastUpdatedTime"]
    if tags.get("appliedAt") and abs(current.timestamp() - float(tags["appliedAt"])) > 1 and not args.get("force"):
        raise ToolError("这次修改之后分析又被改过（可能是在编辑界面里手动改的）。撤销会把那些修改一起覆盖掉；"
                        "确认要撤销时请明确说“强制撤销”。")
    saved = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    restored_at = _save(aid, saved["Name"], saved["Definition"])
    s3.copy_object(Bucket=BUCKET, Key=key.replace("/active/", "/undone/"), CopySource={"Bucket": BUCKET, "Key": key},
                   ServerSideEncryption="AES256")
    s3.delete_object(Bucket=BUCKET, Key=key)
    if len(keys) > 1:  # the analysis is now as it was right after the previous change, which stays undoable
        s3.put_object_tagging(Bucket=BUCKET, Key=keys[-2],
                              Tagging={"TagSet": [{"Key": "appliedAt", "Value": str(restored_at.timestamp())}]})
    log(event="visual_change_undone", by=email, analysis=aid, visual=saved.get("visual_id"), backup=key)
    return {"undone": True, "visual_id": saved.get("visual_id"), "restored_type": saved.get("from"),
            "message": "已撤销，请手动刷新页面查看。"}


TOOLS = {"describe_analysis": describe_analysis, "preview_visual_change": preview_visual_change,
         "apply_pending_change": apply_pending_change, "undo_last_visual_change": undo_last_visual_change}


def handler(event, context):
    event = dict(event or {})
    name = event.pop("__tool", "")
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    if custom.get("bedrockAgentCoreToolName"):
        name = custom["bedrockAgentCoreToolName"].split("___", 1)[-1]
        if name == "apply_pending_change":  # defence in depth: never reachable through the gateway
            return {"error": "apply_pending_change 只能由聊天页的按钮触发"}
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
        return {"error": f"操作失败：{type(error).__name__}"}
