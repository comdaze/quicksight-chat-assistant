#!/usr/bin/env node
import * as path from 'path';
import { App } from 'aws-cdk-lib';
import { loadConfig } from '../lib/config';
import { ChatAssistantStack } from '../lib/stack';

const app = new App();
const configPath = path.resolve(app.node.tryGetContext('config') ?? path.join(__dirname, '..', '..', 'config.json'));
const config = loadConfig(configPath);
const account = process.env.CDK_DEFAULT_ACCOUNT;
new ChatAssistantStack(app, `${config.prefix}-chat-assistant`, config, {
  env: { account, region: config.region },
  description: 'QuickSight chat assistant: chat panel for any QuickSight analysis (AgentCore Runtime + Gateway)',
});
