# 团队任务台 · 后端

给一个小组看今天有哪些事、谁在做、做到哪了。

- 打开就是任务列表，每条有标题、负责人、优先级、状态。
- 可以新建任务，也可以把状态从「待办」改成「进行中」，再改成「完成」。
- 需要登录：注册、登录、退出、当前用户，账号有 `member` / `admin` 两种角色。
- 任务台是团队共享的：登录后能看到所有人的任务，删除只允许创建人或管理员。
- 前端负责列表、新建、改状态和登录页；后端负责存下来并按前端需要提供接口。

纯 Python 标准库实现（`http.server` + `sqlite3`），没有第三方依赖，Python 3.6+ 可直接运行。

## 接口一览

| 方法 | 路径 | 用途 | 需要登录 |
| --- | --- | --- | --- |
| GET | `/api/health` | 健康检查 / 探活 | 否 |
| POST | `/api/auth/register` | 注册（成功即登录） | 否 |
| POST | `/api/auth/login` | 登录，拿 token | 否 |
| POST | `/api/auth/logout` | 退出，注销当前 token | 是 |
| GET | `/api/auth/me` | 当前登录用户（判断登录态） | 是 |
| GET | `/api/users` | 团队成员列表（负责人下拉框） | 是 |
| GET | `/api/tasks` | 任务列表，支持状态、负责人、我的任务和 `?q=` 关键词搜索 | 是 |
| POST | `/api/tasks` | 新建任务 | 是 |
| GET | `/api/tasks/{id}` | 任务详情 | 是 |
| PATCH | `/api/tasks/{id}` | 改任务（主要用来改状态） | 是 |
| DELETE | `/api/tasks/{id}` | 删除任务，仅创建人或管理员 | 是 |
| GET | `/docs` | Swagger UI | 否 |
| GET | `/openapi.json` | OpenAPI 3.0 文档 | 否 |

## 用户系统

- **注册**：`POST /api/auth/register`，`{"username":"zhangsan","password":"secret123","display_name":"张三"}`。
  用户名为 3–32 位字母、数字、点、下划线、短横线，不区分大小写唯一；密码至少 8 位。
  注册成功直接返回 token，前端不用再调登录。
- **登录**：`POST /api/auth/login`，`{"username":"zhangsan","password":"secret123"}`，返回
  `{"token":"...","token_type":"Bearer","expires_at":"...","user":{...}}`。
- **带 token**：之后所有任务接口都要带 `Authorization: Bearer <token>`，否则返回 401。
- **登录态**：token 有效期 7 天。前端存 localStorage，启动时调 `GET /api/auth/me` 验一下。
- **退出**：`POST /api/auth/logout`，当前 token 立即失效。
- **角色**：第一个注册的账号自动是 `admin`，之后注册的都是 `member`。
- 密码用 PBKDF2-HMAC-SHA256（260,000 轮、随机盐）存哈希；token 只存 SHA-256，库里拿不到明文。

任务数据业务字段两边对齐这一份：

| 字段 | 类型 | 取值 | 说明 |
| --- | --- | --- | --- |
| `title` | string | 1–200 字符 | 标题，必填 |
| `note` | string / null | 最多 200 字符 | 可选备注，纯空白按 `null` 处理 |
| `assignee` | string | 1–50 字符 | 负责人，必填 |
| `priority` | string | `low` / `medium` / `high` | 优先级，默认 `medium` |
| `status` | string | `todo` / `doing` / `done` | 待办 / 进行中 / 完成，默认 `todo` |

另有后端生成的 `id`、`created_at`、`updated_at`（UTC，毫秒精度），以及创建人
`created_by`（用户 id）和 `creator`（用户名）。加登录之前的老数据这两项为 `null`，所有人可见，只有管理员能删。

用户对象：`{"id":1,"username":"zhangsan","display_name":"张三","role":"member","created_at":"..."}`，永不返回密码。

## 本地运行

```bash
python3 app.py                      # http://127.0.0.1:8080
python3 app.py --port 9000 --db /tmp/tasks.db
```

打开 <http://127.0.0.1:8080/docs> 就是 Swagger UI，可以直接点 Try it out 调接口。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

用例覆盖注册/登录/退出/失效 token、鉴权与删除权限、任务新建、备注、关键词搜索、状态流转、过滤、CORS、
Swagger 静态资源、前端静态托管。

## 部署

测试环境：`root@47.108.60.176:22`，公网入口 `http://47.108.60.176`（systemd 服务实际监听 80，云端安全组只放行了 80）。

```bash
bash deploy/deploy-remote.sh                 # 默认推 root@47.108.60.176
bash deploy/deploy-remote.sh user@host       # 指定别的机器
```

脚本会把代码同步到 `/opt/team-tasks-backend`，注册 systemd 服务 `team-tasks` 并重启。
数据库放在 `/var/lib/team-tasks/tasks.db`，重新部署不会清掉已有任务。
升级到用户系统时自动给 `tasks` 表补 `created_by` 列，老任务不会丢。

常用运维命令：

```bash
systemctl status team-tasks
systemctl restart team-tasks
journalctl -u team-tasks -f
```

## 前端静态托管

后端顺带把前端静态文件挂在同一个 80 端口的根路径，前端同源调 `/api`，不需要反向代理：

- 服务器目录：`/var/www/team-tasks`，放 `index.html`、`styles.css`、`app.js`
- 打开 <http://47.108.60.176> 就是任务台；`/docs`、`/openapi.json`、`/api/*` 不受影响
- 更新前端直接替换目录里的文件，不用重启服务
- 从仓库同步：`bash deploy/deploy-frontend.sh`（默认拉 `pp5ee/user-sys-frontend` 的三个文件）
- 本地同样效果：`FRONTEND_DIR=./frontend python3 app.py`

## 前端接入

- API 根地址：`http://47.108.60.176`
- Swagger 文档：<http://47.108.60.176/docs>（右上有 Authorize，把 token 贴进去就能试带鉴权的接口）
- 所有响应带 `Access-Control-Allow-Origin: *`，允许 `GET, POST, PATCH, DELETE, OPTIONS` 和
  `Content-Type, Authorization` 请求头，前端直接跨域调用即可，不用代理。
- **登录流程**：先 `register` 或 `login` 拿 `token`，存 localStorage；之后每个请求带
  `Authorization: Bearer <token>`。收到 401 就清掉 token 回登录页。
- 列表返回 `{"items": [...], "total": n}`，新建返回 201 加整条任务，改状态返回 200 加改后的整条任务。
- 建议前端状态映射：`todo` 待办、`doing` 进行中、`done` 完成；优先级 `low` 低、`medium` 中、`high` 高。
- 出错统一返回 `{"error": {"code": "...", "message": "...", "fields": {...}}}`，`fields` 里指出具体哪个字段不合法。
  `code` 可能是 `validation_error`、`unauthorized`、`forbidden`、`username_taken`、`not_found`。

### 示例

```bash
# 注册（第一个账号是管理员）
curl -s -X POST http://47.108.60.176/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"secret123","display_name":"张三"}'

# 登录拿 token
TOKEN=$(curl -s -X POST http://47.108.60.176/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"secret123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s http://47.108.60.176/api/tasks -H "Authorization: Bearer $TOKEN"

curl -s -X POST http://47.108.60.176/api/tasks \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"title":"把登录页的联调做完","assignee":"张三","priority":"high"}'

curl -s -X PATCH http://47.108.60.176/api/tasks/1 \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"status":"doing"}'
```

## 目录结构

```
app.py                    HTTP 路由、鉴权、校验、CORS、静态资源
store.py                  SQLite 存储（任务、用户、会话，WAL）
auth.py                   密码哈希（PBKDF2）与 token
openapi.json              OpenAPI 3.0 文档
docs.html                 Swagger UI 页面
static/swagger-ui/        Swagger UI 静态资源（本地托管，不依赖 CDN）
tests/test_api.py         端到端测试
deploy/team-tasks.service systemd 单元
deploy/install.sh         服务器端安装 / 升级
deploy/deploy-remote.sh   本地一键部署
```
