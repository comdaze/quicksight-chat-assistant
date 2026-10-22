"""Run pandas code on earlier tool results in an AgentCore Code Interpreter session (no network, no credentials).

Only rows the user was already allowed to query go into the sandbox, as CSV files named after their result ids. The
sandbox never receives the session token, database credentials or AWS credentials of this Runtime, and it cannot
call any tool. The Code Interpreter used by this solution is created with network mode SANDBOX (no internet).
"""

import csv
import io
import json
import os

import boto3

CODE_INTERPRETER_ID = os.environ.get("CODE_INTERPRETER_ID", "")
MAX_ROWS_IN = 5000
MAX_ROWS_OUT = 500
OUTPUT_LIMIT = 3000
RESULT_MARKER = "__QSCA_RESULT__"
EPILOGUE = f"""
import json as _qsca_json
_qsca_out = globals().get("result")
if _qsca_out is not None:
    import pandas as _qsca_pd
    if isinstance(_qsca_out, _qsca_pd.Series):
        _qsca_out = _qsca_out.reset_index()
    if isinstance(_qsca_out, _qsca_pd.DataFrame):
        if not isinstance(_qsca_out.index, _qsca_pd.RangeIndex):
            _qsca_out = _qsca_out.reset_index()
        _qsca_out = _qsca_out.head({MAX_ROWS_OUT})
        _qsca_out.columns = [str(c) for c in _qsca_out.columns]
        print("{RESULT_MARKER}" + _qsca_out.to_json(orient="records", date_format="iso", force_ascii=False))
"""


class SandboxError(Exception):
    pass


def to_csv(rows):
    fields = list(dict.fromkeys(k for r in rows[:MAX_ROWS_IN] for k in r))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows[:MAX_ROWS_IN]:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buffer.getvalue()


class Sandbox:
    """One Code Interpreter session per agent run, started on first use and stopped by close()."""

    def __init__(self, region):
        self.client = boto3.client("bedrock-agentcore", region_name=region)
        self.session_id = None
        self.loaded = set()

    def _invoke(self, name, arguments):
        out = self.client.invoke_code_interpreter(codeInterpreterIdentifier=CODE_INTERPRETER_ID,
                                                  sessionId=self.session_id, name=name, arguments=arguments)
        result = {"stdout": "", "stderr": "", "exitCode": 0, "isError": False}
        for event in out["stream"]:
            body = event.get("result")
            if body is None:
                problem = next(iter(event))
                raise SandboxError(f"代码沙箱返回 {problem}")
            structured = body.get("structuredContent") or {}
            result["stdout"] += structured.get("stdout", "")
            result["stderr"] += structured.get("stderr", "")
            result["exitCode"] = structured.get("exitCode", result["exitCode"])
            result["isError"] = result["isError"] or bool(body.get("isError"))
            if not structured:
                result["stdout"] += "".join(c.get("text", "") for c in body.get("content", []) if c.get("type") == "text")
        return result

    def run(self, frames, code):
        """frames: {result_id: rows}. Returns {"rows", "stdout", "stderr", "ok"}."""
        if not CODE_INTERPRETER_ID:
            raise SandboxError("未配置代码沙箱（CODE_INTERPRETER_ID）")
        if self.session_id is None:
            self.session_id = self.client.start_code_interpreter_session(
                codeInterpreterIdentifier=CODE_INTERPRETER_ID, name="qsca-analysis", sessionTimeoutSeconds=900)["sessionId"]
        new = {rid: rows for rid, rows in frames.items() if rid not in self.loaded}
        if new:
            self._invoke("writeFiles", {"content": [{"path": f"{rid}.csv", "text": to_csv(rows)} for rid, rows in new.items()]})
            self.loaded.update(new)
        prelude = "import pandas as pd\nimport numpy as np\n" + "".join(
            f"{rid} = pd.read_csv('{rid}.csv')\n" for rid in frames)
        prepared = self._invoke("executeCode", {"language": "python", "code": prelude + "result = None\n"})
        if prepared["exitCode"] or prepared["isError"]:
            raise SandboxError(f"加载数据失败：{prepared['stderr'][-500:]}")
        ran = self._invoke("executeCode", {"language": "python", "code": code})
        rows, stdout = None, ran["stdout"]
        if not (ran["exitCode"] or ran["isError"]):
            captured = self._invoke("executeCode", {"language": "python", "code": EPILOGUE})
            for line in captured["stdout"].splitlines():
                if line.startswith(RESULT_MARKER):
                    rows = json.loads(line[len(RESULT_MARKER):])
        return {"rows": rows, "stdout": stdout[-OUTPUT_LIMIT:], "stderr": ran["stderr"][-OUTPUT_LIMIT:],
                "ok": not (ran["exitCode"] or ran["isError"])}

    def close(self):
        if self.session_id:
            try:
                self.client.stop_code_interpreter_session(codeInterpreterIdentifier=CODE_INTERPRETER_ID,
                                                          sessionId=self.session_id)
            except Exception:  # noqa: BLE001 - the session also times out by itself
                pass
            self.session_id = None
