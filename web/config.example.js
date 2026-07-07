// 部署期生成:前端运行配置。Cognito 池/客户端为公开值(浏览器登录所需)。
// /ask 走同域 CloudFront(VPC origin → 内网 ALB → Fargate 中继),无需 askUrl/identityPoolId。
window.APP_CONFIG={
  authEnabled:true,
  region:"us-west-2",
  userPoolId:"us-west-2_78c34V4yC",
  clientId:"1pqj1m92r66f0ei79oshhpdesg",
  // 同域相对路径:让 boot() 短路进实时模式(后端常驻,不做 2.5s 网络探针,
  // 高延迟网络下探针超时会被误判成离线 demo)。请求本身仍走 fetch(API+'/ask')。
  askUrl:"/ask"
};
