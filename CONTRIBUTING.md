# 参与 Solevia 开发

欢迎提交问题、交互改进与代码贡献。公开仓库采用 `app/backend`、`app/frontend`、`app/services/web-research` 和根目录 `scripts` 的结构。开发仓库对应目录为 `backend`、`frontend`、`services/web-research`、`distribution/scripts`。

## 从源码运行

需要 Python 3.12、Node.js 24、pnpm 11.25.0。下面以公开仓库为例，命令均从仓库根目录开始。

```powershell
python -m venv app/backend/.venv
app/backend/.venv/Scripts/python -m pip install -r app/backend/requirements.lock
app/backend/.venv/Scripts/python -m pip install -r requirements-core.lock
app/backend/.venv/Scripts/python -m pip install -r requirements/web.lock -c requirements-core.lock
app/backend/.venv/Scripts/python -m pip install psutil==7.2.2 python-docx==1.2.0 python-pptx==1.0.2 openpyxl==3.1.5 numpy==2.5.4
app/backend/.venv/Scripts/python -m pip install --no-deps -e app/backend
```

复制 `backend.env.example` 为 `app/backend/.env`。保持 `LLM_PROVIDER=fake` 可进行不消耗模型额度的开发。勿复制其他人的个人配置。macOS/Linux 使用虚拟环境的 `bin/python` 路径；Windows 安装器及进程隔离回归由 Windows CI 验证。

在第一个终端运行：

```powershell
cd app/backend
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m uvicorn planner_api.solevia_main:app --host 127.0.0.1 --port 8000
```

在第二个终端运行：

```powershell
cd app/frontend
npm install --global pnpm@11.25.0
pnpm install --frozen-lockfile
pnpm dev --host 127.0.0.1
```

打开 `http://127.0.0.1:5173`。API 会话由页面自动建立，无需手动填 token。使用其他开发端口时，将 `SOLEVIA_FRONTEND_ORIGINS` 设置为准确的前端 origin 后重启后端。不要使用通配来源。启用联网能力时，在 `app/services/web-research` 执行 `npm ci`、`npm start`，并按 `requirements/web.lock` 安装 Python 阅读组件。

## 提交前检查

```powershell
python scripts/project_checks.py versions
cd app/backend
.venv/Scripts/python -m ruff check src
.venv/Scripts/python -m pytest
cd ../frontend
pnpm lint
pnpm test
pnpm build
cd ../services/web-research
npm ci
npm run check
cd ../../..
node --test test/*.test.mjs
```

公开包包含产品回归测试、前端交互测试、联网服务测试与安装脚本测试。维护者内部的真实模型验收记录、训练数据和机器专用工具不进入公开包。Windows 专用测试在其他系统上会明确跳过。

## 版本、依赖与发布清单

- 唯一手动修改的版本来源是根目录 `release.json`（开发仓库为 `distribution/release.json`）。修改后运行 `python scripts/project_checks.py versions --write` 同步组件镜像；CI 检查镜像一致性。
- 能力输入在 `requirements/*.in`，对应 `.lock` 为 Windows x64 / Python 3.12 的完整固定版本依赖。维护者以 `requirements-core.lock` 和四组能力的联合解析结果为约束生成各组锁，避免共同启用时互相覆盖。安装器使用这些锁。修改依赖需在干净环境中复验；不要只改输入文件。
- `.gitattributes` 的 `* -text` 有意关闭 Git 换行转换，以便 `MANIFEST.json` 的文件哈希在 Windows/Linux 一致。请使用 UTF-8、LF 编辑文本。不要将全仓转为 CRLF。
- 源码修改后在仓库根目录运行 `python scripts/project_checks.py manifest --write` 刷新已列入清单的文件，再运行 `python scripts/project_checks.py manifest`。新增或移除公开文件需同步清单成员，不要将本地文件批量加入清单。CI 检查受 Git 跟踪的文件与清单集合相符。
- 发布前运行安全测试、完整回归和打包验证；打包工具只复制白名单文件。勿提交 `.env`、加密密钥文件、数据库、日志、模型权重、个人输入或缓存。

涉及排期、依赖、恢复、日期与状态流转的更改应补充后端回归；涉及交互的更改应补充前端测试。数据库变化必须有 Alembic migration。提交身份请使用自己的 GitHub 账号及其隐私邮箱。
