import * as fs from 'fs';

/** One enterprise MCP server registered on the gateway (e.g. DataHub or Metabase behind MCPHub). */
export interface McpTargetConfig {
  /** Target name; its tools appear to the agent as `<name>___<tool>`. Letters, digits and hyphens. */
  name: string;
  /** Public HTTPS Streamable HTTP endpoint (the gateway has no VPC egress in China regions). */
  endpoint: string;
  description?: string;
  /**
   * none: no outbound auth. iam: SigV4 with the gateway role (MCP servers hosted on AgentCore Runtime: set runtimeArn so
   * the role may invoke it, and region when it is in another region).
   * oauth / apiKey: an AgentCore Identity credential provider you created (providerArn).
   */
  auth: { type: 'none' } | { type: 'iam'; service: string; region?: string; runtimeArn?: string } |
    { type: 'oauth'; providerArn: string; scopes: string[] } |
    { type: 'apiKey'; providerArn: string; parameterName?: string; prefix?: string; location?: 'HEADER' | 'QUERY_PARAMETER' };
}

export interface AppConfig {
  prefix: string;
  region: string;
  quickSight: { namespace: string; allowedAnalysisIds: string[] };
  identityCenter: { samlEntityId: string };
  dataAccess: {
    /** Where the data tool Lambda runs so it can reach the databases behind QuickSight datasets; null = no VPC. */
    vpc: null | { vpcId: string; subnetIds: string[]; securityGroupIds: string[]; createInterfaceEndpoints: boolean };
    datasourceSecretPrefix: string;
    extraSecretArns: string[];
  };
  mcpTargets: McpTargetConfig[];
  runtime: { idleSeconds: number; maxLifetimeSeconds: number };
  api: { rateLimit: number; burstLimit: number };
  frameAncestors: string;
  /** Optional example questions shown in the chat page, replacing the built-in generic ones. */
  examples?: { group: string; tip?: string; questions: string[] }[];
}

export function loadConfig(path: string): AppConfig {
  const config = JSON.parse(fs.readFileSync(path, 'utf-8')) as AppConfig;
  const problems: string[] = [];
  if (!/^[a-z][a-z0-9]{1,11}$/.test(config.prefix)) problems.push('prefix: 2-12 lowercase letters/digits, starting with a letter');
  if (!config.region) problems.push('region is required');
  if (!config.identityCenter?.samlEntityId) problems.push('identityCenter.samlEntityId is required');
  const vpc = config.dataAccess?.vpc;
  if (vpc && (!vpc.vpcId || !vpc.subnetIds?.length)) problems.push('dataAccess.vpc needs vpcId and subnetIds (or set it to null)');
  for (const target of config.mcpTargets ?? []) {
    if (!/^[A-Za-z0-9-]{1,40}$/.test(target.name) || ['analysis', 'data'].includes(target.name)) {
      problems.push(`mcpTargets.name "${target.name}": letters, digits, hyphens; "analysis" and "data" are reserved`);
    }
    if (!target.endpoint?.startsWith('https://')) problems.push(`mcpTargets "${target.name}": endpoint must be https://`);
  }
  const examples = Buffer.byteLength(JSON.stringify(config.examples ?? []), 'utf8');
  if (examples > 2500) problems.push(`examples: ${examples} bytes as JSON; keep under 2500 (Lambda environment variables share a 4 KB limit)`);
  if (problems.length) throw new Error(`config ${path}:\n  - ${problems.join('\n  - ')}`);
  return config;
}
