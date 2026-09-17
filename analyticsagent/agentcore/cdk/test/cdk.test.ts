import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AgentCoreStack } from '../lib/cdk-stack';

const emptySpec = {
  name: 'testproject',
  version: 1,
  managedBy: 'CDK' as const,
  runtimes: [],
  memories: [],
  credentials: [],
  evaluators: [],
  onlineEvalConfigs: [],
  configBundles: [],
  policyEngines: [],
  agentCoreGateways: [],
  mcpRuntimeTools: [],
  unassignedTargets: [],
};

test('AgentCoreStack synthesizes with empty spec', () => {
  const app = new cdk.App();
  const stack = new AgentCoreStack(app, 'TestStack', { spec: emptySpec });
  const template = Template.fromStack(stack);
  template.hasOutput('StackNameOutput', {
    Description: 'Name of the CloudFormation Stack',
  });
});

/**
 * `{"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":..."]]}` → `"arn:aws:..."`.
 *
 * Pinning ARNs as whole strings is the point of these tests: the `-*` suffix on the
 * secret and the `/knowledge/*` suffix on the object grant are exactly the characters
 * that decide whether the grant is least-privilege or matches nothing, and a structural
 * matcher on `Fn::Join` fragments hides them. Account and region are literal here
 * because the stack is given an explicit `env`; only the partition stays a pseudo
 * parameter, so that is the one Ref this resolves.
 */
function flatten(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(flatten).join('');
  if (value && typeof value === 'object') {
    const o = value as Record<string, unknown>;
    if (o['Fn::Join']) {
      const [sep, parts] = o['Fn::Join'] as [string, unknown[]];
      return parts.map(flatten).join(sep);
    }
    if (o['Ref'] === 'AWS::Partition') return 'aws';
    return JSON.stringify(value);
  }
  return String(value);
}

// —— exec role 的最小权限策略(B2)——
//
// 这一组断言的意义在于「缺什么」而不是「有什么」:`wireExecutionRole()` 的全部价值是
// exec role **拿不到**数据面权限,而那是个不会报错的属性——多给一条 Allow,synth 照样
// 过、部署照样成,只是 AGENT_ROLE_ARN 从此可有可无。所以这里既钉住四条 Allow 的资源
// 范围,也钉住那条兜底 Deny。
//
// 按 Sid 取语句只在**测试里**可行:cdk.json 开了 `@aws-cdk/aws-iam:minimizePolicies`,
// 它会合并语句并抹掉 Sid,所以真正 `cdk synth` 出来的模板里这些 Sid 是不存在的。
// 测试用的是 `new cdk.App()`,读不到 cdk.json 的 context,Sid 因此保留。
describe('execution-role policy', () => {
  const app = new cdk.App({
    context: { knowledgeBucket: 'kb-test', agentRoleName: 'ro-test' },
  });
  const stack = new AgentCoreStack(app, 'PolicyStack', {
    spec: {
      ...emptySpec,
      runtimes: [
        {
          name: 'analytics',
          build: 'Container',
          entrypoint: 'main.py',
          codeLocation: 'app/analytics/',
          runtimeVersion: 'PYTHON_3_11',
          networkMode: 'PUBLIC',
          protocol: 'HTTP',
        },
      ],
    } as any,
    env: { account: '111122223333', region: 'us-west-2' },
  });
  const template = Template.fromStack(stack).toJSON();

  const policies = Object.entries(template.Resources as Record<string, any>).filter(
    ([id, r]) => r.Type === 'AWS::IAM::Policy' && id.includes('RuntimeExecutionRole')
  );

  test('there is exactly one execution-role policy to reason about', () => {
    expect(policies).toHaveLength(1);
  });

  const statements: any[] = policies[0][1].Properties.PolicyDocument.Statement;
  const bySid = (sid: string): any => {
    const found = statements.filter((s) => s.Sid === sid);
    expect(found).toHaveLength(1);
    return found[0];
  };

  test('reads the runtime-config secret (ARN must end in -*)', () => {
    const s = bySid('ReadRuntimeConfigSecret');
    expect(s.Effect).toBe('Allow');
    expect(s.Action).toBe('secretsmanager:GetSecretValue');
    expect(flatten(s.Resource)).toBe(
      'arn:aws:secretsmanager:us-west-2:111122223333:secret:analytics-agent/runtime-*'
    );
  });

  test('lists the knowledge bucket only under the knowledge/ prefix', () => {
    const s = bySid('ListKnowledgeBucket');
    expect(s.Effect).toBe('Allow');
    expect(s.Action).toBe('s3:ListBucket');
    expect(flatten(s.Resource)).toBe('arn:aws:s3:::kb-test');
    expect(s.Condition).toEqual({ StringLike: { 's3:prefix': ['knowledge/*'] } });
  });

  test('reads the knowledge tree', () => {
    const s = bySid('ReadKnowledgeTree');
    expect(s.Effect).toBe('Allow');
    expect(s.Action).toBe('s3:GetObject');
    expect(flatten(s.Resource)).toBe('arn:aws:s3:::kb-test/knowledge/*');
  });

  test('assumes the governance role, and only that role', () => {
    const s = bySid('AssumeGovernanceRole');
    expect(s.Effect).toBe('Allow');
    expect(s.Action).toBe('sts:AssumeRole');
    expect(flatten(s.Resource)).toBe('arn:aws:iam::111122223333:role/ro-test');
  });

  test('explicitly denies the data plane to the exec role', () => {
    // 显式 Deny 覆盖不了 —— 后面谁再加 Allow 都不生效,所以 AGENT_ROLE_ARN 一直是
    // 拿到数据的唯一路径。少了这条,「exec role 零数据面权限」就只是当下的巧合。
    const s = bySid('DenyDataPlaneToExecRole');
    expect(s.Effect).toBe('Deny');
    expect([...s.Action].sort()).toEqual([
      'athena:*',
      'glue:*',
      'lakeformation:*',
      's3tables:*',
    ]);
    expect(s.Resource).toBe('*');
  });

  test('grants no data-plane action anywhere in the policy', () => {
    // 上面那条 Deny 是兜底;这一条查的是有没有人在别处加了 Allow。两条都要:
    // Deny 在生效,但一条被 Deny 盖住的 Allow 说明有人想要这个权限,那是要看见的。
    const allowed = statements
      .filter((s) => s.Effect === 'Allow')
      .flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
    expect(
      allowed.filter((a: string) => /^(athena|glue|s3tables|lakeformation):/.test(a))
    ).toEqual([]);
  });

  test('emits the governance role ARN for AGENT_ROLE_ARN', () => {
    expect(flatten((template.Outputs as any).GovernanceRoleArnOutput.Value)).toBe(
      'arn:aws:iam::111122223333:role/ro-test'
    );
  });
});
