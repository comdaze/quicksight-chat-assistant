# QuickSight 分析助手（quicksight-chat-assistant）

给任意 QuickSight 分析或仪表盘加一个聊天窗口：按当前控件查询数据并画图、提示控件选择、预览并修改分析图表（需编辑权限，可撤销），可接入企业 MCP（如 DataHub、Metabase）。基于 Amazon Bedrock AgentCore Runtime + Gateway 与 Strands Agents，模型使用客户自己的 OpenAI 兼容端点。已在 AWS 中国区（北京 cn-north-1）验证。

- 部署：[docs/部署手册.md](docs/部署手册.md)
- 最终用户：[docs/使用说明.md](docs/使用说明.md)

## 快速开始

```bash
cp config.example.json config.json        # 按部署手册第 3 节填写
./qsca --profile <profile> deploy         # 构建并部署 CDK 栈
./qsca --profile <profile> idp-metadata <Identity Center 元数据.xml>
#   在 Secrets Manager 中编辑 <prefix>/model-config，填写模型地址和 API Key（或 ./qsca model）
./qsca --profile <profile> datasource-secret <QuickSight 数据源 ID>
./qsca --profile <profile> attach <分析 ID> --apply
./qsca --profile <profile> check --analysis <分析 ID>
```

## 目录

| 路径 | 内容 |
|---|---|
| `agent/` | AgentCore Runtime 上的 Agent（Strands）：后台任务、心跳、取消、图表、预览、控件建议 |
| `web/` | Web Lambda：聊天页、Identity Center SAML 登录、任务接口、“应用到分析”接口；`web/vendor/` 为 ECharts 6.1.0（Apache-2.0） |
| `tools/analysis/` | Gateway 工具：读取分析结构、预览/应用/撤销图表类型修改（按用户 QuickSight 权限） |
| `tools/data/` | Gateway 工具：在分析用到的数据集上做只读聚合查询（结构化请求编译为参数化 SQL） |
| `tools/common/` | 两个工具共用：用户身份校验、QuickSight 权限判断、分析定义解析 |
| `infra/` | CDK（TypeScript）栈 |
| `qsca` | 构建、部署与运维命令行 |
| `tests/unit/` | 单元测试：`python3 -m unittest discover -s tests/unit` |
| `tests/e2e.py` | 部署后的端到端测试 |

## 开发

```bash
ACCOUNT_ID=0 SESSION_SECRET_ID=x QS_REGION=cn-north-1 AWS_DEFAULT_REGION=cn-north-1 python3 -m unittest discover -s tests/unit
(cd infra && npm ci && npx tsc --noEmit)
./qsca build
```

固定版本：strands-agents 1.61.1、bedrock-agentcore 1.24.2、boto3 1.43.115、signxml 5.2.0、lxml 6.1.3、cryptography 50.0.2、pg8000 1.31.5、PyMySQL 1.1.2、aws-cdk-lib 2.274.0、ECharts 6.1.0。
