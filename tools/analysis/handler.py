"""Gateway Lambda target: understand and edit the QuickSight analysis the chat panel is embedded in.

Tools (the Runtime adds `_caller`, `_analysis` and `_parameters` - the current control values - to every call):
- describe_analysis          sheets, visuals (what each one plots), parameters/controls, parameter-driven filters,
                             datasets. Anyone who may view the analysis or a dashboard published from it.
- preview_analysis_change    validate one edit (tools/common/edits.py: chart type, title, measures, category, color,
                             sort, add or delete a visual) on a copy, record it as a pending change (10 minutes) owned
                             by the caller, and return the query that reproduces the edited visual's data.
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
import edits
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


def dataset_columns(definition):
    """{dataset identifier: {column: QuickSight type}} including the analysis' calculated fields (type CALCULATED)."""
    out = {}
    for declaration in definition.get("DataSetIdentifierDeclarations", []):
        dataset_id = declaration["DataSetArn"].rsplit("/", 1)[-1]
        try:
            description = caller.cached(f"ds:{dataset_id}", lambda d=dataset_id: qs.describe_data_set(
                AwsAccountId=caller.ACCOUNT, DataSetId=d)["DataSet"])
            out[declaration["Identifier"]] = {c["Name"]: c["Type"] for c in description.get("OutputColumns", [])}
        except Exception as error:  # noqa: BLE001 - permissions on a dataset should not break the summary
            log(event="describe_data_set_failed", dataset=dataset_id, error=repr(error)[:200])
            out[declaration["Identifier"]] = {}
    for field in definition.get("CalculatedFields", []):
        out.setdefault(field.get("DataSetIdentifier"), {})[field["Name"]] = "CALCULATED"
    return out


def describe_analysis(args):
    _email, aid = caller.require_viewer(args)
    name, definition = _analysis(aid)
    summary = defs.strip_for_model(definition)
    columns = {ds: dict(list(cols.items())[:150]) for ds, cols in dataset_columns(definition).items()}
    return {"analysis_id": aid, "name": name, **summary, "columns": columns,
            "note": "visual_id 用于 preview_analysis_change；datasets 的值是 dataset_id，用于 query_dataset；"
                    "columns 按数据集标识列出可用于编辑图表的列和类型。"}


EDIT_KEYS = ("operation", "visual_id", "chart_type", "title", "measures", "category", "color", "sort", "dataset", "sheet_id")


def preview_analysis_change(args):
    email, aid = caller.require_editor(args)
    edit = {k: args[k] for k in EDIT_KEYS if args.get(k) not in (None, "", [])}
    if "color" in args and args.get("color") is None:
        edit["color"] = None
    _name, definition = _analysis(aid)
    columns = dataset_columns(definition)
    draft = json.loads(json.dumps(definition, default=str))
    try:
        visual_id, summary = edits.apply_edit(draft, edit, columns)
    except edits.EditError as error:
        raise ToolError(str(error)) from error
    parameters = args.get("_parameters") if isinstance(args.get("_parameters"), dict) else {}
    preview = {"unsupported": "图表将被删除"} if visual_id is None else defs.preview_query(draft, visual_id, parameters)
    title, kind = "", None
    if visual_id is not None:
        _s, _v, kind, body = next(x for x in defs.visuals(draft) if x[3].get("VisualId") == visual_id)
        title = defs.text(body.get("Title")) or visual_id
    ids = defs.dataset_ids(draft)
    if "query" in preview:
        preview["query"]["dataset_id"] = ids.get(preview["query"].pop("dataset"))
    change_id, now = uuid.uuid4().hex, int(time.time())
    jobs.put_item(Item={"jobId": f"pending#{change_id}", "status": "PENDING", "owner": email, "analysisId": aid,
                        "edit": json.dumps(edit, ensure_ascii=False), "summary": summary, "createdAt": now,
                        "expiresAt": now + PENDING_SECONDS})
    log(event="analysis_change_previewed", by=email, analysis=aid, edit=edit.get("operation"), change=change_id)
    return {"pending_change_id": change_id, "expires_at": now + PENDING_SECONDS, "summary": summary,
            "visual_id": visual_id, "title": title, "kind": kind,
            "preview": {k: v for k, v in preview.items() if k != "summary"},
            "message": "尚未修改分析。聊天框会显示预览，用户点“应用到分析”后才会修改。"}


def _apply(email, aid, edit):
    """Back up the current definition, then apply the edit to it (not to the preview's copy)."""
    name, definition = _analysis(aid)
    original = json.loads(json.dumps(definition, default=str))
    try:
        visual_id, summary = edits.apply_edit(definition, edit, dataset_columns(definition))
    except edits.EditError as error:
        raise ToolError(f"分析在预览之后有变化，无法应用：{error}") from error
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    key = f"{_prefix(aid)}active/{stamp}.json"
    s3.put_object(Bucket=BUCKET, Key=key, ContentType="application/json", ServerSideEncryption="AES256",
                  Body=json.dumps({"Name": name, "Definition": original, "by": email, "visual_id": visual_id,
                                   "summary": summary, "edit": edit}, ensure_ascii=False).encode())
    updated = _save(aid, name, definition)
    s3.put_object_tagging(Bucket=BUCKET, Key=key, Tagging={"TagSet": [{"Key": "appliedAt", "Value": str(updated.timestamp())}]})
    log(event="analysis_changed", by=email, analysis=aid, edit=edit.get("operation"), visual=visual_id, backup=key)
    return visual_id, summary


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
        visual_id, summary = _apply(email, aid, json.loads(item["edit"]))
    except Exception:
        jobs.update_item(Key=key, UpdateExpression="SET #s = :failed", ExpressionAttributeNames={"#s": "status"},
                         ExpressionAttributeValues={":failed": "FAILED"})
        raise
    jobs.update_item(Key=key, UpdateExpression="SET #s = :applied, appliedAt = :t", ExpressionAttributeNames={"#s": "status"},
                     ExpressionAttributeValues={":applied": "APPLIED", ":t": int(time.time())})
    return {"changed": True, "visual_id": visual_id, "summary": summary,
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
    log(event="analysis_change_undone", by=email, analysis=aid, visual=saved.get("visual_id"), backup=key)
    return {"undone": True, "undid": saved.get("summary") or f"{saved.get('visual_id')} 的修改",
            "message": "已撤销，请手动刷新页面查看。"}


TOOLS = {"describe_analysis": describe_analysis, "preview_analysis_change": preview_analysis_change,
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
