"""API Gateway HTTP API Lambda for the chat panel embedded in QuickSight.

Routes:
  GET  /                       chat page (index.html)
  GET  /static/<vendor file>   ECharts / marked / DOMPurify, versioned file names, cached for a year
  GET  /auth/login             start IAM Identity Center SAML login (popup)
  POST /saml/acs               SAML assertion consumer; hands a signed session token to the iframe
  GET  /me                     who is signed in
  POST /chat                   submit a question: stored as a job, sent to the Runtime, which acknowledges at once
  GET  /jobs/{id}              job status, progress and result (STALLED when heartbeats stop)
  POST /jobs/{id}/cancel       cancel a running job
  POST /changes/{id}/apply     the 应用到分析 button: apply a previewed analysis change as this user
"""

import base64
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import uuid

import boto3
from botocore.config import Config

import auth

RUNTIME_ARN = os.environ["RUNTIME_ARN"]
RUNTIME_VERSION = os.environ.get("RUNTIME_VERSION", "0")
JOBS_TABLE = os.environ["JOBS_TABLE"]
ANALYSIS_TOOLS_FN = os.environ["ANALYSIS_TOOLS_FN"]
# The ACS URL registered in Identity Center. Default: this API's own address (known only per request, which avoids a
# deployment cycle between the Lambda and the HTTP API); set SAML_ACS_URL when serving through a custom domain.
ACS_OVERRIDE = os.environ.get("SAML_ACS_URL", "")
FRAME_ANCESTORS = os.environ.get("FRAME_ANCESTORS", "https://*.quicksight.amazonaws.cn https://*.amazonaws.cn")
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{33,100}$")
ANALYSIS_ID = re.compile(r"^[\w-]{1,512}$")
NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
JOB_TTL_SECONDS = 24 * 3600
STALE_SECONDS = 150  # the Runtime heartbeats every 30 s
# Third-party browser scripts vendored under web/vendor (see vendor/README.md); the page needs no CDN.
STATIC_FILES = ("echarts-6.1.0.min.js", "marked-18.1.1.umd.js", "purify-3.4.17.min.js")

jobs = boto3.resource("dynamodb").Table(JOBS_TABLE)
lambda_client = boto3.client("lambda")
# The Runtime only acknowledges, so a short read timeout is enough (it covers a session cold start).
agentcore = boto3.client("bedrock-agentcore", config=Config(read_timeout=25, connect_timeout=5, retries={"max_attempts": 1}))

HERE = os.path.dirname(os.path.abspath(__file__))
# Optional example questions for the chat page: JSON [{"group", "tip"?, "questions": [...]}] (config.json "examples").
EXAMPLES_JSON = os.environ.get("EXAMPLES_JSON", "") or "null"
with open(os.path.join(HERE, "index.html"), encoding="utf-8") as page:
    INDEX_HTML = page.read().replace("__EXAMPLES__", json.dumps(json.loads(EXAMPLES_JSON), ensure_ascii=True).replace("<", "\\u003c"))
STATIC = {}
for _name in STATIC_FILES:
    with open(os.path.join(HERE, "vendor", _name), encoding="utf-8") as script:
        STATIC[f"/static/{_name}"] = script.read()
STATIC_HEADERS = {"Content-Type": "application/javascript; charset=utf-8",
                  "Cache-Control": "public, max-age=31536000, immutable",
                  "X-Content-Type-Options": "nosniff"}

SECURITY_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
PAGE_HEADERS = {**SECURITY_HEADERS, "Content-Type": "text/html; charset=utf-8",
                "Content-Security-Policy": f"frame-ancestors 'self' {FRAME_ANCESTORS}"}
POPUP_HEADERS = {**SECURITY_HEADERS, "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": "frame-ancestors 'none'"}


def respond(status, body, headers=None):
    return {"statusCode": status,
            "headers": headers or {**SECURITY_HEADERS, "Content-Type": "application/json; charset=utf-8"},
            "body": body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)}


def raw_body(event):
    body = event.get("body") or ""
    return base64.b64decode(body).decode("utf-8") if event.get("isBase64Encoded") else body


def request_json(event):
    return json.loads(raw_body(event) or "{}")


def bearer(event):
    header = (event.get("headers") or {}).get("authorization", "")
    return header[7:].strip() if header[:7].lower() == "bearer " else ""


def current_user(event):
    token = bearer(event)
    return auth.verify_session(token) if token else None


def acs_url(event):
    return ACS_OVERRIDE or f"https://{event['requestContext']['domainName']}/saml/acs"


def origin(event):
    return "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(acs_url(event)))


def popup_page(title, message, payload=None, target_origin=""):
    """Login popup page; hands the session to the opener iframe on this exact origin only."""
    script = ""
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=True).replace("<", "\\u003c")
        script = ("<script>(function(){var d=" + data + ";var o=" + json.dumps(target_origin) + ";"
                  "if(window.opener){window.opener.postMessage(d,o);setTimeout(function(){window.close()},400);}"
                  "else{document.getElementById('m').textContent='登录成功，但无法回传到聊天窗口（弹窗与 QuickSight 页面的连接已断开）。请关闭此窗口，回到 QuickSight 重新点击登录。';}"
                  "})();</script>")
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>' + html.escape(title) + "</title>"
            '<style>body{font:15px/1.6 -apple-system,"PingFang SC",sans-serif;padding:32px;color:#16232b}</style></head>'
            '<body><h3>' + html.escape(title) + '</h3><p id="m">' + html.escape(message) + "</p>" + script + "</body></html>")


def login_start(event):
    nonce = (event.get("queryStringParameters") or {}).get("nonce", "")
    if not NONCE.match(nonce):
        return respond(400, popup_page("无法登录", "缺少或无效的 nonce，请从聊天窗口点击登录。"), POPUP_HEADERS)
    try:
        location = auth.login_redirect(jobs, nonce, acs_url(event))
    except auth.NotConfigured as error:
        return respond(503, popup_page("尚未配置登录", f"{error}。请联系管理员按部署手册完成 Identity Center 配置。"), POPUP_HEADERS)
    return {"statusCode": 302, "headers": {**SECURITY_HEADERS, "Location": location}, "body": ""}


def login_finish(event):
    form = urllib.parse.parse_qs(raw_body(event), keep_blank_values=True)
    try:
        token, payload, nonce, _attributes = auth.complete_login(jobs, (form.get("SAMLResponse") or [""])[0],
                                                                 (form.get("RelayState") or [""])[0], acs_url(event))
    except auth.AuthError as error:
        print(json.dumps({"auth": "rejected", "reason": str(error)[:300]}, ensure_ascii=False))
        return respond(403, popup_page("登录失败", str(error)), POPUP_HEADERS)
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"auth": "error", "error": repr(error)[:500]}, ensure_ascii=False))
        return respond(500, popup_page("登录失败", "签名或断言校验出错，请稍后重试。"), POPUP_HEADERS)
    print(json.dumps({"auth": "ok", "sub": payload["sub"], "exp": payload["exp"]}, ensure_ascii=False))
    message = {"type": "qsca-auth", "nonce": nonce, "token": token,
               "user": {"sub": payload["sub"], "name": payload["name"], "exp": payload["exp"]}}
    return respond(200, popup_page("登录成功", "正在返回聊天窗口…", message, origin(event)), POPUP_HEADERS)


def submit(event, user):
    try:
        body = request_json(event)
    except ValueError:
        return respond(400, {"error": "invalid JSON"})
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
        return respond(400, {"error": "prompt must be 1-2000 characters"})
    analysis = body.get("analysis")
    if not isinstance(analysis, str) or not ANALYSIS_ID.match(analysis):
        return respond(400, {"error": "聊天窗口没有绑定分析（地址缺少 analysis 参数），请联系管理员重新接入"})
    session_id = body.get("sessionId")
    if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
        session_id = f"qs-{uuid.uuid4().hex}"
    # A Runtime session keeps the code version it started with and the browser reuses its id for hours, so bind the
    # session to user, analysis and Runtime version: a redeploy, user switch or another analysis starts fresh.
    runtime_session = "qs-" + hashlib.sha256(
        f"{user['sub']}|{analysis}|{session_id}|{RUNTIME_VERSION}".encode()).hexdigest()[:48]
    job_id = uuid.uuid4().hex
    now = int(time.time())
    jobs.put_item(Item={"jobId": job_id, "status": "RUNNING", "owner": user["sub"], "createdAt": now, "heartbeatAt": now,
                        "runtimeSession": runtime_session, "expiresAt": now + JOB_TTL_SECONDS})
    # "auth" is the user's verified session token: the Runtime never shows it to the model and only attaches it to
    # this solution's own tools, which verify it and check the user's QuickSight permissions.
    payload = {"job": {"jobId": job_id}, "prompt": prompt, "history": body.get("history") or [],
               "analysis": analysis, "parameters": body.get("parameters") or {},
               "user": {"sub": user["sub"], "name": user.get("name")}, "auth": bearer(event)}
    try:
        response = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=runtime_session, contentType="application/json",
            accept="application/json", payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        ack = json.loads(response["response"].read().decode("utf-8"))
        if not ack.get("accepted"):
            raise RuntimeError(ack.get("error") or "runtime did not accept the job")
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"job": job_id, "error": repr(error)[:800]}))
        jobs.update_item(Key={"jobId": job_id}, UpdateExpression="SET #s = :s, #e = :e",
                         ExpressionAttributeNames={"#s": "status", "#e": "error"},
                         ExpressionAttributeValues={":s": "FAILED", ":e": f"提交到 Runtime 失败: {str(error)[:300]}"})
        return respond(502, {"error": "runtime did not accept the job", "jobId": job_id})
    print(json.dumps({"job": job_id, "user": user["sub"], "event": "submitted", "analysis": analysis,
                      "runtimeVersion": RUNTIME_VERSION}, ensure_ascii=False))
    return respond(202, {"jobId": job_id, "sessionId": session_id, "status": "RUNNING"})


def owned_job(job_id, user):
    if not HEX32.match(job_id):
        return None
    item = jobs.get_item(Key={"jobId": job_id}, ConsistentRead=True).get("Item")
    return item if item and item.get("owner") == user["sub"] else None


def status(job_id, user):
    item = owned_job(job_id, user)
    if not item:
        return respond(404, {"error": "job not found"})
    result = {"jobId": job_id, "status": item["status"], "progress": item.get("progress"),
              "heartbeatAt": int(item.get("heartbeatAt") or 0), "createdAt": int(item["createdAt"])}
    if item["status"] == "RUNNING" and time.time() - result["heartbeatAt"] > STALE_SECONDS:
        result["status"] = "STALLED"
        result["error"] = f"任务已 {int(time.time() - result['heartbeatAt'])} 秒没有心跳，Runtime 会话可能已结束"
    if item.get("result"):
        result.update(json.loads(item["result"]))
    if item.get("error"):
        result["error"] = item["error"]
    return respond(200, result)


def cancel(job_id, user):
    item = owned_job(job_id, user)
    if not item:
        return respond(404, {"error": "job not found"})
    try:
        jobs.update_item(Key={"jobId": job_id}, UpdateExpression="SET #s = :c, cancelledAt = :t",
                         ConditionExpression="#s = :running", ExpressionAttributeNames={"#s": "status"},
                         ExpressionAttributeValues={":c": "CANCELLED", ":t": int(time.time()), ":running": "RUNNING"})
    except jobs.meta.client.exceptions.ConditionalCheckFailedException:
        return respond(409, {"error": "job is not running", "status": item["status"]})
    signalled = False
    try:  # same session -> same microVM: the job thread stops now; otherwise at its next heartbeat (<= 30 s)
        response = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=item["runtimeSession"], contentType="application/json",
            accept="application/json", payload=json.dumps({"cancel": {"jobId": job_id}}).encode("utf-8"))
        signalled = bool(json.loads(response["response"].read().decode("utf-8")).get("cancelled"))
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"job": job_id, "cancelSignal": repr(error)[:500]}))
    print(json.dumps({"job": job_id, "user": user["sub"], "event": "cancelled", "signalled": signalled}))
    return respond(200, {"jobId": job_id, "status": "CANCELLED", "signalled": signalled})


def apply_change(change_id, event, user):
    """The 应用到分析 button: the analysis-tools Lambda applies the pending change as this user; no model involved."""
    try:
        body = request_json(event)
    except ValueError:
        body = {}
    analysis = body.get("analysis")
    if not HEX32.match(change_id) or not isinstance(analysis, str) or not ANALYSIS_ID.match(analysis):
        return respond(400, {"error": "invalid change"})
    response = lambda_client.invoke(FunctionName=ANALYSIS_TOOLS_FN, Payload=json.dumps(
        {"__tool": "apply_pending_change", "pending_change_id": change_id, "_caller": bearer(event),
         "_analysis": analysis}).encode("utf-8"))
    result = json.loads(response["Payload"].read() or b"{}")
    print(json.dumps({"change": change_id, "user": user["sub"], "event": "apply", "ok": "error" not in result,
                      "error": result.get("error")}, ensure_ascii=False))
    return respond(409 if "error" in result else 200, result)


def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method", ""), event.get("rawPath", "/")
    if method == "GET" and path in ("/", "/index.html"):
        return respond(200, INDEX_HTML, PAGE_HEADERS)
    if method == "GET" and path in STATIC:
        return respond(200, STATIC[path], STATIC_HEADERS)
    if method == "GET" and path == "/health":
        return respond(200, {"ok": True})
    if method == "GET" and path == "/auth/login":
        return login_start(event)
    if method == "POST" and path == "/saml/acs":
        return login_finish(event)
    user = current_user(event)
    if not user:
        return respond(401, {"error": "unauthorized", "login": True})
    if method == "GET" and path == "/me":
        return respond(200, {"sub": user["sub"], "name": user.get("name"), "exp": user["exp"]})
    if method == "POST" and path == "/chat":
        return submit(event, user)
    if method == "POST" and re.fullmatch(r"/changes/[0-9a-f]{32}/apply", path):
        return apply_change(path.split("/")[2], event, user)
    if method == "POST" and re.fullmatch(r"/jobs/[0-9a-f]{32}/cancel", path):
        return cancel(path.split("/")[2], user)
    if method == "GET" and re.fullmatch(r"/jobs/[0-9a-f]{32}", path):
        return status(path.rsplit("/", 1)[-1], user)
    return respond(404, {"error": "not found"})
