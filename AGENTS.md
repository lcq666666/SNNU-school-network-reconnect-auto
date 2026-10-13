# AGENTS.md — 本项目的智能体工作约定

> 适用仓库：`lcq666666/SNNU-school-network-reconnect-auto`
> 工作分支：`fix/reconnect-stability-20261013`（**禁止直接改 main**）
> 依据文档：`docs/main代码审查与修复方案_2026-10-13.md`（问题 F01–F24 / 测试 T01–T38）、
> `docs/修复开发计划_2026-10-13.md`（阶段 0–5 与各阶段验收标准）

---

## 1. 任务执行方法论（用户既定方法，必须遵守）

这套方法是本项目推进修复工作的固定流程，任何一次接手任务都按此执行：

### 1.1 分阶段推进，一次只做一个阶段
- 严格按《修复开发计划》的阶段顺序：**0 → 1 → 2 → 3 → 4 → 5**，未达上一阶段出口条件不进入下一阶段。
- 除非用户明确要求，**一次只完成一个阶段的任务**；不擅自扩大范围、不顺手做后续阶段的工作。

### 1.2 先建详细 TODO 列表再动手
- 每个阶段开始前，用任务列表（TaskCreate）把该阶段拆成**子任务级**的 TODO，逐条跟踪状态：
  - 开始时置 `in_progress`；
  - 完成后**立即**置 `completed`（不要批量补记）；
  - 未真正完成（有失败/部分完成/未解决错误）**不得**标记为完成。

### 1.3 每个子阶段功能开发同步编写单元测试
- 任何功能开发/修复，**必须同步**产出对应的单元测试用例，不允许"先写代码、测试以后补"。
- 测试用例需与《审查方案》的 T 编号关联（如 `T05`、`T08`），便于回归追溯。

### 1.4 每次开发完成后，全部既有测试用例必须通过
- 每完成一个子任务，运行**全部**既有测试（一条命令，如 `.venv/Scripts/python.exe -m pytest`），要求**全绿**。
- 禁止用永久 `xfail` 掩盖缺陷；回归用例在对应阶段出口前必须转为通过。
- 默认测试集合必须**零真实网络 / 零真实注册表写入 / 零真实凭据读取 / 零真实浏览器启动**；违反即视为测试缺陷。

### 1.5 不确定时触发 Question 机制
- 当遇到**需要完整训练/关键选择/方案取舍**而无法从文档或代码中确定时，**不要擅自假设**，
  使用提问（Question）机制向用户明确询问，给出候选选项与推荐项，等用户确认后再继续。
- 典型场景：解释器/环境选型、依赖版本策略、是否入库某些文档、真实环境验证授权、发布范围等。

### 1.6 每个大阶段完成后执行验收与评估
- 阶段内所有子任务完成后，**逐条核对**该阶段《修复开发计划》中的验收标准，输出**验收报告**
  （每条标准：结论 + 证据/命令输出）。
- 确认该阶段代码与逻辑无问题、出口条件达成后，才进入下一阶段。

### 1.7 每个阶段完成后推送到远端本分支（强制）
- **每完成一个阶段的任务后，把该阶段的提交推送（push）到远端本分支**
  `origin/fix/reconnect-stability-20261013`。
- 推送前按本机约定**清空代理环境变量**，否则会走本地代理导致 TLS 拦截：
  ```bash
  http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY= git push origin fix/reconnect-stability-20261013
  ```
- 若 `github.com:443` 被网络干扰导致 git 推送失败：本机 PAT 已存入 `~/.git-credentials`（`credential.helper=store`）；
  网络干扰时可用 `api.github.com`（始终畅通）更新分支 ref 作为替代（见《个人环境备忘》）。
- **绝不推送 `main`**；不 force push；不覆盖 v0.1 产物。

---

## 2. 提交规范

- **小步提交**：一个提交只处理一类问题，message 用约定前缀：
  `test:` / `fix:` / `refactor:` / `build:` / `docs:` / `chore:`。
- **只 `git add` 明确的源码/测试/文档文件**，**不要用 `git add .`**。
- 以下内容一律不提交（`.gitignore` 已覆盖，保持）：`logs/`、`tray_config.json`、`build/`、`dist/`、
  `CampusReconnect.exe`、`.venv/`、`.pytest_cache/`、`.ruff_cache/`、`*.egg-info/`。
- 提交前本地运行默认离线测试集 + `ruff check`，全绿才提交。

## 3. 环境约定（本机）

- 项目隔离 venv：`.venv/`（由托管 Python 3.13.14 创建）。
- 运行/测试/静态检查一律使用 venv 内显式路径：
  - 运行测试：`.venv/Scripts/python.exe -m pytest`
  - 静态检查：`.venv/Scripts/ruff.exe check .`
- 依赖清单：`requirements.txt`（运行：requests/keyring + 托盘 pystray/pillow）、
  `requirements-dev.txt`（pytest/pytest-cov/ruff/pyinstaller）。精确版本锁定在阶段 4（F22）完成。

## 4. 安全边界（全阶段有效，不得倒退）

- 不执行 Portal 注销（logoff）；不用踢会话方式测试恢复。
- 不主动修改 Clash 配置/订阅/TUN；系统代理仅在用户显式授权后按事务方式改动。
- 密码只留在 Windows 凭据管理器；不改为 JSON/环境变量/云端/日志，不明文保存。
- 不设置 `verify=False`；核心探测与登录保持 `trust_env=False`。
- 不在日志/报告/产物中泄露密码与 Cookie。
- 真实 Portal 与系统级测试**仅在用户明确授权后**按《修复开发计划》5.2 执行。
- 不干扰用户现有托盘实例。

## 5. 记忆位置

- 项目长期记忆：`.workbuddy-ai/memory/MEMORY.md`（项目约定、环境、交付物）。
- 每日工作日志：`.workbuddy-ai/memory/YYYY-MM-DD.md`（追加，不覆盖）。
- 跨项目个人偏好：用户级 `MEMORY.md`。
