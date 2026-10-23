# QuickSight Chat Assistant

[English](#english) | [中文](#中文)

---

<a id="english"></a>

A chat panel for **any Amazon QuickSight analysis or dashboard**. Users stay in QuickSight and ask questions in natural
language; the assistant queries the data behind the analysis using the current control values, calculates, draws
charts, tells the user which control values to pick, and edits the analysis conversationally (with a preview the user
must approve, and undo). Built on Amazon Bedrock **AgentCore Runtime, Gateway and Code Interpreter** with
**Strands Agents**; the model is any **OpenAI-compatible endpoint** you provide (e.g. Amazon Bedrock, or a gateway
such as LiteLLM), switchable at any time without redeploying. Verified in AWS China (Beijing,
cn-north-1).

## Features

| Capability | How it works |
|---|---|
| Ask about the data | Reads the analysis structure (sheets, visuals, parameters, parameter-driven filters, datasets, columns) and runs read-only aggregate queries on the datasets with the current control values |
| Charts in the chat | Answers render as Markdown (headings, lists, tables) and include ECharts line/bar charts; every number in a chart is read from tool results by code, never typed by the model |
| Calculations | `analyze_result` runs pandas on query results in an AgentCore Code Interpreter with **no network** (period-over-period, share, rank, moving average, correlation, outliers, joins) |
| Control guidance | Lists the values to pick in the dashboard controls (QuickSight does not let an embedded iframe change controls) |
| Conversational analysis editing | Change chart type (line / bar / table), title, measures and aggregation, category and date grain, color split, sort; add or delete visuals. Every edit is a **preview** first; only the user's **Apply** button changes the analysis; **undo** restores the saved definition |
| Enterprise tools | Optional MCP servers (e.g. DataHub, Metabase via MCPHub) registered on the gateway |
| Long and cancellable | Answers run as background jobs (HealthyBusy, up to the session lifetime); cancel stops within seconds |
| Access control | Identity Center sign-in (the chat shows the user's name from the Identity Center directory); users only see analyses / dashboards they can view in QuickSight and only edit analyses they can edit there |
| Example questions | A collapsed bar under the input box with one tab per capability; replaceable per deployment (`examples`) |

## Architecture

```
QuickSight analysis / dashboard
  └ Custom visual content (iframe): chat page
       │ HTTPS + session token (after IAM Identity Center SAML sign-in)
       ▼
API Gateway HTTP API ─▶ Web Lambda ───────────────────────────▶ DynamoDB (jobs, sign-in state, pending edits)
                         │ │                                           ▲ heartbeats / result
                         │ └─ sign-in: Identity Center identity store (read the user's name by email)
                         │ submit job (returns at once)                │
                         ▼                                             │
                  AgentCore Runtime: Strands agent (one microVM per session)
                         │  ├─ local tools: plot_result, preview_analysis_change, suggest_controls
                         │  └─ analyze_result ─▶ AgentCore Code Interpreter (SANDBOX: no network, only result rows)
                         │ MCP + SigV4
                         ▼
                  AgentCore Gateway
                    ├ analysis tools Lambda ─▶ QuickSight API (read / edit analysis, permissions), S3 (backups)
                    ├ data tools Lambda (VPC) ─▶ databases behind the datasets (parameterized read-only SQL)
                    └ enterprise MCP servers (optional)
Model: your OpenAI-compatible endpoint, e.g. Bedrock bedrock-runtime /openai/v1 (URL and key in Secrets Manager)
```

Everything is one CDK stack in the QuickSight region. The `Apply` button calls the web Lambda, which invokes the
analysis tools Lambda directly as that user; the model can preview and undo edits but can never apply them.

## Repository layout

| Path | Contents |
|---|---|
| `agent/` | Runtime agent (Strands): jobs, heartbeats, cancel, charts (`charts.py`), sandbox (`sandbox.py`), prompt |
| `web/` | Web Lambda: chat page, SAML login, job API, apply endpoint; `web/vendor/` ECharts 6.1.0, marked 18.1.1, DOMPurify 3.4.17 (pinned copies with npm integrity, see `web/vendor/README.md`; no CDN) |
| `tools/analysis/` | Gateway tools: describe the analysis, preview / apply / undo edits |
| `tools/data/` | Gateway tools: describe datasets, structured aggregate queries, distinct values |
| `tools/common/` | Shared: caller verification and QuickSight permission checks, definition parsing, edit operations |
| `infra/` | CDK (TypeScript) stack |
| `qsca` | CLI: build, deploy, configure, attach, check, smoke test |
| `tests/` | Unit tests (`tests/unit`) and the deployed end-to-end suite (`tests/e2e.py`) |

## Deploy

### Prerequisites

| Item | Requirement |
|---|---|
| AWS | Account and the QuickSight region (cn-north-1 verified); admin-level deploy permissions |
| QuickSight | Enterprise edition, users sign in through IAM Identity Center; QuickSight user emails match Identity Center emails |
| Model | OpenAI-compatible Chat Completions endpoint with tool calling and streaming, reachable from the Runtime over public HTTPS (Amazon Bedrock, OpenAI-compatible providers, or a gateway such as LiteLLM for other APIs) |
| Machine | macOS or Linux, Node.js 20+, Python 3.9+ with `pip` and `boto3`, access to npm and PyPI (mirrors work: `npm config set registry …`, `PIP_INDEX_URL`). No Docker needed |
| CDK bootstrap | Once per account/region: `cd infra && npm ci && npx cdk bootstrap aws://<account>/<region>` |

### 1. Configure

```bash
cp config.example.json config.json
```

| Key | Meaning |
|---|---|
| `prefix` | Resource name prefix, 2-12 lowercase letters/digits (e.g. `qsca`) |
| `region` | QuickSight region |
| `quickSight.namespace` / `allowedAnalysisIds` | QuickSight namespace (usually `default`); analyses the chat may be attached to (`["*"]` = any, still per-user permissions) |
| `identityCenter.samlEntityId` | SAML audience you choose, e.g. `urn:quicksight-chat-assistant:chat` |
| `identityCenter.identityStoreId` / `region` | Optional: where to look up users' display names. Default: the Identity Center instance in the deployment region |
| `dataAccess.vpc` | Subnets for the data tools Lambda when the databases are in a VPC (`null` if they are public). Reusing the subnets of the QuickSight VPC connection is the easiest choice |
| `dataAccess.vpc.securityGroupIds` | Extra security groups for the Lambda (e.g. one the database already allows) |
| `dataAccess.vpc.createInterfaceEndpoints` | `true` creates QuickSight and Secrets Manager interface endpoints (needed when the subnets have no NAT); `false` if the VPC already has them |
| `dataAccess.datasourceSecretPrefix` | Prefix of database credential secrets (default `quicksight-chat-assistant/datasource/`) |
| `dataAccess.extraSecretArns` | Secrets used by QuickSight data sources themselves, if you want to reuse them |
| `mcpTargets` | Enterprise MCP servers (see below); `[]` for none |
| `runtime.idleSeconds` / `maxLifetimeSeconds` | Session idle timeout (300) and max lifetime (28800 = 8 h, also the longest job) |
| `api.rateLimit` / `burstLimit` | HTTP API throttling |
| `frameAncestors` | Origins allowed to embed the chat page (the QuickSight console domains of your region) |
| `examples` | Optional example questions for the chat page, replacing the built-in generic ones: `[{"group": "…", "tip": "…", "questions": ["…"]}]`, at most 2500 bytes as JSON. Use it to tailor a demo to your data. Groups with `"edit": true` (or named 修改…) are hidden in dashboards |

### 2. Deploy

```bash
./qsca --profile <aws-profile> deploy        # builds Linux arm64 packages into build/, then cdk deploy
./qsca --profile <aws-profile> outputs       # ChatUrl, AcsUrl, SamlEntityId, ModelSecretName, IdpMetadataParameter, ...
```

### 3. IAM Identity Center application (console, once)

Custom SAML applications cannot be created by API.

1. IAM Identity Center → **Applications** → **Add application** → **I have an application I want to set up** → **SAML 2.0**.
2. Download the **IAM Identity Center metadata** file.
3. Application ACS URL = output `AcsUrl`; application SAML audience = output `SamlEntityId`.
4. **Actions → Edit attribute mappings**: `Subject` → `${user:email}`, format `emailAddress`. The chat page shows the
   user's name: it is read at sign-in from the identity store by email (CJK names family name first, e.g. 杨志浩).
   Optional SAML attributes `familyName` → `${user:familyName}` and `givenName` → `${user:givenName}` take
   precedence. If Identity Center lives in another account or region, set `identityCenter.identityStoreId` /
   `region`; when the lookup is not possible the email is shown.
5. **Assign users and groups** who may use the assistant.
6. Upload the metadata (takes effect within 5 minutes, no redeploy):

```bash
./qsca --profile <p> idp-metadata ~/Downloads/<metadata>.xml
```

### 4. Model

In Secrets Manager, edit `<prefix>/model-config` (output `ModelSecretName`) as plain text:

```json
{"base_url": "https://your-endpoint/v1", "model": "model-name", "api_key": "your-key", "temperature": 0.2, "timeout": 120}
```

Takes effect within 60 seconds; switching models is just editing this secret (previous versions stay in Secrets
Manager for rollback). Or run `./qsca --profile <p> model [--base-url … --model … --params '{…}' --timeout 180]`,
which prompts for the key without echoing it. Leave out `temperature` for models that reject it.
Optional `max_retries` (default 4) retries model calls that fail with 429, 5xx or a connection error; an error
sent inside an already-open response stream is retried by the agent (up to 3 times).
Optional `params` adds fields to every Chat Completions request (for example `{"reasoning_effort": "medium"}`).
Questions and query results are sent to this endpoint: make sure it meets your data compliance rules and uses HTTPS.

Amazon Bedrock (Chat Completions on the `bedrock-runtime` endpoint, Bedrock API key), for example Kimi K3:

```json
{"base_url": "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1", "model": "us.moonshotai.kimi-k3",
 "api_key": "<Bedrock API key>", "timeout": 180,
 "params": {"reasoning_effort": "medium", "max_tokens": 8192}}
```

Bedrock is not offered in the AWS China regions, so from China this calls a global region across the border; check
that this is acceptable for the data. Notes from testing Kimi K3 (October 2026):

- It reasons at `max` effort by default, which is slow for a chat; use `medium` (`low` hung on edit requests).
- `us.moonshotai.kimi-k3` answered 2-5x faster than `global.moonshotai.kimi-k3`.
- About 1 in 6 answers degenerated into garbled text regardless of effort or temperature, ending only at
  `max_tokens` (1-2 minutes, then a clear error). Keep `max_tokens` set. Occasional HTTP 500s, also inside an open
  stream, are retried automatically.
- Long-term Bedrock API keys expire (a `401 Authentication failed` from the model means a new key is needed).

After switching models, run `tests/e2e.py`: how well a model fills in tool arguments decides both speed and accuracy.

### 5. Database credentials (built-in data queries)

QuickSight does not reveal stored database passwords. For each QuickSight **data source** the analyses use, store a
**read-only** account in the same region:

- name `<datasourceSecretPrefix><DataSourceId>`, value `{"username": "...", "password": "..."}`
- `./qsca --profile <p> datasource-secret <DataSourceId>` creates it; `./qsca check --analysis <id>` lists the ones needed.

Allow the data tools Lambda in the database security group (its security group, the subnet ranges, or a group you add
via `securityGroupIds`). Cross-region databases over VPC peering work as well.

### 6. Attach to an analysis

Self-service, no command line: in the analysis editor add a **Custom visual content** whose URL is the bare
`ChatUrl` (output, e.g. `https://<api>.execute-api.<region>.amazonaws.com.cn/`), sign in inside it, and pick the
analysis in the list it shows (the one just edited comes first) and press it to bind. That fills in the analysis id
and every parameter, the same URL `attach` builds; refresh the editor. Binding needs edit permission on the
analysis. When parameters are added or removed later, the chat offers **更新绑定**. Or from the command line:

```bash
./qsca --profile <p> attach <analysis-id>            # print the embed URL only
./qsca --profile <p> attach <analysis-id> --apply    # add the chat visual (backup saved under build/)
```

The URL carries every analysis parameter (`?analysis=<id>&p.<name>=<<$name>>…`) so the chat knows the current control
values. Options: `--sheet <id>`, `--replace <visual_id>` (take an existing visual's place). Re-run after adding or
removing parameters, then publish the dashboard with

```bash
./qsca --profile <p> publish <analysis-id> <dashboard-id> [--name "…"]   # creates or updates, then publishes
```

or simply **Publish** in the QuickSight console. The browser does not tell the chat which QuickSight page it is on,
and a console publish copies the analysis' chat URL unchanged, so after every dashboard publish (console or API) an
EventBridge rule runs the `<prefix>-dashboard-publisher` Lambda: it adds `where=dashboard` to this solution's chat URL
in the new version and publishes that (about 20 seconds; refresh the dashboard afterwards). The chat in a dashboard
then offers no edits (edits change the analysis, never a published dashboard) and says where to make them. The
dashboard is also tagged with its source analysis, which keeps dashboard-only viewers allowed. `qsca publish` does
the same in one step. Optional: a parameter `pViewer` whose dynamic default is the
viewer's email lets the chat detect a QuickSight user switch in the same browser (not used for authorization).

### 7. Check

```bash
./qsca --profile <p> check --analysis <analysis-id>                 # model, Identity Center, chat visual, credentials
./qsca --profile <p> smoke --analysis <analysis-id> --email <user>  # one question end to end (admin: reads the signing key)
python3 tests/e2e.py --profile <p> --analysis <id> --editor <email> [--other-editor <email>] \
  [--visual <line/bar visual id>] [--dataset <dataset id> --control-column <column>] \
  [--dataset-identifier <identifier> --category-column <column> --measure-column <numeric column>]
```

### Enterprise MCP servers (optional)

```json
"mcpTargets": [
  {"name": "datahub", "endpoint": "https://mcphub.example.com/datahub/mcp",
   "auth": {"type": "oauth", "providerArn": "arn:aws-cn:bedrock-agentcore:cn-north-1:<acct>:token-vault/default/oauth2credentialprovider/datahub", "scopes": ["read"]}},
  {"name": "metabase", "endpoint": "https://mcphub.example.com/metabase/mcp",
   "auth": {"type": "apiKey", "providerArn": "arn:aws-cn:...:apikeycredentialprovider/metabase", "parameterName": "X-API-Key"}}
]
```

`auth.type`: `none`, `oauth` / `apiKey` (create the AgentCore Identity credential provider first), or `iam`
(`service: "bedrock-agentcore"`, `runtimeArn`, optional `region`, for MCP servers hosted on AgentCore Runtime). The
endpoint must be public HTTPS (in China regions the gateway has no VPC egress) and support MCP protocol `2025-03-26`.
These servers never receive the user's identity: expose read-only tools only.

### Update, roll back, uninstall

- Update: pull the new code, `./qsca deploy`. The model secret, Identity Center metadata and credentials are kept.
- Roll back: deploy the previous version of the code.
- Uninstall: `cd infra && AWS_PROFILE=<p> npx cdk destroy --context config=../config.json`. Kept on purpose: the S3
  backup bucket, SSM parameter `/<prefix>/idp-metadata`, data source secrets, the chat visual in analyses, the
  Identity Center application.

## Use

### Add the chat to a new analysis (no admin needed)

Anyone who can edit an analysis can add the assistant to it in the QuickSight editor:

1. **Add a visual → Custom visual content** (自定义可视内容), place and size it (for example a column on the right).
2. Set its URL to the chat address your administrator shares: the stack output `ChatUrl` exactly as it is, e.g.
   `https://<api-id>.execute-api.cn-north-1.amazonaws.com.cn/`, with nothing after the slash.
3. In the new visual, click **使用 Identity Center 登录**. The chat says it is not bound yet and lists the analyses you
   can edit, most recently changed first (the one you are editing is normally at the top).
4. Click this analysis. The chat fills in the analysis id and all of its parameters (so it sees the current control
   values) and saves the analysis.
5. **Refresh the browser page.** The chat now works in this analysis. Publish the dashboard as usual (console
   **Publish**, or **发布到仪表盘** in the chat); its chat knows it is in a dashboard.

Notes: you need edit permission on the analysis; if the administrator limited `quickSight.allowedAnalysisIds`, the
analysis must be on that list. When you later add or remove parameters, the chat shows **更新绑定**: click it and
refresh. Administrators can still do the same from the command line with `./qsca attach <analysis-id> --apply`.

### Customize the example questions

The **示例问题** bar under the input box shows the deployment default until an analysis has its own set: one
question that fits any analysis, or the groups in `examples` of `config.json` if the administrator set them. Editors of the analysis maintain that set in the chat; viewers of the analysis and
of the dashboards published from it see it (groups whose name contains 修改 are hidden in dashboards).

- **☆ 加入示例** under any question you asked adds it to the group 常用问题 (the first time, the default examples
  shown at that moment are kept next to it).
- **管理** next to **示例问题** opens an editor: one box per group with a name, an optional tip and one question per
  line; add or delete groups, **保存**, or **恢复默认** to drop the analysis' set (saving with every group
  deleted does the same).
- To start from suggestions, ask the assistant, e.g. *Suggest some example questions for this analysis*; it lists
  questions based on the real visuals, fields and parameters. Paste the ones you want into **管理** and save.

Limits: 8 groups, 20 questions per group, 200 characters per question. Changes apply at once, no redeploy.

### Ask and edit

1. Open an analysis or dashboard with the assistant, click **Sign in with Identity Center** (usually no password when
   already signed in to QuickSight). The session lasts 8 hours. Example questions sit in the collapsed **示例问题** bar
   under the input box, one tab per group; clicking one asks it.
2. Ask, for example:
   - *Which visuals does this analysis have and what do they show?*
   - *Under the current filters, how did the average price trend last month? Draw it.*
   - *Compute month-over-month change of the main metric and rank the stations.* (pandas in the sandbox)
   - *Only show station X on the dashboard.* (lists the control values to pick)
3. Edit the analysis (users with edit permission, in the analysis editor):
   - *Change "Price trend" to a bar chart* · *Show it by month* · *Use the sum of energy instead* · *Split by station*
   - *Sort descending by value* · *Rename it to …* · *Add a table of average price by station* · *Delete that chart*
   - Each request shows a **preview** (chart or table drawn with the edited visual's data) with **Apply** / **Discard**.
     After applying, **refresh the page** (the editor does not refresh by itself). Say **undo** to revert the latest
     applied change (repeatable). After **Apply**, **发布到仪表盘** (or typing "发布到仪表盘") publishes the analysis as
     it is now to the dashboards published from it that the user may update, with the chat marked; the button stays
     after a page refresh. Edits change the analysis only; republish the dashboard to update it. In a
     dashboard the chat offers no edits.
4. **Cancel** stops a running answer; a page refresh (e.g. after picking a control value) resumes a running answer.
   The browser keeps 8 hours of conversation per user and analysis.

## Security

| Topic | Mechanism |
|---|---|
| Sign-in | Only users assigned to the Identity Center app; SAML assertions checked for signature, audience, ACS, validity and replay |
| Session | HMAC-signed token, 8 hours, stored in the iframe's browser storage |
| Who sees data | Users must be able to view the analysis or a dashboard published from it; only that analysis' datasets can be queried |
| Dataset rules | Datasets with row-level security, dataset filters or joins are refused (querying the source would bypass them); columns under column-level security are hidden from users not granted them |
| Queries | The model never writes SQL: structured requests, column names checked against the dataset, fixed aggregates and operators, bound parameters, read-only transaction, 20 s timeout, at most 500 rows |
| Sandbox | Code Interpreter in SANDBOX network mode, no execution role: it only receives result rows the user could already query |
| Who edits | `quicksight:UpdateAnalysis` on that analysis for the user (QuickSight sharing); apply only by the requester, once, within 10 minutes; the previous definition is backed up to S3 before every change |
| Identity to tools | The Runtime adds the verified token to this solution's own tool calls in code; the model cannot see or forge it; enterprise MCP servers do not get it |
| Data flow | Questions and results go to your model endpoint; logs in CloudWatch never contain tokens or passwords |

SPICE datasets: built-in queries read the current source data, which may be newer than the last SPICE refresh (the
answer says so).

## Cost

Usage-based: AgentCore Runtime (active session CPU/memory time), Gateway calls, Code Interpreter sessions, Lambda, API
Gateway, DynamoDB on demand, Secrets Manager secrets, **VPC interface endpoints (2 endpoints × subnets, hourly; usually
the largest fixed cost)**, CloudWatch Logs, plus your model. Use the AWS pricing calculator for estimates.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Popup says sign-in is not configured | Upload Identity Center metadata (`./qsca idp-metadata`) |
| `audience mismatch` / `bearer confirmation does not match` | SAML audience or ACS URL differs from the stack outputs |
| Error inside Identity Center after sign-in | User not assigned to the app, or `Subject` not mapped to `${user:email}` |
| "管理员尚未配置模型" | Model secret still has the placeholder |
| Model error `401 Authentication failed` | API key invalid or expired (Bedrock long-term keys expire): store a new one |
| Answer fails after 1-2 minutes with "输出过长被截断" | The model produced runaway output and hit `max_tokens`; retry, or use a more stable model |
| Answers are slow | Runtime log event `answered` has `setup_ms`, `tool_ms` and `model_ms` per call; repeated failed tool calls or slow model calls show there |
| The chat shows the email, not the name | Log `name_lookup_failed` in the web Lambda: user not in the identity store, or Identity Center in another account / region (set `identityCenter.identityStoreId` / `region`) |
| No permission for the analysis | The user cannot view it in QuickSight, or the emails differ |
| Missing database credentials | Create the data source secret (step 5) |
| Built-in query does not support a dataset | RLS, dataset filters, joins, or an unsupported source; add an enterprise MCP server |
| Query timeout / connection error | Database security group or routing does not allow the data tools Lambda |
| Chat says it is not bound to an analysis | Pick the analysis in the chat to bind it (edit permission needed), or run `attach --apply` |
| Applied but nothing changed | Refresh the page; republish the dashboard |
| Visuals show "出现错误" right after a publish or an applied change | The page still uses the replaced version; refresh (sometimes a few times) |

Logs: `/aws/lambda/<prefix>-web`, `/aws/lambda/<prefix>-analysis-tools`, `/aws/lambda/<prefix>-data-tools`, and
`/aws/bedrock-agentcore/runtimes/<prefix>_agent-…-DEFAULT` (one stream per session; `model_streaming` events show a
model call that runs longer than 20 s, `model_retry` the automatic retries).

## Limitations

- Built-in data queries: PostgreSQL, Aurora PostgreSQL, MySQL, Aurora MySQL, MariaDB, RDS (PostgreSQL / MySQL); single
  table or custom SQL datasets. Athena, Redshift, S3 and others: not yet (use an enterprise MCP server).
- Editing covers line, bar and table visuals (aggregated field wells); sheets with section-based layouts cannot get
  new visuals automatically; deleting a visual referenced by filters or actions is refused.
- Previews of visuals using calculated fields work only for parameter-switch fields (`ifelse(${p} = '…', {col}, …)`).
- The chat cannot change controls; the editor does not refresh after an applied change; unsaved manual edits in the
  editor and an applied change can overwrite each other.
- Cancelling while the model is still "thinking" stops at the end of that step (seconds to tens of seconds).

## Development

```bash
ACCOUNT_ID=0 SESSION_SECRET_ID=x QS_REGION=cn-north-1 AWS_DEFAULT_REGION=cn-north-1 python3 -m unittest discover -s tests/unit
(cd infra && npm ci && npx tsc --noEmit)
./qsca build
```

Pinned: strands-agents 1.61.1, bedrock-agentcore 1.24.2, boto3 1.43.115, signxml 5.2.0, lxml 6.1.3,
cryptography 50.0.2, pg8000 1.31.5, PyMySQL 1.1.2, aws-cdk-lib 2.274.0, ECharts 6.1.0, marked 18.1.1, DOMPurify 3.4.17.

---

<a id="中文"></a>

# QuickSight 分析助手

给 **任意 QuickSight 分析或仪表盘** 加一个聊天窗口。用户不离开 QuickSight，用中文提问；助手按当前控件查询分析背后的数据、
做计算、在聊天里画图、提示要在控件里选什么值，还能用对话修改分析（先预览，用户点“应用”才生效，可撤销）。基于 Amazon
Bedrock **AgentCore Runtime、Gateway、Code Interpreter** 与 **Strands Agents**，模型使用客户自己的 **OpenAI 兼容端点**
（如 Amazon Bedrock，或 LiteLLM 等网关），随时可换，无需重新部署。
已在 AWS 中国区北京（cn-north-1）验证。

## 功能

| 能力 | 说明 |
|---|---|
| 数据问答 | 读取分析结构（工作表、图表、参数、参数驱动的筛选、数据集和列），按当前控件值对数据集做只读聚合查询 |
| 聊天出图 | 回答按 Markdown 显示（标题、列表、表格），附带 ECharts 折线图 / 柱状图；图里的数字由代码从工具结果读取，不由模型编写 |
| 计算 | `analyze_result` 在 **无网络** 的 Code Interpreter 沙箱里用 pandas 处理查询结果（环比、同比、占比、排名、移动平均、相关性、异常值、合并结果） |
| 控件提示 | 列出要在上方控件中选择的值（QuickSight 不允许嵌入的 iframe 修改控件） |
| 对话式修改分析 | 改图表类型（折线 / 柱状 / 表格）、标题、指标与聚合、分类字段与时间粒度、颜色分组、排序，新增或删除图表。每次修改先 **预览**，用户点 **应用到分析** 才写入；可 **撤销** |
| 企业工具 | 可选接入企业 MCP（如经 MCPHub 的 DataHub、Metabase） |
| 长任务与取消 | 后台任务运行，最长到会话生命周期；取消通常几秒内生效 |
| 权限 | Identity Center 登录（聊天窗口显示 Identity Center 目录中的姓名）；只能查看 QuickSight 中有权查看的分析/仪表盘，只能修改有编辑权限的分析 |
| 示例问题 | 输入框下方的折叠栏，按能力分标签；每个部署可用 `examples` 替换 |

## 架构

架构图见英文部分 [Architecture](#architecture)。要点：一个 CDK 栈部署在 QuickSight 所在区域；聊天页经 API Gateway 提交任务，
AgentCore Runtime 上的 Agent 通过 Gateway 调用三类工具（分析工具、数据工具、企业 MCP），计算放在无网络的 Code Interpreter 里；
“应用到分析”按钮由 Web Lambda 以用户身份直接调用分析工具，模型只能预览和撤销，不能应用。

## 部署

1. **前提**：QuickSight 企业版且用 IAM Identity Center 登录（邮箱一致）；OpenAI 兼容模型端点（支持工具调用和流式输出，Runtime 能经公网 HTTPS 访问，如 Amazon Bedrock、各家兼容接口，其他协议可经 LiteLLM 转换）；部署机器有 Node.js 20+、Python 3.9+（pip、boto3），能访问 npm 与 PyPI；目标账号区域执行过一次 `cd infra && npm ci && npx cdk bootstrap aws://<账号>/<区域>`。
2. **配置**：`cp config.example.json config.json`，按英文部分的配置表填写（前缀、区域、SAML 受众、可选的 Identity Store 位置、数据工具 Lambda 的 VPC 子网、是否创建接口端点、数据源密钥前缀、企业 MCP；允许嵌入聊天页的 `frameAncestors`；可选 `examples` 自定义聊天页的示例问题，便于针对客户数据演示）。数据库在 VPC 内时，推荐直接使用 QuickSight VPC 连接所在的子网。
3. **部署**：`./qsca --profile <p> deploy`，完成后 `./qsca --profile <p> outputs` 查看 `ChatUrl`、`AcsUrl`、`SamlEntityId`、`ModelSecretName`。
4. **Identity Center 应用（控制台手工操作）**：应用程序 → 添加应用程序 → 我有要设置的应用程序 → SAML 2.0；下载元数据；ACS URL 填 `AcsUrl`，SAML 受众填 `SamlEntityId`；编辑属性映射 `Subject` = `${user:email}`、格式 `emailAddress`（聊天窗口显示的姓名在登录时按邮箱从 Identity Center 用户目录读取，中文名按"姓+名"显示；断言里的 `familyName`、`givenName` 属性优先；Identity Center 在其他账号或区域时配置 `identityCenter.identityStoreId` / `region`，查不到就显示邮箱）；分配用户和组；然后 `./qsca --profile <p> idp-metadata <元数据.xml>`。
5. **模型**：在 Secrets Manager 编辑 `<prefix>/model-config`，填入 `{"base_url":"https://…/v1","model":"…","api_key":"…","temperature":0.2,"timeout":120}`，60 秒内生效，Secrets Manager 保留旧版本便于回退；或 `./qsca model`（可带 `--base-url`、`--model`、`--params`、`--timeout`，Key 输入不回显）。不接受 `temperature` 的模型去掉该字段。可选 `max_retries`（默认 4）在模型返回 429、5xx 或连接失败时重试，响应流中途的错误由 Agent 再重试最多 3 次；可选 `params` 给每个请求附加字段（如 `{"reasoning_effort":"medium"}`）。使用 Amazon Bedrock 时 `base_url` 填 `https://bedrock-runtime.<区域>.amazonaws.com/openai/v1`、`api_key` 填 Bedrock API Key（Kimi K3 的模型 ID 如 `global.moonshotai.kimi-k3`（全球路由）或 `us.moonshotai.kimi-k3`（仅美国区域），建议 `params` 为 `{"reasoning_effort":"medium","max_tokens":8192}`；实测 `us.` 比 `global.` 快 2 到 5 倍，`low` 在修改分析类请求上会卡死；无论推理档位和温度，约六分之一的回答会变成乱码，直到 `max_tokens` 才结束，所以必须设 `max_tokens`；偶发的 500 会自动重试；Bedrock 长期 API Key 会过期，模型报 `401 Authentication failed` 时换新 Key）。换模型后建议跑一遍 `tests/e2e.py`；中国区没有 Bedrock，从中国区调用会跨境访问海外区域，需确认数据合规。提问与查询结果会发送到该端点，请确认合规并使用 HTTPS。
6. **数据库凭证**：为分析用到的每个 QuickSight 数据源，在同一区域创建只读账号密钥 `<前缀><DataSourceId>`，内容 `{"username","password"}`（`./qsca datasource-secret <DataSourceId>`）；数据库安全组放行数据工具 Lambda。
7. **接入分析**：自助方式，不用命令行：在分析编辑界面添加“自定义可视内容”（Custom visual content），网址填部署输出的 `ChatUrl`（不带任何参数），在聊天窗口里登录后，从列出的分析中点选它所在的分析（刚编辑过的排在最前）即可绑定，绑定会补上分析 ID 和全部参数，刷新编辑界面后可用；需要该分析的编辑权限；以后分析增删了参数，聊天窗口会提示“更新绑定”。命令行方式：`./qsca --profile <p> attach <分析ID> --apply`（只打印地址时去掉 `--apply`；`--replace <visual_id>` 可替换已有图表的位置），然后在 QuickSight 控制台照常发布仪表盘（或 `./qsca --profile <p> publish <分析ID> <仪表盘ID>`）：浏览器不会告诉聊天窗口自己在哪个页面，控制台发布又会原样复制分析里的聊天地址，所以每次发布后 EventBridge 会触发 `<前缀>-dashboard-publisher` Lambda，在新版本的聊天地址里加 `where=dashboard` 并发布（约 20 秒，之后刷新仪表盘）；仪表盘里的聊天就不提供修改（修改只作用于分析），并提示到分析里改；仪表盘还会打上来源分析的标签，只有仪表盘权限的用户仍可使用。可选：建参数 `pViewer`（动态默认值为当前用户邮箱），用于检测同一浏览器切换了 QuickSight 用户。
8. **自检**：`./qsca --profile <p> check --analysis <分析ID>`；管理员可用 `smoke` 和 `tests/e2e.py` 做端到端测试。
9. **企业 MCP**：在 `mcpTargets` 中配置（示例见英文部分），重新 `deploy`。端点须为公网 HTTPS（中国区 Gateway 不能访问 VPC 内地址），支持 MCP 协议 `2025-03-26`；这些服务收不到用户身份，只开放只读工具。
10. **更新 / 回滚 / 卸载**：更新拉代码后 `deploy`（模型、元数据、凭证不会被覆盖）；回滚用上一个版本代码 `deploy`；卸载 `cd infra && AWS_PROFILE=<p> npx cdk destroy --context config=../config.json`，S3 备份桶、SSM 元数据参数、数据源密钥、分析中的聊天可视对象、Identity Center 应用需手动清理。

## 使用

### 给新分析加上聊天窗口（不需要管理员）

有分析编辑权限的人，在 QuickSight 编辑界面里就能自己加：

1. **添加可视对象 → 自定义可视内容**（Custom visual content），摆好位置和大小（比如放在右侧一列）。
2. 网址填管理员提供的聊天地址，即部署输出的 `ChatUrl` 原样填写，例如 `https://<api-id>.execute-api.cn-north-1.amazonaws.com.cn/`，斜杠后面不要加任何内容。
3. 在新加的可视对象里点 **使用 Identity Center 登录**。聊天窗口会提示“还没有绑定分析”，并列出你能编辑的分析，最近修改的排在前面（正在编辑的这个通常在第一个）。
4. 点选这个分析。聊天窗口会补上分析 ID 和全部参数（这样才能拿到当前控件值），并保存分析。
5. **刷新浏览器页面**，聊天窗口即可在这个分析里使用。之后照常发布仪表盘（控制台“发布”，或在聊天里点/说“发布到仪表盘”），仪表盘里的聊天会自动识别自己在仪表盘中。

说明：需要该分析的编辑权限；如果管理员在 `quickSight.allowedAnalysisIds` 里限定了分析，新分析要在名单内。以后分析增删了参数，聊天窗口会出现 **更新绑定**，点一下再刷新即可。管理员也可以继续用命令行 `./qsca attach <分析ID> --apply`。

### 自定义示例问题

输入框下方的 **示例问题** 在分析有自己的一套之前显示部署级默认：内置的是一个适用于任何分析的问题；管理员也可以在 `config.json` 的 `examples` 里配置。分析的编辑者在聊天窗口里维护这一套；能看这个分析、以及由它发布的仪表盘的人都能看到（仪表盘里不显示名称含“修改”的分组）。

- 自己问过的问题下方有 **☆ 加入示例**，点一下加入“常用问题”分组（第一次加入时，会保留当时显示的默认示例）。
- **示例问题** 旁的 **管理** 打开编辑器：每个分组有名称、可选说明、每行一个问题；可以添加/删除分组、**保存**，或 **恢复默认**（删除分析自己的那一套；删光所有分组再保存效果相同）。
- 想先要一些建议，可以直接问助手，例如“给这个分析出一些示例问题”，它会按真实的图表、字段和参数列出问题；把需要的粘贴到 **管理** 里保存即可。

限制：最多 8 个分组、每组 20 个问题、每个问题 200 字。保存后立即生效，无需重新部署。

### 提问与修改

1. 打开接入了助手的分析或仪表盘，点 **使用 Identity Center 登录**（已登录 QuickSight 时通常无需密码），有效期 8 小时。输入框下方折叠的 **示例问题** 栏按分组列出了示例，展开后点击即可提问。
2. 提问示例：
   - 这个分析有哪些图表，各自展示什么？
   - 按当前筛选，最近一个月的平均价格走势如何？画个图。
   - 计算主要指标的每月环比，并给各场站排名。（沙箱里用 pandas 计算）
   - 在仪表盘上只看某某场站。（列出要在控件里选的值）
3. 修改分析（需要该分析的编辑权限，在分析编辑界面里）：
   - “把价格走势改成柱状图”“改成按月显示”“改用电量的总和”“按场站分组”“按数值降序”“标题改成……”“新增一个按场站统计平均价格的表格”“删除这个图表”
   - 每次都会先出 **预览**（用修改后的口径画图或表格），点 **应用到分析** 才写入，点 **不用了** 放弃；预览 10 分钟内有效。
   - 应用后需要 **手动刷新页面**；说 **撤销** 恢复最近一次已应用的修改，可连续撤销。应用后可点 **发布到仪表盘**（或直接发送“发布到仪表盘”），把分析当前内容发布到由它发布、你有更新权限的仪表盘（自动带上仪表盘标记）；刷新页面后按钮仍在。只改分析，仪表盘需重新发布；仪表盘里的聊天窗口不提供修改。
4. 等待时可点 **取消**；切换控件导致页面刷新后，会自动继续获取刚才的回答。本浏览器按用户和分析保存 8 小时内的对话。

## 安全

见英文部分 [Security](#security)。要点：只允许分配到 Identity Center 应用的用户登录；按 QuickSight 自身的共享权限决定谁能看、谁能改；
启用了行级权限、过滤条件或联接的数据集拒绝内置查询，列级权限限制的列对无权用户隐藏；模型不写 SQL；计算沙箱无网络、只拿到结果行；
修改前备份到 S3，只有发起人能在 10 分钟内应用一次；用户令牌只由代码附加给本方案自己的工具，模型看不到也无法伪造。

## 故障排查与限制

见英文部分 [Troubleshooting](#troubleshooting) 与 [Limitations](#limitations)。常见：登录弹窗提示未配置 → 上传元数据；
回答“管理员尚未配置模型” → 编辑模型密钥；“缺少数据库凭证” → 创建数据源密钥；“内置查询不支持这个数据集” → 数据集有 RLS、过滤或联接，
可接入企业 MCP；应用后看不到变化 → 刷新页面、重新发布仪表盘；发布或应用修改后图表短暂显示“出现错误” → 页面还在用旧版本，刷新（有时要多刷几次）；模型报 `401` → API Key 失效或过期；1 到 2 分钟后提示“输出过长被截断” → 模型输出失控，重试或换更稳定的模型；
回答慢 → 看 Runtime 日志 `answered` 事件里每次的 `setup_ms`、`tool_ms`、`model_ms`；聊天窗口显示邮箱而不是姓名 → 看 web Lambda 日志 `name_lookup_failed`。
