"""QuickSight analysis assistant on AgentCore Runtime (Strands Agents + bedrock-agentcore SDK).

Invocation payloads (POST /invocations), sent by the web Lambda:
- {"job": {"jobId"}, "prompt", "history", "analysis", "parameters", "user", "auth"}: acknowledge at once, run the
  agent in a background thread that writes heartbeats, progress and the result to the DynamoDB jobs table (writes
  conditional on status = RUNNING). /ping reports HealthyBusy while it runs (add_async_task), so the idle timeout does
  not end the session; the bound is the session's maxLifetime.
- {"cancel": {"jobId"}}: stop that job now (same session, so same microVM).
- {"prompt", ...} without "job": answer synchronously (tests).

Model: OpenAI-compatible endpoint from the Secrets Manager secret LLM_SECRET_ID (cached 60 s):
{"base_url": "https://host/v1", "model": "name", "api_key": "...", "temperature": 0.2, "timeout": 120}.

Tools: everything on the AgentCore Gateway. Calls to this solution's own targets (TRUSTED_TARGETS) carry the user's
signed session token, the analysis id and the current control values, added here in code (WithCaller) - the model
neither sees nor sets them. Other targets (an enterprise MCP such as DataHub or Metabase) get nothing extra.
Local tools: plot_result, preview_visual_change, suggest_controls. Chart numbers always come from tool results.
"""

import copy
import datetime as dt
import json
import os
import re
import secrets
import threading
import time
import uuid

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.hooks import AfterToolCallEvent, BeforeModelCallEvent, BeforeToolCallEvent
from strands.models.openai import OpenAIModel
from strands.tools.mcp import MCPClient
from strands.types.tools import AgentTool

import charts
from prompt import SYSTEM_PROMPT

REGION = os.environ.get("APP_REGION") or os.environ.get("AWS_REGION")
GATEWAY_URL = os.environ["GATEWAY_URL"]
LLM_SECRET_ID = os.environ["LLM_SECRET_ID"]
JOBS_TABLE = os.environ.get("JOBS_TABLE", "")
ANALYSIS_TARGET = os.environ.get("ANALYSIS_TARGET", "analysis")
DATA_TARGET = os.environ.get("DATA_TARGET", "data")
TRUSTED_TARGETS = (f"{ANALYSIS_TARGET}___", f"{DATA_TARGET}___")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "8"))
TOOL_RESULT_LIMIT = 12000
SUMMARY_LIMIT = 14000
HEARTBEAT_SECONDS = 30
CONFIG_TTL = 60
MAX_CHARTS = 4
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
TOOL_LIMIT_MESSAGE = "已达到工具调用上限。请基于已有工具结果直接给出结论，并说明哪些信息还没查到。"

SESSION = boto3.Session(region_name=REGION)
SECRETS = SESSION.client("secretsmanager")
JOBS = SESSION.resource("dynamodb").Table(JOBS_TABLE) if JOBS_TABLE else None
app = BedrockAgentCoreApp()
_config = {"at": 0.0, "value": None}
_config_lock = threading.Lock()
_vm = {}


def vm_instance():
    """Id of this microVM, made on first use: import-time values are baked into the Runtime snapshot."""
    if not _vm:
        _vm["id"] = f"{secrets.token_hex(6)}-{time.time_ns() % 10**9}"
    return _vm["id"]


def log(**fields):
    if fields.get("event") != "start":
        fields["vm"] = vm_instance()
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


class ConfigError(Exception):
    pass


def llm_config():
    with _config_lock:
        if _config["value"] and time.time() - _config["at"] < CONFIG_TTL:
            return _config["value"]
        raw = json.loads(SECRETS.get_secret_value(SecretId=LLM_SECRET_ID)["SecretString"])
        if not str(raw.get("base_url", "")).startswith(("https://", "http://")) or "<" in json.dumps(raw):
            raise ConfigError("管理员尚未配置模型：请在 Secrets Manager 中填写 base_url、model 和 api_key（见使用说明）")
        value = {"base_url": raw["base_url"].rstrip("/"), "model": raw["model"], "api_key": raw.get("api_key") or "none",
                 "temperature": raw.get("temperature"), "timeout": int(raw.get("timeout", 120))}
        _config.update(at=time.time(), value=value)
        return value


class GatewaySigV4(httpx.Auth):
    """Signs every MCP request to the AWS_IAM gateway with the Runtime role's (refreshing) credentials."""

    requires_request_body = True

    def auth_flow(self, request):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("connection", "accept-encoding")}
        signed = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(SESSION.get_credentials().get_frozen_credentials(), "bedrock-agentcore", REGION).add_auth(signed)
        request.headers.update(dict(signed.headers.items()))
        yield request


class Cancelled(Exception):
    pass


class WithCaller(AgentTool):
    """Adds the trusted fields (`_caller`, `_analysis`, `_parameters`) to a gateway tool's input in code.

    They are removed from the schema the model sees and overwritten if the model sends them. Only the Gateway request
    carries them: the conversation history keeps the model's own input, and steps/logs drop them.
    """

    def __init__(self, inner, injected):
        super().__init__()
        self._inner, self._injected = inner, injected

    @property
    def tool_name(self):
        return self._inner.tool_name

    @property
    def tool_spec(self):
        spec = copy.deepcopy(self._inner.tool_spec)
        schema = spec.get("inputSchema", {}).get("json", {})
        for key in self._injected:
            schema.get("properties", {}).pop(key, None)
            if key in schema.get("required", []):
                schema["required"].remove(key)
        return spec

    @property
    def tool_type(self):
        return self._inner.tool_type

    async def stream(self, tool_use, invocation_state, **kwargs):
        signed = {**tool_use, "input": {**(tool_use.get("input") or {}), **self._injected}}
        async for event in self._inner.stream(signed, invocation_state, **kwargs):
            yield event


def _text(result):
    return "\n".join(part.get("text", "") for part in (result or {}).get("content", []) if "text" in part)


def _call(mcp, name, arguments):
    """Call a gateway tool from code and return its JSON result; ValueError on a tool error."""
    result = mcp.call_tool_sync(f"sys-{uuid.uuid4().hex[:8]}", name, arguments)
    text = _text(result)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {"error": text[:300]}
    if result.get("status") != "success" or (isinstance(data, dict) and "error" in data):
        raise ValueError(str(data.get("error") if isinstance(data, dict) else text)[:400])
    return data


def clean_history(history):
    if not isinstance(history, list):
        return []
    return [{"role": t["role"], "content": [{"text": t["content"][:4000]}]}
            for t in history[-10:]
            if isinstance(t, dict) and t.get("role") in ("user", "assistant") and isinstance(t.get("content"), str)]


def clean_parameters(parameters):
    if not isinstance(parameters, dict):
        return {}
    out = {}
    for name, value in list(parameters.items())[:40]:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", name):
            continue
        if isinstance(value, list):
            value = [str(v)[:200] for v in value[:50]]
        elif value is not None:
            value = str(value)[:200]
        out[name] = value
    return out


def local_tools(mcp, injected, summary, state):
    """plot_result / preview_visual_change / suggest_controls: the model decides, the numbers come from tools."""

    def add_chart(spec):
        if len(state["charts"]) >= MAX_CHARTS:
            raise ValueError(f"每次回答最多 {MAX_CHARTS} 张图")
        state["charts"].append(spec)

    @tool
    def plot_result(result_id: str, x: str, y: list[str], series_by: str = "", chart_type: str = "line",
                    title: str = "") -> str:
        """Draw a chart in the chat panel from the rows of an earlier tool result. The numbers are read from that
        result by the system, so the chart always matches the data.

        Args:
            result_id: The result_id printed after a tool result that returned rows (e.g. r1).
            x: Field for the x axis (a time bucket or a category).
            y: One or more numeric fields to plot.
            series_by: Optional field whose values become separate lines or bar groups (only the first y is used).
            chart_type: line for trends over time, bar for comparisons.
            title: Short Chinese title.
        """
        rows = state["results"].get(result_id)
        if rows is None:
            return json.dumps({"chart_added": False, "error": f"没有结果 {result_id}；可用：{', '.join(state['results'])}"},
                              ensure_ascii=False)
        try:
            labels, series, notes = charts.from_rows(rows, x, y, series_by or None)
            add_chart(charts.chart(chart_type, title or "、".join(y), labels, series, notes))
        except ValueError as error:
            return json.dumps({"chart_added": False, "error": str(error)}, ensure_ascii=False)
        return json.dumps({"chart_added": True, "points": len(labels), "series": [s["name"] for s in series],
                           "notes": notes}, ensure_ascii=False)

    @tool
    def preview_visual_change(visual_id: str, chart_type: str) -> str:
        """Prepare (do not apply) switching one visual of the analysis between line chart and bar chart. The chat
        panel shows a preview with the visual's data under the current controls, plus an 应用到分析 button; the analysis
        changes only when the user presses it. Never say it is changed.

        Args:
            visual_id: visual_id from the analysis summary.
            chart_type: bar (柱状图) or line (折线图).
        """
        try:
            change = _call(mcp, f"{ANALYSIS_TARGET}___preview_visual_change",
                           {"visual_id": visual_id, "chart_type": chart_type, **injected})
        except ValueError as error:
            return json.dumps({"preview_ready": False, "error": str(error)}, ensure_ascii=False)
        kind = "bar" if change["to"] == "BarChartVisual" else "line"
        to_name = "柱状图" if kind == "bar" else "折线图"
        title = change.get("title") or visual_id
        for name, value in injected["_parameters"].items():
            title = title.replace(f"<<${name}>>", str(value))
        pending = {"id": change["pending_change_id"], "expires_at": change["expires_at"], "visual_id": visual_id,
                   "title": title, "to": to_name}
        preview, notes = change.get("preview") or {}, []
        labels, series = [], []
        if "query" in preview and preview["query"].get("dataset_id"):
            query = preview["query"]
            notes += preview.get("notes", [])
            try:
                data = _call(mcp, f"{DATA_TARGET}___query_dataset", {**query, **injected})
                rows, columns = data.get("rows", []), data.get("columns", [])
                x = next((c["name"] for c in columns if c["role"] in ("time", "dimension")), None)
                metrics = [c["name"] for c in columns if c["role"] == "metric"]
                dims = [c["name"] for c in columns if c["role"] == "dimension" and c["name"] != x]
                labels, series, more = charts.from_rows(rows, x, metrics, dims[0] if dims else None)
                notes += more
                if data.get("truncated"):
                    notes.append("数据超过 500 行，预览不完整")
            except ValueError as error:
                notes.append(f"无法取数画预览：{error}")
        else:
            notes.append(f"无法画预览：{preview.get('unsupported', '这个图表的数据口径无法在聊天里复现')}。仍可直接应用。")
        add_chart(charts.chart(kind if series else "none", f"预览：{title}（{to_name}）", labels, series, notes,
                               pending_change=pending))
        return json.dumps({"preview_ready": True, "visual": title, "to": to_name, "points": len(labels), "notes": notes,
                           "message": "预览已显示在回答下方；分析尚未修改，用户点“应用到分析”后才会修改。"},
                          ensure_ascii=False)

    @tool
    def suggest_controls(values: dict[str, str]) -> str:
        """Tell the user which values to pick in the controls above the dashboard (you cannot change them).
        Values are checked against the parameter's control and, when the control lists dataset values, the data.

        Args:
            values: Parameter name -> value to select, e.g. {"pStation": "ST001"}. Parameter names come from the
                analysis summary.
        """
        parameters = summary.get("parameters", {}) if isinstance(summary, dict) else {}
        datasets = summary.get("datasets", {}) if isinstance(summary, dict) else {}
        out, problems = [], []
        for name, value in (values or {}).items():
            info = parameters.get(name)
            if info is None:
                problems.append(f"没有参数 {name}")
                continue
            if info.get("choices") and value not in info["choices"]:
                problems.append(f"{info.get('control') or name} 没有选项 {value}")
                continue
            source = info.get("values_from")
            if source and datasets.get(source["dataset"]):
                try:
                    found = _call(mcp, f"{DATA_TARGET}___distinct_values",
                                  {"dataset_id": datasets[source["dataset"]], "column": source["column"],
                                   "search": str(value), **injected})
                    if str(value) not in [str(v) for v in found.get("values", [])]:
                        problems.append(f"{info.get('control') or name} 里没有 {value}；相近的值："
                                        f"{', '.join(map(str, found.get('values', [])[:8])) or '无'}")
                        continue
                except ValueError:
                    pass  # the data tool may not support this dataset; still show the suggestion
            out.append({"parameter": name, "control": info.get("control") or name, "value": str(value),
                        "current": injected["_parameters"].get(name)})
        if out:
            state["controls"] = out
        return json.dumps({"controls": out, "problems": problems}, ensure_ascii=False)

    return [plot_result, preview_visual_change, suggest_controls]


def run_agent(payload, cancel_signal=None):
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
        raise ValueError("prompt must be a non-empty string of at most 2000 characters")
    started = time.time()
    analysis = str(payload.get("analysis") or "")
    parameters = clean_parameters(payload.get("parameters"))
    token = payload.get("auth") if isinstance(payload.get("auth"), str) else ""
    injected = {"_caller": token, "_analysis": analysis, "_parameters": parameters}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    steps, state = [], {"model_calls": 0, "charts": [], "controls": None, "results": {}}

    def before_model(_event: BeforeModelCallEvent):
        state["model_calls"] += 1

    def before_tool(event: BeforeToolCallEvent):
        if state["model_calls"] > MAX_TURNS:
            event.cancel_tool = TOOL_LIMIT_MESSAGE

    def after_tool(event: AfterToolCallEvent):
        name = event.tool_use.get("name", "")
        result = event.result or {}
        text = _text(result)
        ok = result.get("status") == "success" and event.cancel_message is None
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and "error" in parsed:
            ok = False
        rows = charts.find_rows(parsed) if ok and parsed is not None else None
        content = text if len(text) <= TOOL_RESULT_LIMIT else text[:TOOL_RESULT_LIMIT] + "…（已截断）"
        if rows:
            result_id = f"r{len(state['results']) + 1}"
            state["results"][result_id] = rows
            content += f"\n[result_id: {result_id}] 可以用 plot_result 画这份结果"
        if content != text:
            event.result = {**result, "content": [{"text": content}]}
        arguments = {k: v for k, v in (event.tool_use.get("input") or {}).items() if not k.startswith("_")}
        # This solution's tools show their short name; enterprise MCP tools keep "<target>/<tool>" to show the source.
        shown = name.split("___", 1)[-1] if name.startswith(TRUSTED_TARGETS) or "___" not in name else name.replace("___", "/", 1)
        steps.append({"tool": shown, "arguments": arguments, "ok": ok})
        log(event="tool_call", tool=name, ok=ok, chars=len(text), rows=len(rows) if rows else 0)

    cfg = llm_config()
    params = {"temperature": cfg["temperature"]} if cfg["temperature"] is not None else None
    model = OpenAIModel(client_args={"base_url": cfg["base_url"], "api_key": cfg["api_key"], "timeout": cfg["timeout"]},
                        model_id=cfg["model"], params=params)
    with MCPClient(url=GATEWAY_URL, auth_provider=GatewaySigV4()) as mcp:
        gateway_tools = mcp.list_tools_sync()
        try:
            summary = _call(mcp, f"{ANALYSIS_TARGET}___describe_analysis", injected) if analysis else \
                {"error": "聊天窗口没有绑定分析"}
        except ValueError as error:
            summary = {"error": str(error)}
        replaced = {f"{ANALYSIS_TARGET}___preview_visual_change"}
        tools = [WithCaller(t, injected) if t.tool_name.startswith(TRUSTED_TARGETS) else t
                 for t in gateway_tools if t.tool_name not in replaced]
        tools += local_tools(mcp, injected, summary, state)
        controls = {name: {"control": (summary.get("parameters", {}).get(name) or {}).get("control"), "value": value}
                    for name, value in parameters.items()} if isinstance(summary, dict) else parameters
        today = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8)))
        system = (f"{SYSTEM_PROMPT}\n\n今天是 {today:%Y-%m-%d}（北京时间）。\n当前控件值：{json.dumps(controls, ensure_ascii=False)}\n"
                  f"分析结构：{json.dumps(summary, ensure_ascii=False, default=str)[:SUMMARY_LIMIT]}")
        agent = Agent(model=model, tools=tools, system_prompt=system, messages=clean_history(payload.get("history")),
                      callback_handler=None)
        agent.hooks.add_callback(BeforeModelCallEvent, before_model)
        agent.hooks.add_callback(BeforeToolCallEvent, before_tool)
        agent.hooks.add_callback(AfterToolCallEvent, after_tool)
        result = agent(prompt.strip(), cancel_signal=cancel_signal)
    if result.stop_reason == "cancelled":
        raise Cancelled()
    answer = "".join(part.get("text", "") for part in result.message.get("content", []) if "text" in part).strip()
    elapsed = int((time.time() - started) * 1000)
    log(event="answered", user=user.get("sub"), analysis=analysis, steps=len(steps), charts=len(state["charts"]),
        elapsed_ms=elapsed)
    out = {"answer": answer, "steps": steps, "model": cfg["model"], "elapsed_ms": elapsed}
    if state["charts"]:
        out["charts"] = state["charts"]
    if state["controls"]:
        out["controls"] = state["controls"]
    return out


class Job:
    """One background job. Every write is conditional on status = RUNNING: a cancelled job is never overwritten."""

    active = {}
    lock = threading.Lock()

    def __init__(self, job, payload):
        self.id = job["jobId"]
        self.payload = payload
        self.started = time.time()
        self.stop = threading.Event()  # also Strands' cancel_signal
        self.progress = "已开始"

    def _write(self, expression, values, names=None):
        try:
            JOBS.update_item(Key={"jobId": self.id}, UpdateExpression=expression, ConditionExpression="#s = :running",
                             ExpressionAttributeNames={"#s": "status", **(names or {})},
                             ExpressionAttributeValues={":running": "RUNNING", **values})
            return True
        except JOBS.meta.client.exceptions.ConditionalCheckFailedException:
            self.stop.set()
            return False

    def heartbeat(self):
        self._write("SET heartbeatAt = :h, progress = :p", {":h": int(time.time()), ":p": self.progress})

    def _heartbeats(self):
        while not self.stop.wait(HEARTBEAT_SECONDS):
            self.heartbeat()

    def run(self):
        task = app.add_async_task("job", {"jobId": self.id})
        with Job.lock:
            Job.active[self.id] = self
        log(event="job_started", job=self.id)
        self.heartbeat()
        threading.Thread(target=self._heartbeats, daemon=True).start()
        try:
            result = run_agent(self.payload, cancel_signal=self.stop)
            result["totalMs"] = int((time.time() - self.started) * 1000)
            ok = self._write("SET #s = :done, #r = :r, heartbeatAt = :h",
                             {":done": "SUCCEEDED", ":r": json.dumps(result, ensure_ascii=False), ":h": int(time.time())},
                             {"#r": "result"})
            log(event="job_finished", job=self.id, written=ok, total_ms=result["totalMs"])
        except Cancelled:
            log(event="job_cancelled", job=self.id, after_s=int(time.time() - self.started))
        except ConfigError as error:
            self._write("SET #s = :failed, #e = :e", {":failed": "FAILED", ":e": str(error)}, {"#e": "error"})
        except Exception as error:  # noqa: BLE001 - report, never crash the server
            log(event="job_failed", job=self.id, error=repr(error)[:800])
            self._write("SET #s = :failed, #e = :e", {":failed": "FAILED", ":e": str(error)[:500]}, {"#e": "error"})
        finally:
            self.stop.set()
            with Job.lock:
                Job.active.pop(self.id, None)
            app.complete_async_task(task)


def start_job(payload):
    job = payload.get("job") or {}
    if JOBS is None:
        raise ValueError("JOBS_TABLE is not configured")
    if not JOB_ID.match(str(job.get("jobId", ""))):
        raise ValueError("job.jobId must be 32 hex characters")
    runner = Job(job, {key: value for key, value in payload.items() if key != "job"})
    threading.Thread(target=runner.run, daemon=True).start()
    return {"accepted": True, "jobId": runner.id}


def cancel_job(payload):
    job_id = str((payload.get("cancel") or {}).get("jobId", ""))
    if not JOB_ID.match(job_id):
        raise ValueError("cancel.jobId must be 32 hex characters")
    with Job.lock:
        runner = Job.active.get(job_id)
    if runner:
        runner.stop.set()
    log(event="job_cancel_signal", job=job_id, found=bool(runner))
    return {"cancelled": bool(runner), "jobId": job_id}


@app.entrypoint
def invoke(payload, context=None):
    try:
        if "cancel" in payload:
            return cancel_job(payload)
        return start_job(payload) if "job" in payload else run_agent(payload)
    except (ValueError, ConfigError) as error:
        return {"error": str(error)}


if __name__ == "__main__":
    log(event="start", region=REGION, gateway=GATEWAY_URL)
    app.run(host="0.0.0.0", port=8080)  # the SDK only binds 0.0.0.0 by itself inside Docker
