// 这是**参考样式，不是要传的文件**。线上那份 config.js 由
// scripts/deploy/deploy_web.sh 在部署期从 foundation 栈的输出现算并直接传进 S3
// (生成物落 $TMPDIR，故意不落 web/config.js —— 那个路径一存在就会把线上配置
// 喂给本地页面，见 web/index.html 第 11-14 行的本地分流逻辑)。
//
// 下面两个 ID 是**占位符**。这里原来放的是一对真实格式的旧 ID，池早就删了，
// 于是它同时具备"看着像能用"和"用了必挂"两个属性;真要手填就从
//   aws cloudformation describe-stacks --stack-name analytics-agent-foundation \
//     --query "Stacks[0].Outputs"
// 的 UserPoolId / UserPoolClientId 取。Cognito 池/客户端是公开值(浏览器登录必需)。
//
// /ask 走同域 CloudFront(VPC origin → 内网 ALB → Fargate 中继),无需 askUrl/identityPoolId。
window.APP_CONFIG={
  authEnabled:true,
  region:"us-west-2",
  userPoolId:"us-west-2_XXXXXXXXX",
  clientId:"XXXXXXXXXXXXXXXXXXXXXXXXXX",
  // 同域相对路径:让 boot() 短路进实时模式(后端常驻,不做 2.5s 网络探针,
  // 高延迟网络下探针超时会被误判成离线 demo)。请求本身仍走 fetch(API+'/ask')。
  askUrl:"/ask"
};
