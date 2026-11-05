// A small, realistic stand-in for the Service Authorization Reference the backend fetches (backend/API.md,
// "AWS access"): real service prefixes and action names, access levels, a few Read actions outside
// ReadOnlyAccess, Secrets Manager and ssm:GetParameter* blocked, and two services with no endpoint mapping.

export type Level = 'List' | 'Read' | 'Write' | 'Permissions' | 'Tagging';
const CODES: Record<string, Level> = { L: 'List', R: 'Read', W: 'Write', P: 'Permissions', T: 'Tagging' };

const SECRETS = 'never enabled here: iron-proxy fetches its own secrets with the same session';
const SSM_PARAMS = "never enabled here: reads SecureString parameters, such as the playground's own origin secret";
const UNMAPPED = "can't be enabled here (no endpoint mapping)";

interface Def {
  id: string;
  name: string;
  signing?: string;
  hosts?: string[];
  unmapped?: boolean;
  /** "Name:L Name:R ..." */
  actions: string;
}

const R = 'us-east-1';
const DEFS: Def[] = [
  { id: 's3', name: 'Amazon S3', hosts: ['*.s3.amazonaws.com', `s3.${R}.amazonaws.com`, `*.s3.${R}.amazonaws.com`], actions: 'ListAllMyBuckets:L ListBucket:L ListBucketVersions:L ListMultipartUploadParts:L ListAccessPoints:L GetObject:R GetObjectVersion:R GetObjectAcl:R GetObjectTagging:R GetObjectAttributes:R GetBucketLocation:R GetBucketPolicy:R GetBucketPolicyStatus:R GetBucketAcl:R GetBucketTagging:R GetBucketVersioning:R GetBucketLogging:R GetEncryptionConfiguration:R GetLifecycleConfiguration:R GetAccessGrantsInstanceForPrefix:R PutObject:W DeleteObject:W CreateBucket:W DeleteBucket:W RestoreObject:W PutBucketPolicy:P PutBucketAcl:P DeleteBucketPolicy:P PutObjectTagging:T PutBucketTagging:T' },
  { id: 'ec2', name: 'Amazon EC2', actions: 'DescribeInstances:L DescribeInstanceStatus:L DescribeInstanceTypes:L DescribeImages:L DescribeVpcs:L DescribeSubnets:L DescribeSecurityGroups:L DescribeSecurityGroupRules:L DescribeRouteTables:L DescribeNatGateways:L DescribeInternetGateways:L DescribeNetworkInterfaces:L DescribeVolumes:L DescribeSnapshots:L DescribeKeyPairs:L DescribeAddresses:L DescribeRegions:L DescribeAvailabilityZones:L DescribeLaunchTemplates:L DescribeLaunchTemplateVersions:L DescribeTags:L DescribeVpcEndpoints:L DescribeFlowLogs:L DescribeAccountAttributes:L DescribeLocalGatewayRouteTableVirtualInterfaceGroupAssociations:L DescribeTransitGatewayPolicyTableAssociations:L DescribeTrafficMirrorFilterRules:L GetConsoleOutput:R GetPasswordData:R GetLaunchTemplateData:R GetEbsEncryptionByDefault:R RunInstances:W StartInstances:W StopInstances:W TerminateInstances:W RebootInstances:W CreateVolume:W AttachVolume:W CreateSnapshot:W CreateVpc:W CreateSubnet:W AuthorizeSecurityGroupIngress:W RevokeSecurityGroupIngress:W ModifyInstanceAttribute:W CreateTags:T DeleteTags:T' },
  { id: 'dynamodb', name: 'Amazon DynamoDB', actions: 'ListTables:L ListBackups:L ListStreams:L ListGlobalTables:L ListTagsOfResource:R DescribeTable:R DescribeTimeToLive:R DescribeContinuousBackups:R DescribeStream:R GetItem:R BatchGetItem:R Query:R Scan:R GetRecords:R GetShardIterator:R PutItem:W UpdateItem:W DeleteItem:W BatchWriteItem:W CreateTable:W DeleteTable:W UpdateTable:W PutResourcePolicy:P DeleteResourcePolicy:P TagResource:T UntagResource:T' },
  { id: 'lambda', name: 'AWS Lambda', actions: 'ListFunctions:L ListVersionsByFunction:L ListAliases:L ListLayers:L ListLayerVersions:L ListEventSourceMappings:L ListTags:R GetFunction:R GetFunctionConfiguration:R GetPolicy:R GetAlias:R GetAccountSettings:R GetFunctionUrlConfig:R GetRuntimeManagementConfig:R InvokeFunction:W CreateFunction:W UpdateFunctionCode:W UpdateFunctionConfiguration:W DeleteFunction:W PublishVersion:W AddPermission:P RemovePermission:P TagResource:T UntagResource:T' },
  { id: 'iam', name: 'AWS Identity and Access Management (IAM)', hosts: ['*.iam.amazonaws.com'], actions: 'ListRoles:L ListUsers:L ListPolicies:L ListAttachedRolePolicies:L ListRolePolicies:L ListGroups:L ListAccessKeys:L ListMFADevices:L GetRole:R GetUser:R GetPolicy:R GetPolicyVersion:R GetRolePolicy:R GetAccountSummary:R GetAccountAuthorizationDetails:R GenerateCredentialReport:R GetCredentialReport:R CreateRole:P DeleteRole:P AttachRolePolicy:P PutRolePolicy:P CreateUser:W CreateAccessKey:W DeleteAccessKey:W UpdateAssumeRolePolicy:P TagRole:T UntagRole:T' },
  { id: 'sts', name: 'AWS Security Token Service', hosts: ['sts.amazonaws.com', `*.sts.${R}.amazonaws.com`], actions: 'GetCallerIdentity:R GetAccessKeyInfo:R GetSessionToken:R DecodeAuthorizationMessage:W AssumeRole:W AssumeRoleWithWebIdentity:W GetFederationToken:R TagSession:T' },
  { id: 'ecs', name: 'Amazon Elastic Container Service', actions: 'ListClusters:L ListServices:L ListTasks:L ListTaskDefinitions:L ListContainerInstances:L ListTagsForResource:R DescribeClusters:R DescribeServices:R DescribeTasks:R DescribeTaskDefinition:R DescribeContainerInstances:R DescribeCapacityProviders:R GetTaskProtection:R RunTask:W StopTask:W UpdateService:W CreateService:W DeleteService:W RegisterTaskDefinition:W ExecuteCommand:W TagResource:T UntagResource:T' },
  { id: 'logs', name: 'Amazon CloudWatch Logs', actions: 'DescribeLogGroups:L DescribeLogStreams:L DescribeMetricFilters:L DescribeQueries:L DescribeSubscriptionFilters:L ListTagsForResource:L GetLogEvents:R FilterLogEvents:R GetQueryResults:R GetLogRecord:R StartLiveTail:R StartQuery:R CreateLogGroup:W CreateLogStream:W PutLogEvents:W DeleteLogGroup:W PutRetentionPolicy:W PutResourcePolicy:P TagResource:T' },
  { id: 'cloudwatch', name: 'Amazon CloudWatch', signing: 'monitoring', hosts: [`*.monitoring.${R}.amazonaws.com`], actions: 'ListMetrics:L ListDashboards:L ListTagsForResource:R DescribeAlarms:R DescribeAlarmHistory:R DescribeAlarmsForMetric:R GetMetricData:R GetMetricStatistics:R GetDashboard:R GetMetricWidgetImage:R PutMetricData:W PutMetricAlarm:W DeleteAlarms:W PutDashboard:W TagResource:T' },
  { id: 'elasticloadbalancing', name: 'Elastic Load Balancing V2', actions: 'DescribeLoadBalancers:R DescribeTargetGroups:R DescribeTargetHealth:R DescribeListeners:R DescribeRules:R DescribeTags:R DescribeLoadBalancerAttributes:R CreateLoadBalancer:W DeleteLoadBalancer:W RegisterTargets:W DeregisterTargets:W ModifyListener:W AddTags:T RemoveTags:T' },
  { id: 'states', name: 'AWS Step Functions', actions: 'ListStateMachines:L ListExecutions:L ListActivities:L DescribeStateMachine:R DescribeExecution:R GetExecutionHistory:R ListTagsForResource:R StartExecution:W StopExecution:W CreateStateMachine:W UpdateStateMachine:W DeleteStateMachine:W TagResource:T' },
  { id: 'es', name: 'Amazon OpenSearch Service', actions: 'ListDomainNames:L ListVersions:L ListTags:R DescribeDomain:R DescribeDomains:R DescribeDomainConfig:R GetCompatibleVersions:R ESHttpGet:R ESHttpHead:R ESHttpPost:W ESHttpPut:W ESHttpDelete:W CreateDomain:W DeleteDomain:W UpdateDomainConfig:W AddTags:T' },
  { id: 'ses', name: 'Amazon SES', signing: 'ses', hosts: [`*.email.${R}.amazonaws.com`], actions: 'ListIdentities:L ListEmailIdentities:L ListConfigurationSets:L GetIdentityVerificationAttributes:R GetSendQuota:R GetSendStatistics:R GetEmailIdentity:R GetAccount:R SendEmail:W SendRawEmail:W CreateEmailIdentity:W DeleteEmailIdentity:W PutEmailIdentityPolicy:P TagResource:T' },
  { id: 'secretsmanager', name: 'AWS Secrets Manager', actions: 'ListSecrets:L ListSecretVersionIds:L DescribeSecret:R GetResourcePolicy:R GetSecretValue:R BatchGetSecretValue:R GetRandomPassword:R CreateSecret:W PutSecretValue:W UpdateSecret:W DeleteSecret:W RotateSecret:W PutResourcePolicy:P DeleteResourcePolicy:P TagResource:T' },
  { id: 'ssm', name: 'AWS Systems Manager', actions: 'DescribeParameters:L DescribeInstanceInformation:L DescribeDocument:R ListDocuments:L ListCommands:L ListTagsForResource:R GetParameter:R GetParameters:R GetParametersByPath:R GetParameterHistory:R GetDocument:R GetCommandInvocation:R GetInventory:R PutParameter:W DeleteParameter:W SendCommand:W StartSession:W AddTagsToResource:T' },
  { id: 'kms', name: 'AWS Key Management Service', actions: 'ListKeys:L ListAliases:L ListGrants:L ListKeyPolicies:L ListResourceTags:R DescribeKey:R GetKeyPolicy:R GetKeyRotationStatus:R GetPublicKey:R Decrypt:W Encrypt:W GenerateDataKey:W ReEncryptFrom:W CreateKey:W ScheduleKeyDeletion:W PutKeyPolicy:P CreateGrant:P TagResource:T' },
  { id: 'sqs', name: 'Amazon SQS', actions: 'ListQueues:L ListDeadLetterSourceQueues:L ListQueueTags:R GetQueueAttributes:R GetQueueUrl:R ReceiveMessage:W SendMessage:W DeleteMessage:W PurgeQueue:W CreateQueue:W DeleteQueue:W SetQueueAttributes:W AddPermission:P RemovePermission:P TagQueue:T' },
  { id: 'sns', name: 'Amazon SNS', actions: 'ListTopics:L ListSubscriptions:L ListSubscriptionsByTopic:L ListTagsForResource:R GetTopicAttributes:R GetSubscriptionAttributes:R Publish:W Subscribe:W Unsubscribe:W CreateTopic:W DeleteTopic:W SetTopicAttributes:W AddPermission:P RemovePermission:P TagResource:T' },
  { id: 'cloudformation', name: 'AWS CloudFormation', actions: 'ListStacks:L ListStackResources:L ListExports:L ListChangeSets:L DescribeStacks:R DescribeStackResources:R DescribeStackEvents:R DescribeChangeSet:R GetTemplate:R GetTemplateSummary:R CreateStack:W UpdateStack:W DeleteStack:W ExecuteChangeSet:W SetStackPolicy:P TagResource:T' },
  { id: 'ecr', name: 'Amazon Elastic Container Registry', hosts: [`*.api.ecr.${R}.amazonaws.com`], actions: 'DescribeRepositories:R DescribeImages:R ListImages:L ListTagsForResource:R BatchGetImage:R GetDownloadUrlForLayer:R GetAuthorizationToken:R GetRepositoryPolicy:R PutImage:W InitiateLayerUpload:W CreateRepository:W DeleteRepository:W SetRepositoryPolicy:P TagResource:T' },
  { id: 'route53', name: 'Amazon Route 53', hosts: ['*.route53.amazonaws.com'], actions: 'ListHostedZones:L ListResourceRecordSets:L ListHealthChecks:L GetHostedZone:R GetHealthCheck:R GetChange:R ChangeResourceRecordSets:W CreateHostedZone:W DeleteHostedZone:W ChangeTagsForResource:T' },
  { id: 'cloudtrail', name: 'AWS CloudTrail', actions: 'ListTrails:L LookupEvents:R DescribeTrails:R GetTrailStatus:R GetEventSelectors:R CreateTrail:W DeleteTrail:W StartLogging:W StopLogging:W AddTags:T' },
  { id: 'kinesis', name: 'Amazon Kinesis Data Streams', actions: 'ListStreams:L ListShards:L DescribeStream:R DescribeStreamSummary:R GetRecords:R GetShardIterator:R PutRecord:W PutRecords:W CreateStream:W DeleteStream:W AddTagsToStream:T' },
  { id: 'cloudshell', name: 'AWS CloudShell', unmapped: true, actions: 'DescribeEnvironments:R GetEnvironmentStatus:R CreateEnvironment:W StartEnvironment:W StopEnvironment:W PutCredentials:W' },
  { id: 'a4b', name: 'Alexa for Business', unmapped: true, actions: 'ListSkills:L ListTags:L GetDevice:R SearchRooms:R CreateRoom:W DeleteRoom:W TagResource:T' },
];

// ---------------------------------------------------------------- roles (part 2's four sandbox roles)

export type RoleId = 'view-only' | 'read-only' | 'data-reader' | 'builder';
export const ROLES: { id: RoleId; description: string; default: boolean; warning: string | null; policies: string[] }[] = [
  { id: 'view-only', description: 'AWS ViewOnlyAccess: list and describe resources, no data reads.', default: true, warning: null, policies: ['ViewOnlyAccess', 'inline: sandbox-guards'] },
  { id: 'read-only', description: 'AWS ReadOnlyAccess: reads, including object and item data.', default: false, warning: null, policies: ['ReadOnlyAccess', 'inline: sandbox-guards'] },
  { id: 'data-reader', description: 'Reads the demo S3 bucket and DynamoDB tables my2cents-demo-*; nothing else.', default: false, warning: null, policies: ['inline: sandbox-guards', 'inline: demo-data'] },
  { id: 'builder', description: 'AWS PowerUserAccess in us-east-1, without changes to the workspace’s own stack.', default: false, warning: 'Can create billable resources in this account.', policies: ['PowerUserAccess', 'inline: sandbox-guards', 'inline: builder-guardrails'] },
];
export const DEFAULT_ROLE: RoleId = 'view-only';
export function roleOf(v: string | null | undefined): RoleId | null {
  if (!v) return DEFAULT_ROLE;
  return (ROLES.find((r) => r.id === v)?.id ?? null) as RoleId | null;
}

/** Read or List actions that ReadOnlyAccess doesn't grant (as the real evaluation finds). */
const OUTSIDE_READ_ONLY = new Set([
  's3:GetAccessGrantsInstanceForPrefix', 'ec2:GetPasswordData', 'lambda:GetRuntimeManagementConfig',
  'ecs:GetTaskProtection', 'logs:StartLiveTail', 'ecr:GetAuthorizationToken', 'sts:GetFederationToken',
  'sts:GetSessionToken', 'es:ESHttpGet', 'es:ESHttpHead', 'kinesis:GetRecords',
]);
/** Reads of data, which ViewOnlyAccess leaves out. */
const DATA_READS = /^(s3:GetObject|dynamodb:(GetItem|BatchGetItem|Query|Scan)|logs:(GetLogEvents|FilterLogEvents|GetQueryResults)|kinesis:GetRecords|sqs:ReceiveMessage|lambda:GetFunction$|ecr:(BatchGetImage|GetDownloadUrlForLayer)|cloudtrail:LookupEvents|ssm:GetDocument)/;
/** data-reader: its inline policy only (scoped to one bucket and the demo tables, so "limited"). */
const DATA_READER = /^(s3:(GetObject|GetObjectVersion|ListBucket)|dynamodb:(GetItem|BatchGetItem|Query|Scan|DescribeTable|ConditionCheckItem))$/;
const BUILDER_DENIED: Record<string, string> = {
  'sts:AssumeRole': 'NoAssumeRole',
  'sts:GetFederationToken': 'NoAssumeRole',
};

export interface MAction {
  action: string;
  service: string;
  name: string;
  level: Level;
}

export interface MService {
  id: string;
  name: string;
  signing: string[];
  hosts: string[];
  enabled: boolean;
  reason: string | null;
  actions: MAction[];
}

export interface Verdict {
  allowed: boolean;
  limited: boolean;
  reason: string | null;
}

/** What the backend's local evaluation of the role's policies says about one action. */
export function evaluate(a: MAction, s: MService, role: RoleId): Verdict {
  const no = (reason: string): Verdict => ({ allowed: false, limited: false, reason });
  if (!s.enabled) return no(UNMAPPED);
  if (a.service === 'secretsmanager') return no(SECRETS);
  if (a.service === 'ssm' && a.name.startsWith('GetParameter')) return no(SSM_PARAMS);
  const outside = no(`outside the ${role} role's permissions`);
  switch (role) {
    case 'view-only':
      if (a.level === 'List') return { allowed: true, limited: false, reason: null };
      if (a.level === 'Read' && /^(Describe|List|Get)/.test(a.name) && !DATA_READS.test(a.action) && !OUTSIDE_READ_ONLY.has(a.action)) {
        return { allowed: true, limited: false, reason: null };
      }
      return outside;
    case 'read-only':
      if ((a.level === 'List' || a.level === 'Read') && !OUTSIDE_READ_ONLY.has(a.action)) return { allowed: true, limited: false, reason: null };
      return outside;
    case 'data-reader':
      return DATA_READER.test(a.action) ? { allowed: true, limited: true, reason: null } : outside;
    case 'builder':
      if (BUILDER_DENIED[a.action]) return no(`denied by the builder role (${BUILDER_DENIED[a.action]})`);
      if (a.service === 'iam' && a.level !== 'List' && a.level !== 'Read') return outside;
      return { allowed: true, limited: false, reason: null };
  }
}

export const SERVICES: MService[] = DEFS.map((d) => {
  const actions = d.actions.split(/\s+/).filter(Boolean).map((tok) => {
    const [name, code] = tok.split(':');
    return { action: `${d.id}:${name}`, service: d.id, name, level: CODES[code] ?? 'Read' };
  });
  return {
    id: d.id,
    name: d.name,
    signing: [d.signing ?? d.id],
    hosts: d.unmapped ? [] : (d.hosts ?? [`*.${d.id}.${R}.amazonaws.com`]),
    enabled: !d.unmapped,
    reason: d.unmapped ? UNMAPPED : null,
    actions: actions.sort((a, b) => a.name.localeCompare(b.name)),
  };
}).sort((a, b) => a.id.localeCompare(b.id));

export const BY_ID = new Map(SERVICES.map((s) => [s.id, s]));
const BY_ACTION = new Map(SERVICES.flatMap((s) => s.actions.map((a) => [a.action.toLowerCase(), [a, s] as const])));
export const TOTAL_ACTIONS = SERVICES.reduce((n, s) => n + s.actions.length, 0);

const blockedAction = (a: MAction) => a.service === 'secretsmanager' || (a.service === 'ssm' && a.name.startsWith('GetParameter'));
/** Can go in a session policy: mapped and not blocked (outside the role is fine: no effect). */
const pickableAction = (a: MAction, s: MService) => s.enabled && !blockedAction(a);

export function actionsFor(s: MService, role: RoleId) {
  return s.actions.map((a) => {
    const v = evaluate(a, s, role);
    // sts:GetCallerIdentity needs no permission
    const always = a.action === 'sts:GetCallerIdentity';
    return { ...a, ...v, allowed: always || v.allowed, reason: always ? null : v.reason, pickable: pickableAction(a, s) };
  });
}

const VERBS: [string, string][] = [['', 'Every action'], ['List', 'Every List action'], ['Describe', 'Every Describe action'], ['Get', 'Every Get action']];

type Covered = ReturnType<typeof actionsFor>;

/** Same as the backend's coverage_of: how many of the covered actions are effective, and why not the rest. */
export function coverageOf(covered: Covered, role: RoleId) {
  const eff = covered.filter((a) => a.allowed);
  const blk = covered.filter((a) => blockedAction(a));
  const outside = covered.length - eff.length - blk.length;
  const parts: string[] = [];
  if (outside) parts.push(covered.length > 1 ? `${outside} outside the ${role} role` : `no effect with ${role}: ${covered.find((a) => !a.allowed)?.reason ?? ''}`);
  if (blk.length) parts.push(`${blk.length} never enabled here (${blk.slice(0, 2).map((a) => a.action).join(', ')}${blk.length > 2 ? ', ...' : ''}), denied in the session policy`);
  const tone = covered.length && eff.length === covered.length ? 'all' : eff.length === 0 ? 'none' : 'partial';
  return {
    covers: covered.length,
    effective: eff.length,
    limited: eff.filter((a) => a.limited).length,
    blocked: blk.length,
    tone,
    reason: parts.length ? `${covered.length > 1 ? `${eff.length} of ${covered.length} effective: ` : ''}${parts.join('; ')}` : null,
  };
}

export function wildcards(s: MService, role: RoleId) {
  const acts = actionsFor(s, role);
  return VERBS.flatMap(([verb, label]) => {
    if (!verb && s.id === 'secretsmanager') return [];
    const covered = acts.filter((a) => a.name.startsWith(verb));
    if (!covered.length) return [];
    const pickable = s.enabled && s.id !== 'secretsmanager';
    const cov = coverageOf(covered, role);
    const reason = !s.enabled ? UNMAPPED : s.id === 'secretsmanager' ? SECRETS : cov.reason;
    return [{
      action: `${s.id}:${verb}*`,
      label,
      covers: covered.length,
      effective: pickable ? cov.effective : 0,
      tone: pickable ? cov.tone : 'none',
      pickable,
      allowed: pickable && cov.effective === covered.length,
      limited: pickable && cov.limited > 0,
      reason,
      covered,
    }];
  });
}

/** What a pick covers, or a refusal (the backend's 422): unknown, blocked, unmapped. */
export function resolve(action: string, role: RoleId): { covered: Covered; wildcard: boolean } | string {
  const hit = BY_ACTION.get(action.toLowerCase());
  if (hit) {
    const a = actionsFor(hit[1], role).find((x) => x.action === hit[0].action)!;
    return a.pickable ? { covered: [a], wildcard: false } : `${action} (${a.reason})`;
  }
  const [svc, rest] = action.split(':');
  const s = BY_ID.get(svc);
  if (s && rest && rest.endsWith('*')) {
    const w = wildcards(s, role).find((x) => x.action === action);
    if (w) return w.pickable ? { covered: w.covered, wildcard: true } : `${action} (${w.reason})`;
  }
  return `${action}: not in the Service Authorization Reference`;
}

export function summary(s: MService, role: RoleId) {
  const levels: Partial<Record<Level, number>> = {};
  for (const a of s.actions) levels[a.level] = (levels[a.level] ?? 0) + 1;
  return {
    id: s.id,
    name: s.name,
    actions: s.actions.length,
    allowed: actionsFor(s, role).filter((a) => a.allowed).length,
    levels,
    enabled: s.enabled,
    pickable: s.enabled && s.id !== 'secretsmanager',
    reason: s.reason,
    signing_names: s.signing,
  };
}

export function search(qRaw: string, level: Level | null, limit: number, role: RoleId) {
  const q = qRaw.trim().toLowerCase();
  const all = SERVICES.flatMap((s) => actionsFor(s, role));
  const hit = (a: MAction) => !q || a.name.toLowerCase().includes(q) || a.action.toLowerCase().includes(q);
  const rank = (a: MAction) => {
    if (!q) return 2;
    const n = a.name.toLowerCase();
    const full = a.action.toLowerCase();
    if (n === q || full === q) return 0;
    if (n.startsWith(q) || full.startsWith(q)) return 1;
    return 2;
  };
  const actions = all.filter((a) => (!level || a.level === level) && hit(a));
  actions.sort((a, b) => rank(a) - rank(b) || Number(!a.allowed) - Number(!b.allowed) || a.service.localeCompare(b.service) || a.name.localeCompare(b.name));
  const perService = new Map<string, number>();
  for (const a of actions) perService.set(a.service, (perService.get(a.service) ?? 0) + 1);
  const services = SERVICES.filter((s) => (q && (s.id.includes(q) || s.name.toLowerCase().includes(q))) || perService.has(s.id)).map((s) => ({
    id: s.id,
    name: s.name,
    enabled: s.enabled,
    matches: perService.get(s.id) ?? 0,
  }));
  return { q: qRaw, level, total: actions.length, truncated: actions.length > limit, services, actions: actions.slice(0, limit) };
}

// ---------------------------------------------------------------- "allow *": the explicit lists

/**
 * With no session policy the backend writes every signing name and host the catalog can map, minus Secrets Manager.
 * The mock catalog is small, so pad it with real AWS endpoint prefixes to get list lengths like the real ones.
 */
const MORE = (
  'access-analyzer account acm acm-pca airflow amplify apigateway appconfig appflow application-autoscaling ' +
  'applicationinsights appmesh apprunner appstream appsync aps athena auditmanager autoscaling autoscaling-plans backup ' +
  'batch bedrock billingconductor braket budgets ce chatbot chime cleanrooms cloud9 cloudcontrolapi clouddirectory ' +
  'cloudhsm cloudsearch cloudshell codeartifact codebuild codecommit codeconnections codedeploy codeguru-profiler ' +
  'codeguru-reviewer codepipeline codestar-connections codestar-notifications cognito-identity cognito-idp cognito-sync ' +
  'comprehend comprehendmedical compute-optimizer config connect controltower cur databrew dataexchange datapipeline ' +
  'datasync datazone dax deadline detective devicefarm devops-guru directconnect discovery dlm dms docdb-elastic drs ds ' +
  'ebs ec2-instance-connect ecr-public eks elasticache elasticbeanstalk elasticfilesystem elasticloadbalancing ' +
  'elasticmapreduce elastictranscoder emr-containers emr-serverless entityresolution es events evidently finspace ' +
  'firehose fis fms forecast frauddetector fsx gamelift geo glacier globalaccelerator glue grafana greengrass groundstation ' +
  'guardduty health healthlake identitystore imagebuilder inspector inspector2 internetmonitor iot iotevents iotfleetwise ' +
  'iotsitewise iottwinmaker ivs kafka kafkaconnect kendra kinesis kinesisanalytics kinesisvideo lakeformation ' +
  'launchwizard lex license-manager lightsail logs lookoutequipment m2 machinelearning macie2 managedblockchain ' +
  'mediaconnect mediaconvert medialive mediapackage mediastore mediatailor memorydb mgn migrationhub monitoring mq ' +
  'neptune-graph network-firewall networkmanager notifications oam omics opensearch organizations osis outposts ' +
  'payment-cryptography personalize pi pipes polly pricing proton qbusiness quicksight ram rbin rds rds-data redshift ' +
  'redshift-data redshift-serverless rekognition resiliencehub resource-explorer-2 resource-groups robomaker rolesanywhere ' +
  'route53 route53-recovery-control-config route53domains route53resolver rum s3-outposts s3express sagemaker savingsplans ' +
  'scheduler schemas sdb securityhub securitylake serverlessrepo servicecatalog servicediscovery servicequotas ses shield ' +
  'signer simspaceweaver sms-voice snowball sns sqs ssm-contacts ssm-incidents sso states storagegateway support swf ' +
  'synthetics textract timestream transcribe transfer translate trustedadvisor verifiedpermissions voiceid vpc-lattice ' +
  'waf wafv2 wellarchitected wisdom workdocs workmail workspaces xray'
).split(/\s+/);

export function openLists(role: RoleId = DEFAULT_ROLE): { allowed_services: string[]; hosts: string[] } {
  const signing = new Set<string>();
  const hosts = new Set<string>();
  for (const s of SERVICES) {
    if (!s.enabled || s.id === 'secretsmanager' || !actionsFor(s, role).some((a) => a.allowed)) continue;
    s.signing.forEach((n) => signing.add(n));
    s.hosts.forEach((h) => hosts.add(h));
  }
  for (const id of MORE) {
    if (id === 'secretsmanager') continue;
    signing.add(id);
    hosts.add(`*.${id}.${R}.amazonaws.com`);
  }
  return { allowed_services: [...signing].sort(), hosts: [...hosts].sort() };
}
