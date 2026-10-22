import * as path from 'path';
import * as fs from 'fs';
import { CfnOutput, Duration, RemovalPolicy, SecretValue, Stack, StackProps, Fn } from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';
import * as apigwIntegrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';
import * as secrets from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { AppConfig, McpTargetConfig } from './config';

const ROOT = path.join(__dirname, '..', '..');
const BUILD = path.join(ROOT, 'build');
/** Kept constant on purpose: if it changed, a stack update would overwrite the model settings the admin entered. */
const LLM_PLACEHOLDER = JSON.stringify({
  base_url: '<https://your-openai-compatible-endpoint/v1>', model: '<model-name>', api_key: '<api-key>',
  temperature: 0.2, timeout: 120,
});

function toolSchema(file: string): agentcore.CfnGatewayTarget.ToolDefinitionProperty[] {
  return JSON.parse(fs.readFileSync(path.join(ROOT, file), 'utf-8'));
}

function mcpCredentials(target: McpTargetConfig): agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty[] | undefined {
  const auth = target.auth;
  switch (auth.type) {
    case 'none': return undefined;
    case 'iam': return [{ credentialProviderType: 'GATEWAY_IAM_ROLE', credentialProvider: { iamCredentialProvider: { service: auth.service, region: auth.region } } }];
    case 'oauth': return [{ credentialProviderType: 'OAUTH', credentialProvider: { oauthCredentialProvider: { providerArn: auth.providerArn, scopes: auth.scopes } } }];
    case 'apiKey': return [{
      credentialProviderType: 'API_KEY', credentialProvider: { apiKeyCredentialProvider: {
        providerArn: auth.providerArn, credentialParameterName: auth.parameterName, credentialPrefix: auth.prefix,
        credentialLocation: auth.location ?? 'HEADER' } },
    }];
  }
}

export class ChatAssistantStack extends Stack {
  constructor(scope: Construct, id: string, config: AppConfig, props?: StackProps) {
    super(scope, id, props);
    const { prefix } = config;
    const partition = this.partition;
    const qs = `arn:${partition}:quicksight:${this.region}:${this.account}`;
    const analysisResources = config.quickSight.allowedAnalysisIds.includes('*')
      ? [`${qs}:analysis/*`] : config.quickSight.allowedAnalysisIds.map((a) => `${qs}:analysis/${a}`);
    const idpParameter = `/${prefix}/idp-metadata`;
    const logGroup = (id: string, fn: string) => new logs.LogGroup(this, id, {
      logGroupName: `/aws/lambda/${fn}`, retention: logs.RetentionDays.THREE_MONTHS, removalPolicy: RemovalPolicy.DESTROY,
    });

    // ---- Storage and secrets ----
    const jobs = new dynamodb.Table(this, 'Jobs', {
      tableName: `${prefix}-jobs`, partitionKey: { name: 'jobId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST, timeToLiveAttribute: 'expiresAt',
      removalPolicy: RemovalPolicy.DESTROY, // chat jobs, login state and pending changes; all expire within a day
    });
    const backups = new s3.Bucket(this, 'Backups', {
      encryption: s3.BucketEncryption.S3_MANAGED, blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, enforceSSL: true,
      versioned: true, removalPolicy: RemovalPolicy.RETAIN, // analysis definitions saved before each change
    });
    const sessionKey = new secrets.Secret(this, 'SessionKey', {
      secretName: `${prefix}/session-key`, description: 'Signs chat session tokens (any random string).',
      generateSecretString: { passwordLength: 64, excludePunctuation: true },
    });
    const llm = new secrets.Secret(this, 'ModelConfig', {
      secretName: `${prefix}/model-config`,
      description: 'OpenAI-compatible model endpoint: {"base_url","model","api_key","temperature","timeout"}. Edit after deployment.',
      secretStringValue: SecretValue.unsafePlainText(LLM_PLACEHOLDER),
    });

    // ---- Tool Lambdas (Python 3.12, arm64; built into build/ by ./qsca build) ----
    const common = {
      ACCOUNT_ID: this.account, QS_REGION: this.region, QS_NAMESPACE: config.quickSight.namespace,
      SESSION_SECRET_ID: sessionKey.secretName, ALLOWED_ANALYSIS_IDS: config.quickSight.allowedAnalysisIds.join(','),
    };
    const quicksightRead = new iam.PolicyStatement({
      actions: ['quicksight:DescribeAnalysis', 'quicksight:DescribeAnalysisDefinition', 'quicksight:DescribeAnalysisPermissions'],
      resources: analysisResources,
    });
    const quicksightPeople = new iam.PolicyStatement({
      actions: ['quicksight:ListUsers', 'quicksight:ListUserGroups'],
      resources: [`${qs}:user/${config.quickSight.namespace}/*`],
    });
    const quicksightDashboards = new iam.PolicyStatement({
      actions: ['quicksight:ListDashboards', 'quicksight:DescribeDashboard', 'quicksight:DescribeDashboardPermissions'],
      resources: [`${qs}:dashboard/*`],
    });

    const analysisFn = new lambda.Function(this, 'AnalysisTools', {
      functionName: `${prefix}-analysis-tools`, runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler', code: lambda.Code.fromAsset(path.join(BUILD, 'tools-analysis')),
      timeout: Duration.seconds(60), memorySize: 512, logGroup: logGroup('AnalysisToolsLogs', `${prefix}-analysis-tools`),
      environment: { ...common, BACKUP_BUCKET: backups.bucketName, JOBS_TABLE: jobs.tableName },
      description: 'Describe and edit the QuickSight analysis the chat is embedded in (per-user permission checks).',
    });
    analysisFn.addToRolePolicy(quicksightRead);
    analysisFn.addToRolePolicy(quicksightPeople);
    analysisFn.addToRolePolicy(quicksightDashboards);
    analysisFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['quicksight:UpdateAnalysis'], resources: analysisResources }));
    // Column names and types for validating conversational edits.
    analysisFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['quicksight:DescribeDataSet'], resources: [`${qs}:dataset/*`] }));
    // UpdateAnalysis requires PassDataSet on every dataset the definition references.
    analysisFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['quicksight:PassDataSet'], resources: [`${qs}:dataset/*`] }));
    sessionKey.grantRead(analysisFn);
    backups.grantReadWrite(analysisFn, 'analysis-backups/*');
    backups.grantDelete(analysisFn, 'analysis-backups/*');
    analysisFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:PutObjectTagging', 's3:GetObjectTagging'], resources: [backups.arnForObjects('analysis-backups/*')],
    }));
    analysisFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem', 'dynamodb:GetItem', 'dynamodb:UpdateItem'], resources: [jobs.tableArn],
      conditions: { 'ForAllValues:StringLike': { 'dynamodb:LeadingKeys': ['pending#*'] } },
    }));

    let vpcProps: Partial<lambda.FunctionProps> = {};
    const vpcConfig = config.dataAccess.vpc;
    if (vpcConfig) {
      const vpc = ec2.Vpc.fromLookup(this, 'DataVpc', { vpcId: vpcConfig.vpcId });
      const subnets = vpcConfig.subnetIds.map((id, i) => ec2.Subnet.fromSubnetId(this, `DataSubnet${i}`, id));
      const own = new ec2.SecurityGroup(this, 'DataToolsSg', { vpc, description: `${prefix} data tools`, allowAllOutbound: true });
      const groups = [own, ...vpcConfig.securityGroupIds.map((id, i) => ec2.SecurityGroup.fromSecurityGroupId(this, `DataSg${i}`, id))];
      if (vpcConfig.createInterfaceEndpoints) {
        // The Lambda has no internet in these subnets: reach QuickSight and Secrets Manager privately.
        const endpointSg = new ec2.SecurityGroup(this, 'EndpointSg', { vpc, description: `${prefix} interface endpoints` });
        endpointSg.addIngressRule(own, ec2.Port.tcp(443), 'data tools');
        for (const [id, service] of [['QuickSightEndpoint', 'quicksight'], ['SecretsEndpoint', 'secretsmanager']]) {
          new ec2.InterfaceVpcEndpoint(this, id, {
            vpc, service: new ec2.InterfaceVpcEndpointService(`com.amazonaws.${this.region}.${service}`, 443),
            subnets: { subnets }, securityGroups: [endpointSg], privateDnsEnabled: true,
          });
        }
      }
      vpcProps = { vpc, vpcSubnets: { subnets }, securityGroups: groups, allowPublicSubnet: true };
    }
    const dataFn = new lambda.Function(this, 'DataTools', {
      functionName: `${prefix}-data-tools`, runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler', code: lambda.Code.fromAsset(path.join(BUILD, 'tools-data')),
      timeout: Duration.seconds(60), memorySize: 512, logGroup: logGroup('DataToolsLogs', `${prefix}-data-tools`),
      environment: { ...common, DATASOURCE_SECRET_PREFIX: config.dataAccess.datasourceSecretPrefix },
      description: 'Read-only aggregate queries on the datasets of the analysis the chat is embedded in.',
      ...vpcProps,
    });
    dataFn.addToRolePolicy(quicksightRead);
    dataFn.addToRolePolicy(quicksightPeople);
    dataFn.addToRolePolicy(quicksightDashboards);
    dataFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['quicksight:DescribeDataSet', 'quicksight:DescribeDataSource'],
      resources: [`${qs}:dataset/*`, `${qs}:datasource/*`],
    }));
    dataFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['rds:DescribeDBInstances'], resources: ['*'] }));
    sessionKey.grantRead(dataFn);
    dataFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'],
      resources: [`arn:${partition}:secretsmanager:*:${this.account}:secret:${config.dataAccess.datasourceSecretPrefix}*`,
        ...config.dataAccess.extraSecretArns],
    }));

    // ---- AgentCore Gateway (MCP, IAM inbound) with this solution's tools and optional enterprise MCP servers ----
    const gatewayRole = new iam.Role(this, 'GatewayRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', { conditions: { StringEquals: { 'aws:SourceAccount': this.account } } }),
    });
    analysisFn.grantInvoke(gatewayRole);
    dataFn.grantInvoke(gatewayRole);
    const gateway = new agentcore.CfnGateway(this, 'Gateway', {
      name: `${prefix}-gateway`, protocolType: 'MCP', authorizerType: 'AWS_IAM', roleArn: gatewayRole.roleArn,
      description: 'Tools for the QuickSight chat assistant',
    });
    gateway.node.addDependency(gatewayRole);
    const lambdaTarget = (name: string, fn: lambda.Function, schema: string, description: string) => {
      const target = new agentcore.CfnGatewayTarget(this, `Target-${name}`, {
        gatewayIdentifier: gateway.attrGatewayIdentifier, name, description,
        targetConfiguration: { mcp: { lambda: { lambdaArn: fn.functionArn, toolSchema: { inlinePayload: toolSchema(schema) } } } },
        credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
      });
      target.node.addDependency(gatewayRole);
    };
    lambdaTarget('analysis', analysisFn, 'tools/analysis/tool_schema.json', 'Describe and edit the embedding analysis');
    lambdaTarget('data', dataFn, 'tools/data/tool_schema.json', 'Read-only queries on the analysis datasets');
    for (const target of config.mcpTargets ?? []) {
      if (target.auth.type === 'iam' && target.auth.runtimeArn) {
        gatewayRole.addToPolicy(new iam.PolicyStatement({
          actions: ['bedrock-agentcore:InvokeAgentRuntime'], resources: [target.auth.runtimeArn, `${target.auth.runtimeArn}/*`],
        }));
      }
      const mcpTarget = new agentcore.CfnGatewayTarget(this, `Target-${target.name}`, {
        gatewayIdentifier: gateway.attrGatewayIdentifier, name: target.name, description: target.description,
        targetConfiguration: { mcp: { mcpServer: { endpoint: target.endpoint } } },
        credentialProviderConfigurations: mcpCredentials(target),
      });
      mcpTarget.node.addDependency(gatewayRole); // the gateway lists the server's tools when the target is created
    }

    // ---- Code Interpreter for analyze_result: no internet (SANDBOX), no execution role, only result rows go in ----
    const codeInterpreter = new agentcore.CfnCodeInterpreterCustom(this, 'CodeInterpreter', {
      name: `${prefix}_sandbox`, description: 'pandas calculations on chat query results (no network)',
      networkConfiguration: { networkMode: 'SANDBOX' },
    });

    // ---- AgentCore Runtime (Strands agent, Python 3.12 ZIP built by ./qsca build) ----
    const agentCode = new s3assets.Asset(this, 'AgentCode', { path: path.join(BUILD, 'agent.zip') });
    const runtimeRole = new iam.Role(this, 'RuntimeRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', { conditions: { StringEquals: { 'aws:SourceAccount': this.account } } }),
    });
    agentCode.grantRead(runtimeRole);
    llm.grantRead(runtimeRole);
    runtimeRole.addToPolicy(new iam.PolicyStatement({ actions: ['bedrock-agentcore:InvokeGateway'], resources: [gateway.attrGatewayArn] }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({ actions: ['dynamodb:UpdateItem'], resources: [jobs.tableArn] }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:StartCodeInterpreterSession', 'bedrock-agentcore:InvokeCodeInterpreter',
        'bedrock-agentcore:StopCodeInterpreterSession', 'bedrock-agentcore:GetCodeInterpreterSession'],
      resources: [codeInterpreter.attrCodeInterpreterArn],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams', 'logs:DescribeLogGroups',
        'xray:PutTraceSegments', 'xray:PutTelemetryRecords', 'cloudwatch:PutMetricData'],
      resources: ['*'],
    }));
    runtimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:GetWorkloadAccessToken', 'bedrock-agentcore:GetWorkloadAccessTokenForJWT', 'bedrock-agentcore:GetWorkloadAccessTokenForUserId'],
      resources: [`arn:${partition}:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default*`],
    }));
    const runtime = new agentcore.CfnRuntime(this, 'Agent', {
      agentRuntimeName: `${prefix}_agent`, roleArn: runtimeRole.roleArn, description: 'QuickSight chat assistant agent',
      agentRuntimeArtifact: { codeConfiguration: {
        code: { s3: { bucket: agentCode.s3BucketName, prefix: agentCode.s3ObjectKey } }, runtime: 'PYTHON_3_12', entryPoint: ['main.py'] } },
      networkConfiguration: { networkMode: 'PUBLIC' },
      lifecycleConfiguration: { idleRuntimeSessionTimeout: config.runtime.idleSeconds, maxLifetime: config.runtime.maxLifetimeSeconds },
      environmentVariables: {
        GATEWAY_URL: gateway.attrGatewayUrl, LLM_SECRET_ID: llm.secretName, JOBS_TABLE: jobs.tableName, APP_REGION: this.region,
        CODE_INTERPRETER_ID: codeInterpreter.attrCodeInterpreterId,
      },
    });
    runtime.node.addDependency(runtimeRole);

    // ---- Web Lambda + HTTP API (chat page, SAML login, jobs, apply button) ----
    const identityRegion = config.identityCenter.region ?? this.region;
    const webFn = new lambda.Function(this, 'Web', {
      functionName: `${prefix}-web`, runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler', code: lambda.Code.fromAsset(path.join(BUILD, 'web')),
      timeout: Duration.seconds(30), memorySize: 512, logGroup: logGroup('WebLogs', `${prefix}-web`),
      environment: {
        RUNTIME_ARN: runtime.attrAgentRuntimeArn, RUNTIME_VERSION: runtime.attrAgentRuntimeVersion, JOBS_TABLE: jobs.tableName,
        ANALYSIS_TOOLS_FN: analysisFn.functionName, SESSION_SECRET_ID: sessionKey.secretName,
        IDP_METADATA_PARAMETER: idpParameter, SAML_SP_ENTITY_ID: config.identityCenter.samlEntityId,
        FRAME_ANCESTORS: config.frameAncestors, EXAMPLES_JSON: JSON.stringify(config.examples ?? []),
        IDENTITY_STORE_ID: config.identityCenter.identityStoreId ?? '', IDENTITY_CENTER_REGION: identityRegion,
      },
      description: 'Chat page, IAM Identity Center SAML login and job API for the QuickSight chat assistant.',
    });
    jobs.grantReadWriteData(webFn);
    sessionKey.grantRead(webFn);
    analysisFn.grantInvoke(webFn);
    webFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime', 'bedrock-agentcore:StopRuntimeSession'],
      resources: [runtime.attrAgentRuntimeArn, `${runtime.attrAgentRuntimeArn}/*`],
    }));
    webFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'], resources: [`arn:${partition}:ssm:${this.region}:${this.account}:parameter${idpParameter}`],
    }));
    // Display names: read-only lookup of the signed-in user (by email) in the Identity Center identity store.
    const store = config.identityCenter.identityStoreId ?? '*';
    webFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['identitystore:GetUserId', 'identitystore:DescribeUser'],
      resources: [`arn:${partition}:identitystore::${this.account}:identitystore/${store}`, `arn:${partition}:identitystore:::user/*`],
    }));
    if (!config.identityCenter.identityStoreId) {
      webFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['sso:ListInstances'], resources: ['*'] }));
    }
    const api = new apigw.HttpApi(this, 'Api', {
      apiName: `${prefix}-api`, defaultIntegration: new apigwIntegrations.HttpLambdaIntegration('WebIntegration', webFn),
    });
    const stage = api.defaultStage!.node.defaultChild as apigw.CfnStage;
    stage.defaultRouteSettings = { throttlingRateLimit: config.api.rateLimit, throttlingBurstLimit: config.api.burstLimit };

    const apiUrl = Fn.join('', ['https://', api.apiId, `.execute-api.${this.region}.${this.urlSuffix}`]);
    new CfnOutput(this, 'ChatUrl', { value: Fn.join('', [apiUrl, '/']), description: 'Base URL of the chat page (used by qsca attach)' });
    new CfnOutput(this, 'AcsUrl', { value: Fn.join('', [apiUrl, '/saml/acs']), description: 'Identity Center application ACS URL' });
    new CfnOutput(this, 'SamlEntityId', { value: config.identityCenter.samlEntityId, description: 'Identity Center application SAML audience' });
    new CfnOutput(this, 'IdpMetadataParameter', { value: idpParameter, description: 'SSM parameter for the Identity Center metadata XML' });
    new CfnOutput(this, 'ModelSecretName', { value: llm.secretName, description: 'Edit this secret to configure the model' });
    new CfnOutput(this, 'SessionSecretName', { value: sessionKey.secretName });
    new CfnOutput(this, 'RuntimeArn', { value: runtime.attrAgentRuntimeArn });
    new CfnOutput(this, 'GatewayId', { value: gateway.attrGatewayIdentifier });
    new CfnOutput(this, 'AnalysisToolsFunction', { value: analysisFn.functionName });
    new CfnOutput(this, 'DataToolsFunction', { value: dataFn.functionName });
    new CfnOutput(this, 'JobsTable', { value: jobs.tableName });
    new CfnOutput(this, 'BackupBucket', { value: backups.bucketName });
  }
}
