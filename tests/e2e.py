#!/usr/bin/env python3
"""End-to-end checks against a deployed stack (admin only: reads the session key to sign test tokens).

  python3 tests/e2e.py --profile PROFILE --analysis ANALYSIS_ID --editor EMAIL [--other-editor EMAIL]
      [--visual VISUAL_ID] [--dataset DATASET_ID --control-column COLUMN] [--only NAME]

--visual (a line or bar chart) enables the preview/apply/undo test; --dataset with --control-column (a column a
control lists values from) enables the control-suggestion test. The chart question assumes nothing about the data.

Checks: auth rejections; a data question answered with a chart; pandas calculations in the sandbox; control
suggestions; preview -> apply (owner only, once) -> undo; add / retitle / delete a visual then undo all three; fast
cancel; refusal for an unknown user. Edits are always undone, leaving the analysis as it was.
"""

import argparse
import importlib.machinery
import importlib.util
import json
import pathlib
import sys
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("qsca", str(ROOT / "qsca"))
spec = importlib.util.spec_from_loader("qsca", loader)
qsca = importlib.util.module_from_spec(spec)
loader.exec_module(qsca)

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    print(f"   {'PASS' if condition else 'FAIL'} {name}{': ' + str(detail)[:220] if detail else ''}")
    return condition


class Client:
    def __init__(self, args):
        self.config = qsca.load_config(args)
        self.session = qsca.session(args, self.config)
        self.out = qsca.outputs(args, self.config)
        self.base = self.out["ChatUrl"].rstrip("/")
        self.analysis = args.analysis

    def token(self, email):
        return qsca.mint_token(self.session, self.out["SessionSecretName"], email)

    def ask(self, token, prompt, parameters=None, session=None, wait=True):
        status, job = qsca.http("POST", f"{self.base}/chat", token, {
            "prompt": prompt, "analysis": self.analysis, "parameters": parameters or {},
            "sessionId": session or f"qs-e2e-{uuid.uuid4().hex}"})
        if status != 202:
            return {"status": f"HTTP {status}", "error": job}
        if not wait:
            return job
        for _ in range(150):
            time.sleep(3)
            _status, result = qsca.http("GET", f"{self.base}/jobs/{job['jobId']}", token)
            if result.get("status") != "RUNNING":
                return result
        return {"status": "TIMEOUT"}

    def definition(self):
        qs = self.session.client("quicksight")
        account = self.session.client("sts").get_caller_identity()["Account"]
        return qs.describe_analysis_definition(AwsAccountId=account, AnalysisId=self.analysis)["Definition"]

    def visuals(self):
        """[(visual_id, type, title)] of the analysis."""
        out = []
        for sheet in self.definition().get("Sheets", []):
            for visual in sheet.get("Visuals", []):
                kind, body = next(iter(visual.items()))
                title = ((body.get("Title") or {}).get("FormatText") or {}).get("PlainText", "")
                out.append((body.get("VisualId"), kind, title))
        return out

    def visual_type(self, visual_id):
        qs = self.session.client("quicksight")
        account = self.session.client("sts").get_caller_identity()["Account"]
        for _ in range(5):
            definition = qs.describe_analysis_definition(AwsAccountId=account, AnalysisId=self.analysis)["Definition"]
            for sheet in definition.get("Sheets", []):
                for visual in sheet.get("Visuals", []):
                    kind, body = next(iter(visual.items()))
                    if body.get("VisualId") == visual_id:
                        return kind
            time.sleep(2)
        return None


def tools(result):
    return [(s["tool"], s["ok"]) for s in result.get("steps", [])]


def test_auth(c, args):
    print("==> auth")
    for label, header in (("no token", None), ("forged token", "x.y")):
        status, _ = qsca.http("POST", f"{c.base}/chat", header or "", {"prompt": "hi", "analysis": c.analysis})
        check(f"{label} -> 401", status == 401, status)
    import urllib.request
    request = urllib.request.Request(f"{c.base}/auth/login?nonce={uuid.uuid4().hex}", method="GET")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=qsca.tls_context()))
    try:
        opener.open(request, timeout=30)
        code = 200
    except urllib.error.HTTPError as error:
        code = error.code
    check("GET /auth/login -> 302 to Identity Center", code == 302, code)


def test_chat(c, args):
    print("==> data question with a chart")
    r = c.ask(c.token(args.editor), args.chart_question)
    used = tools(r)
    check("job succeeded", r.get("status") == "SUCCEEDED", r.get("error") or r.get("status"))
    check("queried the dataset", ("query_dataset", True) in used, used)
    charts = r.get("charts") or []
    check("returned a chart with data", any(ch["type"] in ("line", "bar") and len(ch["x"]) >= 3 for ch in charts),
          [(ch["type"], ch["title"], len(ch["x"])) for ch in charts])
    check("token not leaked into the result", "_caller" not in json.dumps(r), "")
    print("     answer:", (r.get("answer") or "")[:160].replace("\n", " "))


def test_analyze(c, args):
    print("==> calculations in the code sandbox")
    r = c.ask(c.token(args.editor), args.analyze_question)
    used = tools(r)
    check("job succeeded", r.get("status") == "SUCCEEDED", r.get("error") or r.get("status"))
    check("ran analyze_result", ("analyze_result", True) in used, used)
    check("chart drawn", bool(r.get("charts")), [(ch["type"], ch["title"], len(ch["x"])) for ch in r.get("charts") or []])
    print("     answer:", (r.get("answer") or "")[:160].replace("\n", " "))


def apply_preview(c, token, result, label):
    previews = [ch for ch in result.get("charts") or [] if ch.get("pending_change")]
    if not check(f"{label}: preview returned", bool(previews), tools(result)):
        return None
    status, body = qsca.http("POST", f"{c.base}/changes/{previews[0]['pending_change']['id']}/apply", token, {"analysis": c.analysis})
    check(f"{label}: applied", status == 200 and body.get("changed"), body)
    return body


def test_edits(c, args):
    print("==> add / retitle / delete a visual, then undo all three")
    if not (args.dataset_identifier and args.category_column and args.measure_column):
        print("   skipped (pass --dataset-identifier, --category-column and --measure-column)")
        return
    token, session = c.token(args.editor), f"qs-e2e-{uuid.uuid4().hex}"
    original = c.visuals()
    r = c.ask(token, f"在分析里新增一个柱状图：按 {args.category_column} 统计 {args.measure_column} 的总和，"
                     f"标题叫“E2E 测试图表”，数据集用 {args.dataset_identifier}。先给我看预览。", session=session)
    added = apply_preview(c, token, r, "add_visual")
    if not added:
        return
    new_id = added.get("visual_id")
    check("visual added", any(v[0] == new_id and v[2] == "E2E 测试图表" for v in c.visuals()), new_id)
    r = c.ask(token, f"把 visual_id 为 {new_id} 的图表标题改成“E2E 改名”，先预览。", session=session)
    apply_preview(c, token, r, "set_title")
    check("title changed", any(v[0] == new_id and v[2] == "E2E 改名" for v in c.visuals()), "")
    r = c.ask(token, f"删除 visual_id 为 {new_id} 的图表，先预览。", session=session)
    apply_preview(c, token, r, "delete_visual")
    check("visual deleted", all(v[0] != new_id for v in c.visuals()), "")
    for i in range(3):
        r = c.ask(token, "撤销", session=session)
        check(f"undo {i + 1}", ("undo_last_visual_change", True) in tools(r), tools(r))
    check("analysis back to the original visuals", c.visuals() == original,
          [v for v in c.visuals() if v not in original] or "")


def test_controls(c, args):
    print("==> control suggestion")
    if not (args.dataset and args.control_column):
        print("   skipped (pass --dataset and --control-column)")
        return
    token = c.token(args.editor)
    station = None
    data = c.session.client("lambda").invoke(FunctionName=c.out["DataToolsFunction"], Payload=json.dumps({
        "__tool": "distinct_values", "_caller": token, "_analysis": c.analysis, "dataset_id": args.dataset,
        "column": args.control_column}).encode())
    values = json.loads(data["Payload"].read()).get("values") or []
    station = values[0] if values else None
    check(f"distinct_values on {args.control_column}", bool(station), values[:3])
    if not station:
        return
    r = c.ask(token, f"在仪表盘上只看 {args.control_column} 为 {station} 的数据")
    controls = r.get("controls") or []
    check("suggested the control value", any(x["value"] == station for x in controls), controls)


def test_preview_apply_undo(c, args):
    print("==> preview -> apply -> undo")
    if not args.visual:
        print("   skipped (pass --visual)")
        return
    before = c.visual_type(args.visual)
    target = "折线图" if before == "BarChartVisual" else "柱状图"
    want = "LineChartVisual" if before == "BarChartVisual" else "BarChartVisual"
    token, session = c.token(args.editor), f"qs-e2e-{uuid.uuid4().hex}"
    r = c.ask(token, f"把分析里 visual_id 为 {args.visual} 的图表改成{target}，先给我看预览", session=session)
    previews = [ch for ch in r.get("charts") or [] if ch.get("pending_change")]
    check("preview returned", bool(previews), tools(r))
    check("analysis unchanged by the preview", c.visual_type(args.visual) == before, before)
    if not previews:
        return
    change = previews[0]["pending_change"]["id"]
    check("preview has data", previews[0]["type"] != "none" and previews[0]["x"], previews[0]["notes"])
    if args.other_editor:
        status, body = qsca.http("POST", f"{c.base}/changes/{change}/apply", c.token(args.other_editor), {"analysis": c.analysis})
        check("another editor cannot apply it", status == 409, body)
    status, body = qsca.http("POST", f"{c.base}/changes/{change}/apply", token, {"analysis": c.analysis})
    check("owner applies it", status == 200 and body.get("changed"), body)
    check("analysis changed", c.visual_type(args.visual) == want, c.visual_type(args.visual))
    status, body = qsca.http("POST", f"{c.base}/changes/{change}/apply", token, {"analysis": c.analysis})
    check("second apply refused", status == 409, body)
    r = c.ask(token, "撤销", session=session)
    check("undo via chat", ("undo_last_visual_change", True) in tools(r), tools(r))
    check("analysis restored", c.visual_type(args.visual) == before, c.visual_type(args.visual))


def test_cancel(c, args):
    print("==> cancel")
    token = c.token(args.editor)
    job = c.ask(token, "逐个数据集、逐个数值列做按月汇总，并详细分析每一项的变化原因。", wait=False)
    time.sleep(10)
    started = time.time()
    _s, body = qsca.http("POST", f"{c.base}/jobs/{job['jobId']}/cancel", token)
    check("cancel accepted", body.get("status") == "CANCELLED", body)
    _s, result = qsca.http("GET", f"{c.base}/jobs/{job['jobId']}", token)
    check("job stays CANCELLED", result.get("status") == "CANCELLED", result.get("status"))
    print(f"     signalled={body.get('signalled')} in {time.time() - started:.1f} s")


def test_refusal(c, args):
    print("==> unknown user")
    r = c.ask(c.token("nobody@example.invalid"), "这个分析有哪些图表？")
    check("no analysis access for unknown user", not any(ok for _t, ok in tools(r)), tools(r))


TESTS = {"auth": test_auth, "chat": test_chat, "analyze": test_analyze, "controls": test_controls,
         "preview": test_preview_apply_undo, "edits": test_edits, "cancel": test_cancel, "refusal": test_refusal}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--editor", required=True, help="email of a QuickSight user who can edit the analysis")
    parser.add_argument("--other-editor", help="another editor; must not be able to apply the first one's preview")
    parser.add_argument("--visual", help="line or bar visual used for preview/apply/undo")
    parser.add_argument("--dataset", help="dataset id for the control check")
    parser.add_argument("--control-column", help="column a control lists its values from")
    parser.add_argument("--chart-question", default="按月汇总这个分析里最主要的数值指标，并画出趋势图。")
    parser.add_argument("--analyze-question", default="按月汇总这个分析里最主要的数值指标，再计算每月环比变化百分比，并画出环比图。")
    parser.add_argument("--dataset-identifier", help="dataset identifier (key of the summary's datasets) for the edit test")
    parser.add_argument("--category-column", help="a category column for the edit test")
    parser.add_argument("--measure-column", help="a numeric column for the edit test")
    parser.add_argument("--only", action="append", choices=list(TESTS))
    args = parser.parse_args()
    client = Client(args)
    for name in args.only or TESTS:
        TESTS[name](client, args)
    failed = [n for n, ok in results if not ok]
    print(f"==> {len(results) - len(failed)}/{len(results)} checks passed" + (f"; failed: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
