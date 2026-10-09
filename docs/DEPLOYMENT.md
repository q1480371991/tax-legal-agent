# 🚀 部署与运行说明

本文对应 [tax-legal-agent](https://github.com/q1480371991/tax-legal-agent) 当前源码，覆盖后端依赖、数据库初始化、前端启动与可选服务。技术架构与设计原理见 [项目 README](../README.md)。

> 本指南采用当前仓库源码构建。Compose、环境模板与数据库快照存在需要按部署环境处理的差异，请依次完成配置核对、数据库准备与服务启动。

## 📋 部署组成

后端 Compose 位于 [rag_backend/docker-compose.yml](../rag_backend/docker-compose.yml)，前端单独启动。

| 服务 | Compose 名称 | 默认宿主机端口 | 用途 |
|------|--------------|----------------|------|
| PostgreSQL 16 + pgvector | `db` | 5432 | 业务数据、向量与全文索引 |
| Redis 7 | `redis` | 6379 | 缓存、队列与会话相关状态 |
| Neo4j 5.15 Community | `neo4j` | 7474 / 7687 | 图谱管理与关系检索 |
| MinIO | `minio` | 9000 / 9001 | 文件存储 / 管理控制台 |
| FastAPI 后端 | `backend` | 8000 | API、智能体与文档处理 |
| PgBouncer | `pgbouncer` | 6432 | 可选数据库连接池 |
| Unstructured API | `unstructured-api` | 8001 | 可选文档解析服务，需启用 profile |
| Vue 3 前端 | 不在后端 Compose 中 | 5500 | Vite 本地开发服务 |

**准备工具**：Git、Docker Engine 与 Docker Compose v2；Windows 可使用启用 Linux 容器的 Docker Desktop。运行 PgBouncer 配置渲染脚本需本机 Python 3.11 或兼容版本，它只使用标准库；运行前端需 Node.js 与 npm，前端 Dockerfile 的构建环境为 Node.js 20。后端 Python 依赖及系统库由后端 Dockerfile 构建。

Compose 对多个服务设置了内存上限，基础服务限额合计约 5 GB，另有 PgBouncer 的 128 MB 限额；可选 Unstructured 额外限制为 4 GB；这不是测得的最低运行需求，请给宿主机和构建过程预留空间。后端当前还会依赖 Neo4j 服务健康，即使图谱功能开关为 false，也不能直接跳过该容器而保持原有依赖配置。

文中的命令示例使用 **PowerShell 7**。每个脚本块设置 `$ErrorActionPreference = 'Stop'`；关键外部命令另外检查退出码。Linux 容器内部启动流程由仓库现有 Dockerfile 和入口脚本负责。

## 📦 1. 获取当前仓库与配置模板

在用于运行项目的电脑执行：

```powershell
$ErrorActionPreference = 'Stop'
git clone https://github.com/q1480371991/tax-legal-agent.git
if ($LASTEXITCODE -ne 0) { throw '克隆仓库失败' }
Set-Location -LiteralPath './tax-legal-agent/rag_backend'

if (Test-Path -LiteralPath '.env') { throw '.env 已存在，请保留并手动检查现有配置' }
Get-Content -LiteralPath '.env.example' -Raw -Encoding UTF8 |
    Set-Content -LiteralPath '.env' -Encoding UTF8
```

编辑新建的 `rag_backend/.env`，替换占位值。不要将真实密码、API Key、数据库备份或渲染后的 PgBouncer 配置提交到 Git。

后端采用当前仓库的 [Dockerfile](../rag_backend/Dockerfile) 从源码构建，确保镜像与当前代码保持一致。

## 🔑 2. 设置后端必要配置

### 基础服务与认证

| 配置项 | 容器部署时的要求 |
|--------|------------------|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | 设置数据库账户、独立密码与数据库名；导入命令使用相同值 |
| `POSTGRES_SERVER` | 改为 `db`；模板中的 `localhost` 在后端容器内指向后端自身 |
| `POSTGRES_PORT` | 按当前 Compose 保持 `5432` |
| `REDIS_PASSWORD` / `REDIS_PORT` / `REDIS_DB` | 填写独立密码，端口保持 `6379`，通常使用 DB `0` |
| `NEO4J_PASSWORD` | 填写独立密码；Compose 已把后端图谱地址设置为 `bolt://neo4j:7687` |
| `SECRET_KEY` | 设置至少 32 字符的随机密钥，用于 JWT |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | 与 MinIO 服务端实际凭据保持一致，见下方说明 |
| `MINIO_PUBLIC_ENDPOINT` | 设置成浏览器可达的对象存储地址；本地通常为 `127.0.0.1:9000` |
| `PGBOUNCER_ENABLED` | 初次部署可先保持 `false`，使用数据库直连 |

**MinIO 配置差异**：当前 Compose 的 `minio.environment` 固定写了默认账户与密码，`.env.example` 的 `MINIO_SECRET_KEY` 却是待替换值。因此，**只修改 .env 不会同步改变 MinIO 服务端密码**。部署时应将 Compose 对应两项改为读取同一组配置，再在 .env 填写自己的凭据：

```yaml
# rag_backend/docker-compose.yml 中 minio 服务的 environment
MINIO_ROOT_USER: ${MINIO_ACCESS_KEY}
MINIO_ROOT_PASSWORD: ${MINIO_SECRET_KEY}
```

这是一项部署者需要执行的配置调整，仓库当前配置尚未自动完成。不要将默认凭据用于公开环境。后端容器访问 MinIO 使用 Compose 设置的 `minio:9000`；浏览器访问地址不能使用这个容器内部名称。

**端口差异**：当前 Compose 把 `POSTGRES_PORT` 和 `REDIS_PORT` 同时用于宿主机映射与后端连接。若宿主机端口冲突，应分别调整映射和容器连接端口，不能只把 .env 中两项改成新宿主机端口。首次部署可保留 5432 / 6379，避免混淆。

### 对话、向量与重排模型

`LLM_PROVIDER` 决定环境默认对话模型；配置所选供应商的 API Key、模型名与 Base URL。直接使用供应商平台与使用 OpenRouter 时，**密钥、Base URL 和模型标识必须属于同一接入方式**，不要只替换 API Key。

| 能力 | 配置 | 是否需要 |
|------|------|----------|
| 对话与专家推理 | `LLM_PROVIDER` + 所选供应商的 `*_API_KEY` / `*_MODEL` / `*_BASE_URL` | 使用智能问答与专家协作时需要 |
| 文档与查询向量化 | `EMBEDDING_PROVIDER` + 对应供应商配置 | 使用知识库上传与向量检索时需要 |
| 重排 | `ENABLE_RERANK`、`SILICONFLOW_RERANK_MODEL` 及对应密钥 | 可选；未配置时可先关闭 |
| 本地 Ollama | `OLLAMA_BASE_URL`、相应模型名 | 可选；容器不能通过 `localhost` 访问宿主机 Ollama |

环境模板默认 `EMBEDDING_PROVIDER=siliconflow`、`ENABLE_RERANK=true`，因此只配置对话模型密钥并不足以使用完整 RAG 流程。可配置硅基流动向量/重排服务，或按 [向量适配器工厂](../rag_backend/app/services/embedding_factory.py) 支持的 `zhipu`、`openai`、`ollama`、`siliconflow` 选择向量服务。

管理员登录后可在 `/settings/models` 管理对话、向量和重排模型。向量配置涉及已有数据和索引维度，修改前先确认数据迁移或重新向量化策略。恢复完整开发数据库时，还要检查数据库中的租户/部署模型配置，它们可能优先于 .env 默认值。

## 🐳 3. 启动基础依赖

在 `rag_backend` 目录执行，先启动依赖，完成数据库初始化后再启动后端：

```powershell
$ErrorActionPreference = 'Stop'
docker compose up -d db redis neo4j minio
if ($LASTEXITCODE -ne 0) { throw '基础服务启动失败' }
docker compose ps
```

数据库健康检查只表示 PostgreSQL 接受连接，并不表示业务表已经初始化。保留服务日志用于检查启动错误；不要把包含敏感信息的完整日志公开提交。

### 可选：PgBouncer

Compose 挂载的是 `docker/pgbouncer_transaction_mode.ini`，该文件由模板生成：

```powershell
$ErrorActionPreference = 'Stop'
python ./docker/render_pgbouncer.py
if ($LASTEXITCODE -ne 0) { throw 'PgBouncer 配置渲染失败' }
docker compose up -d pgbouncer
if ($LASTEXITCODE -ne 0) { throw 'PgBouncer 启动失败' }
```

注意当前 [transaction 模板](../rag_backend/docker/pgbouncer_transaction_mode.ini.template) 的上游用户写死为 `postgres`，`auth_type=trust`，池模式也写死为 `transaction`。如果使用其他数据库用户、认证方式或池模式，需要同步调整模板与认证配置再重新渲染；修改 .env 的 `PGBOUNCER_POOL_MODE` 不会改变挂载文件内的模式。

启用后端连接池入口时，再设置 `PGBOUNCER_ENABLED=true` 并重新创建后端容器。当前 Compose 没有让 backend 等待 PgBouncer 健康，启用前需检查它的实际可用状态。公开环境应收紧认证与端口暴露。

## 🗄️ 4. 初始化数据库

### 优先路径：恢复与当前代码匹配的数据库结构或备份

如果已有可运行的开发环境，优先从该环境导出**与当前代码版本匹配**的 PostgreSQL 结构，再恢复到新的空库。标准 `pg_dump` 可以记录扩展声明、表、函数、索引和约束，避免把手写修补的快照当作完整迁移链。

下面是仅导出结构的参考命令，在开发环境的 `rag_backend` 目录执行，按实际用户名与数据库名输入：

```powershell
$ErrorActionPreference = 'Stop'
$databaseUser = Read-Host '开发数据库用户名'
$databaseName = Read-Host '开发数据库名'
docker exec rag_db pg_dump -U $databaseUser -d $databaseName --schema-only --no-owner --no-privileges --encoding=UTF8 -f /tmp/rag-schema.sql
if ($LASTEXITCODE -ne 0) { throw '数据库结构导出失败' }
docker cp rag_db:/tmp/rag-schema.sql ./schema.restore.sql
if ($LASTEXITCODE -ne 0) { throw '复制结构文件失败' }
```

把结构文件传递到部署环境的 `rag_backend` 目录，在**确认目标是新建空库**后导入：

```powershell
$ErrorActionPreference = 'Stop'
$databaseUser = Read-Host '目标数据库用户名'
$databaseName = Read-Host '目标数据库名'
docker cp ./schema.restore.sql rag_db:/tmp/schema.restore.sql
if ($LASTEXITCODE -ne 0) { throw '复制结构文件失败' }
docker exec rag_db psql -X -U $databaseUser -d $databaseName -v ON_ERROR_STOP=1 --single-transaction -f /tmp/schema.restore.sql
if ($LASTEXITCODE -ne 0) { throw '结构导入失败，请检查扩展版本与错误日志' }
```

目标 pgvector/pgcrypto 等扩展必须可用，并与导出结构兼容。仅导出结构不包含用户、文档或企业数据，后续需创建管理员并配置模型。若使用自定义格式的完整备份，应采用匹配版本的 `pg_restore`；完整数据恢复还要配套迁移 MinIO 文件等持久化数据。数据库备份可能包含账号、业务数据与密钥配置，只在受控环境传递。

### 仓库中的 530.sql：结构参考快照

[530.sql](../530.sql) 是 Navicat 导出的结构快照，**不是已经核验的全新数据库安装脚本**：

- 包含 `DROP TABLE` / `DROP TYPE`，不应直接导入已有业务数据的数据库。
- 包含 pgvector 的 `vector`、`halfvec`、`sparsevec` 类型及扩展底层函数定义，也包含 pgcrypto 相关底层函数；没有标准 `CREATE EXTENSION` 初始化声明。
- 部分名称带 `hnsw` 的向量索引定义没有显式的 `USING hnsw`，不能仅凭索引名称认定它们已正确建立。
- 文件不包含可使用的初始管理员账户；结构快照也不能替代业务数据和对象存储备份。

使用它初始化前，应在开发/部署环境核对扩展声明、对象创建顺序、向量索引、当前代码所需字段与数据库结构；用空库做恢复验证后再形成安装步骤。导入时应设置遇错停止，并检查恢复结果，避免把报错后继续执行的状态视为成功。

仓库当前跟踪的 `rag_backend/alembic/versions` 仅有 `__init__.py`，不包含完整迁移链；**不能依靠 `alembic upgrade head` 从零建出全部业务表**。后端启动脚本的向量索引检查也不能替代数据库结构初始化。

## ⚙️ 5. 从源码构建并启动后端

基础依赖与数据库结构准备完成后，在 `rag_backend` 执行：

```powershell
$ErrorActionPreference = 'Stop'
docker compose up -d --build backend
if ($LASTEXITCODE -ne 0) { throw '后端构建或启动失败' }
docker compose ps
docker compose logs --tail 100 backend
```

后端镜像使用 Python 3.11 多阶段构建，并安装 PDF/OCR 等相关系统库。当前 Compose 将整个 `rag_backend` 挂载到 `/app`，Pydantic 从其中的 .env 读取配置；Compose 的 `environment` 会覆盖同名文件配置。对以 `os.getenv` 读取的配置，还需显式传入容器环境，见可选 MCP 章节。

访问：

- API 文档：[http://localhost:8000/docs](http://localhost:8000/docs)
- 进程检查：[http://localhost:8000/api/health](http://localhost:8000/api/health)
- 快速健康检查：[http://localhost:8000/health/quick](http://localhost:8000/health/quick)

```powershell
$ErrorActionPreference = 'Stop'
Invoke-RestMethod -Uri 'http://localhost:8000/health/quick'
```

健康接口可访问只证明对应检查通过；还需在实际运行环境验证登录、知识库上传、检索和智能对话，才能确认业务链路可用。

## 🌐 6. 启动前端

另开一个 PowerShell 7 终端，在仓库根目录进入 `rag_frontend`，使用跟踪的 `package-lock.json` 安装依赖：

```powershell
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath './rag_frontend'
if (-not (Test-Path -LiteralPath '.env')) {
    Get-Content -LiteralPath '.env.example' -Raw -Encoding UTF8 |
        Set-Content -LiteralPath '.env' -Encoding UTF8
}
npm ci
if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }
npm run dev
if ($LASTEXITCODE -ne 0) { throw '前端开发服务启动失败' }
```

打开 [http://localhost:5500](http://localhost:5500)。[vite.config.ts](../rag_frontend/vite.config.ts) 固定端口为 5500，启用 `strictPort`；端口被占用时不会自动切换。Vite 将 `/api` 代理到 `http://localhost:8000`，并将 `/ws-api` WebSocket 路径改写到后端 `/api`。

前端模板使用 `VITE_API_BASE`，公共请求封装兼容 `VITE_API_BASE_URL` 和 `VITE_API_BASE`；部分单独的流式请求直接使用 `VITE_API_BASE_URL`。为减少入口差异，部署时可明确设置：

```env
VITE_API_BASE_URL=http://127.0.0.1:8000
VITE_API_BASE=http://127.0.0.1:8000
```

这里填写浏览器可访问的后端根地址，公共请求封装会补充 `/api/v1`。远程部署时不要继续填写 `localhost`，它会指向访问者自己的电脑。所有 `VITE_*` 值会进入前端资源，不能存放服务端 API Key 或密码。

### 静态构建与容器部署

```powershell
$ErrorActionPreference = 'Stop'
npm run build
if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
```

构建结果位于 `rag_frontend/dist`，可交给 Web 服务器。环境值在构建时写入资源，修改后需要重新构建。

仓库提供 [前端 Dockerfile](../rag_frontend/Dockerfile) 和 [nginx.conf](../rag_frontend/nginx.conf)，但后端 Compose 未包含前端服务。现有 Nginx 将 `/api/` 和 `/ws/` 转发到 `backend:8000`，因此容器部署需让前端与后端处于同一 Docker 网络并可解析 `backend`，或改成实际后端地址。Vite 的 `/ws-api` 改写不会自动出现在生产 Nginx 中，需同步检查客户端 WebSocket 路径。SSE 代理还需检查缓冲与读取超时，HTTPS 页面应使用 HTTPS / WSS 接口。

## 👤 7. 创建企业管理员与成员账户

数据库初始化后，打开前端 `/register`，选择**企业管理员注册**。当前 [注册接口](../rag_backend/app/api/v1/endpoints/auth.py) 为 `POST /api/v1/auth/register/admin`，该流程无需短信验证码，创建用户时设置 `is_admin=true`，并分配企业租户、创建企业配置。

| 字段 | 要求 |
|------|------|
| `email` | 有效邮箱 |
| `username` | 2–50 字符 |
| `password` | 至少 6 字符；实际使用应设置强密码 |
| `full_name` | 2–100 字符 |
| `company_name` | 2–200 字符 |
| `phone` / `company_position` / `nickname` | 可选 |

字段约束见 [auth_request.py](../rag_backend/app/schemas/auth_request.py)。也可在 Swagger 中按这些字段调用管理员注册接口；没有预置的通用管理员密码。

普通注册使用 `POST /api/v1/auth/register`，企业成员可填写管理员生成的邀请码加入对应企业。登录接口为 `POST /api/v1/auth/login`，支持 JSON 中的邮箱或用户名与密码。公开部署时需审视管理员自助注册入口、入口访问控制与企业权限策略，不能把演示环境的开放注册直接视为生产权限设计。

## 🔌 可选功能与配置

| 功能 | 配置或操作 | 说明 |
|------|------------|------|
| 知识图谱提取 | `ENABLE_KNOWLEDGE_GRAPH`、`ENABLE_ENTITY_EXTRACTION`、`ENABLE_RELATION_EXTRACTION` | 模板默认关闭；开启后检查 Neo4j 与提取模型 |
| 云端视觉 OCR | `OCR_ENGINE=siliconflow_vlm` + 对应密钥 | OCR 需求与文本型文件解析不同 |
| Unstructured | `--profile heavy` / `full` + `ENABLE_UNSTRUCTURED_PARSER=true` | 默认不启动；服务地址通常为 `http://unstructured-api:8000` |
| MinerU / PaddleOCR | 模板对应服务地址与选项 | 后端 Compose 没有提供完整可用的所有解析服务 |
| 天气 / 地图 / 外部搜索 | `QWEATHER_*`、`GAODE_API_KEY`、`TAVILY_API_KEY` | 仅相关工具需要；未配置不应期待这些工具返回真实结果 |
| 短信 | `ALIYUN_*` | 按业务场景配置；不属于当前企业管理员注册的必要前置条件 |
| LangSmith | `LANGSMITH_TRACING`、`LANGSMITH_API_KEY` 等 | 可选；按数据传输与观测需求启用 |
| 政策采集 | `POLICY_ONLINE_CRAWL_ENABLED` 等 | 检查采集站点与样例回退开关，不把样例数据当作真实采集结果 |

Unstructured 的 Compose 目前固定设置了指向 `host.docker.internal:7890` 的 HTTP/HTTPS 代理；宿主机没有这个代理时，应移除或替换这些设置。启用 profile 与修改后端开关之后，用 `docker compose up -d` 重新创建受环境变量影响的服务，单纯 `restart` 不会重新读取 Compose 环境定义。

### 可选：独立 MCP HTTP 服务

本地工具由后端注册并执行，远端 MCP 只在需要独立 HTTP 工具服务时部署。模板中的 `MCP_SERVER_URL=http://your-mcp-server:8080` 是占位地址，未部署远端服务时应清空。

[MCP Dockerfile](../mcp_server/Dockerfile) 从当前源码构建，Nginx 固定监听 8080 并转发到容器内 Uvicorn 的 8888；此启动命令不依赖 `MCP_PORT` 来设置监听端口。服务端通过 `MCP_API_KEYS`（复数，逗号分隔）读取允许的 API Key，后端客户端使用 `MCP_API_KEY`（单数）。服务端空列表不会限制 API Key，不适合直接作为公开服务配置。

例如，在运行环境的 `mcp_server/.env` 中自行设置 `MCP_API_KEYS` 与实际工具需要的配置后：

```powershell
$ErrorActionPreference = 'Stop'
docker build -t tax-legal-mcp:local ./mcp_server
if ($LASTEXITCODE -ne 0) { throw 'MCP 镜像构建失败' }
docker run -d --name tax_legal_mcp -p 8080:8080 --env-file ./mcp_server/.env tax-legal-mcp:local
if ($LASTEXITCODE -ne 0) { throw 'MCP 服务启动失败' }
```

这段在仓库根目录执行。后端需要可达的 `MCP_SERVER_URL`、匹配的 `MCP_API_KEY` 与所需超时/模式配置；由 `os.getenv` 读取的这些变量应显式加入 backend 的 Compose `environment` 或正确的 `env_file`。Compose 为变量替换读取 .env，不等于自动把所有键注入容器。不同 MCP 入口的模式支持不同，不能把 `MCP_MODE=local` 一概解释为“无需服务的进程内工具”——客户端工厂里的 local 指 HTTP 服务，stdio 指子进程通信。

## 🔧 常见问题排查

| 现象 | 优先检查 |
|------|----------|
| 后端连接数据库失败 | `POSTGRES_SERVER=db`、端口与密码；容器内不要使用 localhost 连接 db |
| 提示表或字段不存在 | 数据库是否恢复与当前代码匹配的结构；不要用进程健康检查替代表结构确认 |
| MinIO AccessDenied / SignatureDoesNotMatch | .env 与 Compose 服务端凭据是否一致；检查浏览器访问地址与已保存的文件路径 |
| PgBouncer 启动失败 | 配置是否渲染、挂载路径是否为文件、上游用户名与池模式是否匹配 |
| 构建失败或依赖下载失败 | Dockerfile 使用的 Debian/Python 包源、网络连通性与构建日志 |
| bind mount 写入失败 | 后端以 appuser 运行；宿主机挂载的 uploads / logs 目录权限是否允许写入 |
| 上传成功但向量化失败 | Embedding 服务密钥、模型权限、网络与向量维度 |
| 5500 已占用 | Vite strictPort 固定端口；释放端口或明确修改 Vite 配置 |
| 流式输出延迟 / WebSocket 失败 | API 地址、生产代理路径、SSE buffering、超时与 HTTPS/WSS |
| 配置修改后不生效 | Compose environment 优先级、是否需重建容器、数据库模型配置覆盖、前端是否需重建 |

常用诊断命令，在 `rag_backend` 执行：

```powershell
$ErrorActionPreference = 'Stop'
docker compose ps
docker compose logs --tail 100 db backend
Invoke-RestMethod -Uri 'http://localhost:8000/health/quick'
```

数据库连通性可使用：

```powershell
$ErrorActionPreference = 'Stop'
$databaseUser = Read-Host '数据库用户名'
$databaseName = Read-Host '数据库名'
docker exec rag_db psql -X -U $databaseUser -d $databaseName -v ON_ERROR_STOP=1 -c 'SELECT 1;'
if ($LASTEXITCODE -ne 0) { throw '数据库连接检查失败' }
```

修改 Compose 传入的配置后，可重新创建对应服务：

```powershell
$ErrorActionPreference = 'Stop'
docker compose up -d backend
if ($LASTEXITCODE -ne 0) { throw '后端配置应用失败' }
```

停止服务使用 `docker compose down`。当前数据库、Redis、Neo4j 和 MinIO 使用 `rag_backend` 下的绑定目录持久化，停止容器不会自动备份这些数据；清理目录前先做备份。

## ✅ 在实际运行环境确认

- 基础容器可用，数据库结构与当前代码、扩展版本匹配。
- 管理员注册/登录成功，企业租户与成员权限符合预期。
- 新建知识库、上传文档、解析和向量化成功，能检索到刚上传的内容。
- 对话与专家协作可使用所配置模型；按需验证图谱、重排、OCR 和外部工具。
- 对外开放前完成 HTTPS、访问控制、端口暴露范围与数据库/对象存储备份设置。

完成这些检查后，再按实际业务场景确认运行效果。
