# 团队任务台 · 后端

给一个小组看今天有哪些事、谁在做、做到哪了。

- 打开就是一张任务列表，每条有标题、负责人、优先级、状态。
- 可以新建任务，也可以把状态从「待办」改成「进行中」，再改成「完成」。
- 没有登录，没有权限。
- 前端负责列表、新建和改状态；后端负责存下来并按前端需要提供接口。

纯 Python 标准库实现（`http.server` + `sqlite3`），没有第三方依赖，Python 3.6+ 可直接运行。

## 接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 / 探活 |
| GET | `/api/tasks` | 任务列表，支持 `?status=`、`?assignee=` 过滤 |
| POST | `/api/tasks` | 新建任务 |
| GET | `/api/tasks/{id}` | 任务详情 |
| PATCH | `/api/tasks/{id}` | 改任务（主要用来改状态） |
| DELETE | `/api/tasks/{id}` | 删除任务（联调、清理用） |
| GET | `/docs` | Swagger UI |
| GET | `/openapi.json` | OpenAPI 3.0 文档 |

任务数据就四个业务字段，两边对齐这一份：

| 字段 | 类型 | 取值 | 说明 |
| --- | --- | --- | --- |
| `title` | string | 1–200 字符 | 标题，必填 |
| `assignee` | string | 1–50 字符 | 负责人，必填 |
| `priority` | string | `low` / `medium` / `high` | 优先级，默认 `medium` |
| `status` | string | `todo` / `doing` / `done` | 待办 / 进行中 / 完成，默认 `todo` |

另有后端生成的 `id`、`created_at`、`updated_at`（UTC，毫秒精度）。

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

14 个用例覆盖新建、校验、状态流转、过滤、删除、CORS、Swagger 静态资源。

## 部署

测试环境：`root@47.108.60.176:22`，公网入口 `http://47.108.60.176`（systemd 服务实际监听 80，云端安全组只放行了 80）。

```bash
bash deploy/deploy-remote.sh                 # 默认推 root@47.108.60.176
bash deploy/deploy-remote.sh user@host       # 指定别的机器
```

脚本会把代码同步到 `/opt/team-tasks-backend`，注册 systemd 服务 `team-tasks` 并重启。
数据库放在 `/var/lib/team-tasks/tasks.db`，重新部署不会清掉已有任务。

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
- Swagger 文档：<http://47.108.60.176/docs>
- 所有响应带 `Access-Control-Allow-Origin: *`，允许 `GET, POST, PATCH, DELETE, OPTIONS`，前端直接跨域调用即可，不用代理。
- 列表返回 `{"items": [...], "total": n}`，新建返回 201 加整条任务，改状态返回 200 加改后的整条任务。
- 建议前端状态映射：`todo` 待办、`doing` 进行中、`done` 完成；优先级 `low` 低、`medium` 中、`high` 高。
- 出错统一返回 `{"error": {"code": "...", "message": "...", "fields": {...}}}`，`fields` 里指出具体哪个字段不合法。

### 示例

```bash
curl -s http://47.108.60.176/api/tasks

curl -s -X POST http://47.108.60.176/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title":"把登录页的联调做完","assignee":"张三","priority":"high"}'

curl -s -X PATCH http://47.108.60.176/api/tasks/1 \
  -H 'Content-Type: application/json' \
  -d '{"status":"doing"}'
```

## 目录结构

```
app.py                    HTTP 路由、校验、CORS、静态资源
store.py                  SQLite 存储（WAL）
openapi.json              OpenAPI 3.0 文档
docs.html                 Swagger UI 页面
static/swagger-ui/        Swagger UI 静态资源（本地托管，不依赖 CDN）
tests/test_api.py         端到端测试
deploy/team-tasks.service systemd 单元
deploy/install.sh         服务器端安装 / 升级
deploy/deploy-remote.sh   本地一键部署
```
