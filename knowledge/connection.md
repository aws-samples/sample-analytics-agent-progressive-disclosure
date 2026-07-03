# 数据库连接信息

## 本地 Docker 部署（推荐体验）

| 参数 | 值 |
|------|-----|
| Host | `localhost` |
| Port | `5432` |
| Database | `app_analytics` |
| User | `postgres` |
| Password | `postgres` |

### 本地查询脚本

```bash
# 使用本地查询脚本
./scripts/dbquery.sh "SELECT * FROM users LIMIT 5"

# 或直接使用 psql
PGPASSWORD='postgres' psql -h localhost -U postgres -d app_analytics -c "SELECT * FROM users LIMIT 5"

# 或使用 docker exec
docker exec -it app-analytics-db psql -U postgres -d app_analytics -c "SELECT * FROM users LIMIT 5"
```

---

## 云端 RDS 部署

连接信息存储在项目根目录的 `.env` 文件中（不包含在版本控制中）。

参考 `.env.example` 获取配置模板：

| 参数 | 环境变量 |
|------|----------|
| Host | `DB_HOST` |
| Port | `DB_PORT` |
| Database | `DB_NAME` |
| User | `DB_USER` |
| Password | `DB_PASSWORD` |

### 方式 1: 通过 kubectl exec 直接连接

适用于已配置 EKS 集群访问权限的场景：

```bash
# 先加载环境变量
source .env

# 进入 pgweb pod 执行 psql
kubectl exec -it deploy/pgweb -n app-analytics -- psql \
  "host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USER password=$DB_PASSWORD sslmode=require"
```

### 方式 2: 通过 Port-Forward 使用 pgweb

适用于需要图形界面操作的场景：

```bash
# 1. 启动端口转发
kubectl port-forward svc/pgweb 8081:8081 -n app-analytics

# 2. 浏览器访问
# http://localhost:8081

# 3. 在 pgweb 界面输入连接信息（参考 .env 文件）
```

### 方式 3: 本地 psql 直连（需 VPN/堡垒机）

```bash
source .env
PGPASSWORD="$DB_PASSWORD" psql \
  -h "$DB_HOST" \
  -p "$DB_PORT" \
  -U "$DB_USER" \
  -d "$DB_NAME" \
  --set=sslmode=require
```

## 常用命令

```sql
-- 查看所有表
\dt

-- 查看表结构
\d table_name

-- 查看表大小
SELECT relname, pg_size_pretty(pg_total_relation_size(relid))
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC;

-- 查看当前连接数
SELECT count(*) FROM pg_stat_activity WHERE datname = 'app_analytics';
```

## 注意事项

- 数据库部署在 AWS Aurora PostgreSQL，位于 ap-northeast-1 区域
- 生产环境请使用只读副本进行分析查询
- 避免在业务高峰期执行大规模查询
- 查询超时时间默认 30 秒，复杂查询请优化或分批执行
