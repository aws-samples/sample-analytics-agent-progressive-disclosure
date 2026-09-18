import {
  AgentCoreApplication,
  AgentCoreMcp,
  type AgentCoreProjectSpec,
  type AgentCoreMcpSpec,
} from '@aws/agentcore-cdk';
import { CfnOutput, Stack, aws_iam as iam, type StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface HarnessConfig {
  name: string;
  executionRoleArn?: string;
  memoryName?: string;
  containerUri?: string;
  hasDockerfile?: boolean;
  dockerfile?: string;
  codeLocation?: string;
  tools?: { type: string; name: string }[];
  apiKeyArn?: string;
}

export interface AgentCoreStackProps extends StackProps {
  /**
   * The AgentCore project specification containing agents, memories, and credentials.
   */
  spec: AgentCoreProjectSpec;
  /**
   * The MCP specification containing gateways and servers.
   */
  mcpSpec?: AgentCoreMcpSpec;
  /**
   * Credential provider ARNs from deployed state, keyed by credential name.
   */
  credentials?: Record<string, { credentialProviderArn: string; clientSecretArn?: string }>;
  /**
   * Harness role configurations. Each entry creates an IAM execution role for a harness.
   *
   * When `hasDockerfile` is true and `codeLocation` is provided (without an explicit
   * `containerUri`), the L3 construct builds and pushes a container image via CodeBuild
   * and emits its URI as a stack output for the post-CDK harness deployer.
   */
  harnesses?: HarnessConfig[];
}

/**
 * CDK Stack that deploys AgentCore infrastructure.
 *
 * This is a thin wrapper that instantiates L3 constructs.
 * All resource logic and outputs are contained within the L3 constructs.
 */
export class AgentCoreStack extends Stack {
  /** The AgentCore application containing all agent environments */
  public readonly application: AgentCoreApplication;

  constructor(scope: Construct, id: string, props: AgentCoreStackProps) {
    super(scope, id, props);

    const { spec, mcpSpec, credentials, harnesses } = props;

    // Create AgentCoreApplication with all agents and harness roles
    this.application = new AgentCoreApplication(this, 'Application', {
      spec,
      harnesses: harnesses?.length ? (harnesses as any) : undefined,
    });

    // Create AgentCoreMcp if there are gateways configured
    if (mcpSpec?.agentCoreGateways && mcpSpec.agentCoreGateways.length > 0) {
      new AgentCoreMcp(this, 'Mcp', {
        projectName: spec.name,
        mcpSpec,
        agentCoreApplication: this.application,
        credentials,
        projectTags: spec.tags,
      });
    }

    this.wireExecutionRole(spec.runtimes?.length ?? 0);

    // Stack-level output
    new CfnOutput(this, 'StackNameOutput', {
      description: 'Name of the CloudFormation Stack',
      value: this.stackName,
    });
  }

  /**
   * Least-privilege inline policy for the Runtime execution role.
   *
   * This used to live only in `docs/deployment.md` as three bullet points under
   * "部署完之后手工补三样". Prose is not a deployment: the role really was created by
   * `agentcore deploy` with no data-plane grants and then patched by hand, so every
   * re-deploy from a clean account started with a Runtime that boots and cannot answer,
   * and nothing anywhere asserted the shape of the policy. Encoding it here makes the
   * grant a reviewable artifact and makes the *absence* of Athena/Glue permissions
   * an enforced invariant instead of an accident of history.
   *
   * Three grants, and deliberately nothing else:
   *
   *  1. `secretsmanager:GetSecretValue` on the runtime-config secret. `runtime_config.py`
   *     reads it at import time and raises if it cannot — see the "为什么这里要硬失败"
   *     note there for what a silent failure cost us.
   *  2. Read on the knowledge bucket (`ListBucket` scoped by prefix + `GetObject` under
   *     `knowledge/`). `read_doc` serves progressive disclosure out of the local copy
   *     synced from here; an empty sync means the agent guesses column names.
   *  3. `sts:AssumeRole` on the governance role, and that is the **only** path to data.
   *
   * Bedrock model access is intentionally not repeated here: the L3 construct grants it
   * from the agent spec, and a second hand-written copy would drift (the global
   * cross-region inference profile ARN is exactly the kind of string that drifts).
   *
   * The trailing explicit Deny is the point of the whole method. "The exec role has no
   * data-plane permissions" is only true as long as nobody adds any — and adding one is
   * a one-line, plausible-looking mistake (a managed policy, a `.json` picked up by
   * `attachAdditionalPolicies`, a debugging grant left behind). If Athena/Glue/S3
   * Tables/Lake Formation were reachable with exec-role credentials, `AGENT_ROLE_ARN`
   * would stop being load-bearing: a container that failed to assume the governance role
   * would still answer questions, just without any column-level boundary. An explicit
   * Deny cannot be overridden by any later Allow, so the invariant survives edits by
   * people who have not read this comment. It does **not** restrict the agent's queries:
   * the assumed-role session is a different principal with its own policy (written by
   * `scripts/lakehouse/governance.py`), evaluated separately.
   *
   * Resource coordinates come from CDK context so a fork can override them without
   * editing this file, e.g.:
   *   cdk deploy -c knowledgeBucket=my-bucket -c agentRoleName=my-ro-role
   */
  private wireExecutionRole(declaredRuntimes: number): void {
    const ctx = (k: string, fallback: string): string =>
      (this.node.tryGetContext(k) as string | undefined) || fallback;

    const secretName = ctx('runtimeSecretName', 'analytics-agent/runtime');
    const knowledgeBucket = ctx('knowledgeBucket', 'analytics-agent-knowledge');
    const knowledgePrefix = ctx('knowledgePrefix', 'knowledge/');
    const agentRoleName = ctx('agentRoleName', 'analytics-agent-ro');

    const statements = [
      new iam.PolicyStatement({
        sid: 'ReadRuntimeConfigSecret',
        actions: ['secretsmanager:GetSecretValue'],
        // Secrets Manager appends a random 6-character suffix to the ARN, so the
        // resource has to end in `-*`. Naming the bare secret name matches nothing.
        resources: [
          `arn:${this.partition}:secretsmanager:${this.region}:${this.account}:secret:${secretName}-*`,
        ],
      }),
      new iam.PolicyStatement({
        sid: 'ListKnowledgeBucket',
        actions: ['s3:ListBucket'],
        resources: [`arn:${this.partition}:s3:::${knowledgeBucket}`],
        // Without this condition the role can enumerate the whole bucket. It is one
        // bucket today, but "list only what you may read" is the shape that stays
        // correct when someone parks something else in it.
        conditions: { StringLike: { 's3:prefix': [`${knowledgePrefix}*`] } },
      }),
      new iam.PolicyStatement({
        sid: 'ReadKnowledgeTree',
        actions: ['s3:GetObject'],
        resources: [`arn:${this.partition}:s3:::${knowledgeBucket}/${knowledgePrefix}*`],
      }),
      new iam.PolicyStatement({
        sid: 'AssumeGovernanceRole',
        actions: ['sts:AssumeRole'],
        resources: [`arn:${this.partition}:iam::${this.account}:role/${agentRoleName}`],
      }),
      new iam.PolicyStatement({
        sid: 'DenyDataPlaneToExecRole',
        effect: iam.Effect.DENY,
        actions: ['athena:*', 'glue:*', 's3tables:*', 'lakeformation:*'],
        resources: ['*'],
      }),
    ];

    // `runtimes` in agentcore.json, not `harnesses` — this project has no harnesses,
    // so `application.harnessRoles` is empty and the exec role lives on the runtime.
    // Iterate rather than looking up 'analytics' by name: renaming the runtime should
    // not silently stop granting.
    //
    // Runtimes declared but no environments built means the L3 construct's shape moved
    // under us (an alpha dependency), and the failure mode is silent: synth succeeds,
    // deploys a Runtime, and the role has none of the grants below. Fail synth instead.
    // A spec with **zero** runtimes is not that — there is legitimately nothing to
    // attach to (the unit test synthesizes exactly that), so leave it alone.
    if (declaredRuntimes > 0 && this.application.environments.size === 0) {
      throw new Error(
        `Spec declares ${declaredRuntimes} runtime(s) but AgentCoreApplication built no ` +
          'environments, so the execution-role policy has nothing to attach to. The ' +
          '@aws/agentcore-cdk construct API likely changed.'
      );
    }
    for (const env of this.application.environments.values()) {
      // addToPolicy warns at synth time (it cannot mutate) when the role was imported
      // via `executionRoleArn` — in that case the grants above must already be on it.
      for (const s of statements) env.runtime.addToPolicy(s);
    }

    new CfnOutput(this, 'GovernanceRoleArnOutput', {
      description:
        'Governance role the Runtime assumes to query data (AGENT_ROLE_ARN). Its trust ' +
        'policy must list the execution role — governance.py --apply merges it in.',
      value: `arn:${this.partition}:iam::${this.account}:role/${agentRoleName}`,
    });
  }
}
