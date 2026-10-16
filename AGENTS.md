# AGENTS.md — 本项目的智能体工作约定

> 适用仓库：`lcq666666/SNNU-school-network-reconnect-auto`
> 工作分支：`fix/reconnect-stability-20261013`（**禁止直接改 main**）
> 本文定位：通用工作约定 + 现行基准指针。**当用户给出新版本方案或开发计划时，
> 以 `docs/` 下最新的版本文档为准，并同步更新第 1 节"现行基准"表。**

---

## 1. 现行基准（动态指针，随版本更新）

| 项 | 现行值 |
|---|---|
| 当前版本 | **V0.3**（2026-10-14 起；V0.2 为已发布稳定版，保持可安装、不被覆盖）<br>**V0.4**（2026-10-16，能力已落地：连接后上网校验 + 远控清退）<br>**V0.5 已发布 `v0.5.0`**（2026-10-16 起，V0.5-0 → V0.5-8 全部完成，见下） |
| 现行方案文档 | `docs/V0.3双网卡智能故障切换与远程在线保障开发方案_2026-10-14.md`<br>**V0.4**：`docs/V0.4连接后上网校验与远控清退开发方案_2026-10-16.md`<br>**V0.5**：`docs/V0.5双链路真实验证与UU恢复开发计划_2026-10-16.md` |
| 现行开发计划 | **`docs/V0.3_P7.5集成修复与P8验收实施计划_2026-10-15.md`**（2026-10-15 起，P7.5-0 → P7.5-7 → P8）<br>**V0.4**：同上方案 §4 的阶段划分（V0.4-1 需求1 → V0.4-2/3 需求2 → V0.4-4 真机验收）<br>**V0.5**：同上计划 §4（V0.5-0 基线 → V0.5-1 接口受限验证 → V0.5-2 恢复协调器 → V0.5-3 UU 恢复 → V0.5-4 整合 → V0.5-5 UI → V0.5-6 日志 → V0.5-7 版本 → V0.5-8 回归/真机/发布） |
| 前一版（历史参考） | `docs/V0.3阶段执行计划_2026-10-14.md`（P0–P8 原始阶段划分）、`docs/main代码审查与修复方案_2026-10-13.md`、`docs/修复开发计划_2026-10-13.md` |
| 基线提交 | P7.5 基线 `cebc62299196ebf5b2ac5bb3776e3ad11eb613a3`（V0.3 P0–P7 完成点）；V0.3 起点 `4900ca95a670e47b31f36bb41cc4815b4cc0aa2c`；V0.4 起点 `56c42ad`；**V0.5 基线 `6e1250a7b9487158ec64b687a51af78b2fb47d1f`（版本 0.4.0，全量离线 791 passed）**；**V0.5-6 完成点 `53e260a`（全量离线 981 passed，`ruff` 全绿）** |

**V0.5 进度（2026-10-16 实测，勿据旧文档误判）**：
- `V0.5-0` `38aee55` → `V0.5-1` `89b68ea` → `V0.5-2` `c571d9a` → `V0.5-3` `be55c06` →
  `V0.5-4` `f5a1ffb` → `V0.5-5` `0bc1cb6` → `V0.5-6` `53e260a`，**均已提交并推送**（本地 == 远端）。
- 新增模块：`campus_recovery.py`（恢复事件协调器）、`campus_dual_path.py`（整合层 + 验证前置门）、
  `campus_ui_state.py`（UI 视图模型）、`campus_timeline.py`（断网/恢复时间线）。
- **`V0.5-7`（版本号 `0.4.0 → 0.5.0`，新增 `--version` CLI 标志）已完成并推送 `f78eb1a`；**
  **`V0.5-8`（P8 真机矩阵 / PyInstaller 重编译 / SHA256 / tag `v0.5.0` / GitHub Release）**
  **已完成（Release 414764742，prerelease=False，assets_count=2）**：
  `CampusReconnect.exe`（asset id 646886135，sha256:05428fc0ca2441b7d94d5f5e41f6c698dffea3d752c12bf2775b066bfef4b6ee）与
  `CampusReconnect.exe.sha256`（asset id 646887595，sha256:ffd4880fc4205806086301fb3b19ffb7f1f297f0c7c4431358ac3eddcd40c3f3）
  均已确认大小与摘要正确；EXE 作为 Release asset，**未放入**源码 Git 历史。
- 注意：`pytest -m regression` 当前收集 **0 例**（P7.5 回归用例转绿后已摘标记），
  V0.5-8 的「单独跑 regression」须按现状重新定义口径，不得据此判为失败。

**V0.4 追加规则（2026-10-16 起，与下文 §2–§6 叠加执行）**：
- V0.4 只**追加**两项独立能力：①连接后真实上网校验（防「真假死」，`verify_internet`）；
  ②确认在线后清退重启 UU 远程（网易 GameViewer）。**不改动** V0.3 已验收的认证引擎与三表计时器。
- V0.4 的阶段顺序为 `V0.4-1 → V0.4-2 → V0.4-3 → V0.4-4`（方案 §4），一次只做一个阶段；
  每阶段出口须「新增 `U-*` 全绿 + 全量离线回归零失败 + `ruff check .` 全绿」。
- **默认安全档**：需求2 的清退重启**默认关闭**（`remote_restart_enabled=False`），
  且**在本机真实清退 UU 进程前必须获得用户当面二次确认**（避免切断用户正在进行的远控会话）；
  清退范围为**用户态进程**，不动 `GameViewerService`（重启服务需 UAC）。
- 需求1 判据（闸门 + 交叉）与需求2 的触发/默认开关等选择项见方案 §6；用户改档即同步改方案与实现。

**V0.5 追加规则（2026-10-16 起，与下文 §2–§6 叠加执行）**：
- V0.5 目标：把项目升级为「校园网双链路自动故障恢复控制器」。**禁止推倒重写**已有网络状态机、
  `NetworkFailoverManager` 判定骨架、`InterfaceTransport` socket 约束、`campus_remote_app` 进程清退；
  只**新增**验证输入、恢复事件与 UU 触发三类接缝。
- **核心真值**：`Connected ≠ Internet`。真值 = **指定物理接口**经 `InterfaceTransport`
  （`IP_UNICAST_IF` + `bind(source_ip)` + 接口内 DNS）**绕过系统代理**（`trust_env=False`）的直连 HTTP
  探测；判据沿用 V0.4（Baidu 闸门 PASS **且** 交叉 ≥2，交叉站 `qq/163/sohu` 三站不扩充）。
- **验证必须绑定接口**：`verify(Ethernet)` 只能走 Ethernet socket，`verify(WiFi)` 只能走 Wi-Fi socket；
  禁止「验证 Wi-Fi 实际走 Ethernet 默认出口」。`source_ip` 变更后旧 transport 立即作废。
- **fail-closed**：接口约束失败 ⇒ `PROBE_UNRELIABLE`，**不允许**触发 failover、**不允许**触发 UU 重启。
- **UU 重启三条件**：`ACTIVE PATH` + `DIRECT INTERNET VERIFIED` + `Recovery Event` 同时成立；
  Wi-Fi 单纯 Connected / DHCP 成功 / 单站 PASS **一律不得**触发 UU 重启。UU 是恢复流水线**最后一步**，
  且 kill 前须再做一次轻量确认（active path 仍可用 + Baidu 闸门仍 PASS）。
- **去重与冷却**：同一 `network_epoch` 只产生一次 verification recovery 与一次 UU restart；
  `UU_RESTART_COOLDOWN = 300s`。
- **默认安全档（2026-10-16 用户确认）**：`remote_restart_enabled` 保持 **`False`（默认关闭）**；
  在本机真实清退 UU 进程前仍必须获得用户当面二次确认；清退范围限**用户态**进程，不动
  `GameViewerService`；`UU_WAS_RUNNING=False` ⇒ 不启动。
- **阶段顺序**：`V0.5-0 → V0.5-1 → V0.5-2 → V0.5-3 → V0.5-4 → V0.5-5 → V0.5-6 → V0.5-7 → V0.5-8`，
  一次只做一个阶段；每阶段出口须「新增 `V0.5-*` 全绿 + 全量离线回归零失败 + `ruff check .` 全绿」。
  **UI 重构（V0.5-5）必须在全部功能测试通过之后**才开始，不得与网络核心逻辑同批改动。
- 真实网络 / Portal / 路由 / UU kill 一律单独进入真机阶段，逐项授权（见方案 §10 与
  `docs/P8真实环境验收手册_2026-10-15.md`）；发布（tag `v0.5.0` / GitHub Release）时，EXE 只作
  Release asset，**不得**塞进源码 Git 历史。

**P7.5 追加规则（2026-10-15 起，与下文 §2–§6 叠加执行）**：
- P7.5 共 8 个子阶段（P7.5-0 基线复现 → P7.5-1 托盘与模式隔离 → P7.5-2 独立网络探测 →
  P7.5-3 动态无线认证 → P7.5-4 路由事务修复 → P7.5-5 远控安全 → P7.5-6 权限/UI/配置 →
  P7.5-7 完整离线集成）；**P7.5-4 未通过前禁止任何真实 Metric 切换实验**。
- 缺陷编号 `F75-01..F75-08`，复现用例编号 `R75-*`，集中在
  `tests/unit/test_v03_p75_repro.py`（`regression` 标记，默认排除）。
  **修复后必须转绿并移除 `regression` 标记**进入默认回归，不得用永久 xfail 掩盖。
- P8 真实环境验收（16 项）只能在 P7.5 全部出口条件满足后启动，且每项真实操作
  （网卡/路由/Portal/远控故障）须**再次获得用户明确授权**；结论只允许
  `PASS` / `PARTIAL` / `BLOCKED` / `FAIL`，不得以离线测试代替真实远控恢复验收。

**跟踪规则**：
- 任何任务开始前，先读"现行方案文档 + 现行开发计划"，以其阶段划分为推进顺序。
- 若 `docs/` 下出现更新的 V0.x 方案/计划文档，立即更新上表后再动手。
- 旧版文档仅作历史参考，其中的阶段号、验收标准**不**直接套用到新版本（以新文档为准）。

## 2. 任务执行方法论（用户既定方法，必须遵守）

### 2.1 分阶段推进，一次只做一个阶段
- 严格按现行开发计划的阶段顺序推进（V0.3 为 P0 → P8），未达上一阶段出口条件不进入下一阶段。
- 除非用户明确要求，**一次只完成一个阶段的任务**；不擅自扩大范围、不顺手做后续阶段的工作。

### 2.2 先建详细 TODO 列表再动手
- 每个阶段开始前，用任务列表（TaskCreate）把该阶段拆成**子任务级**的 TODO，逐条跟踪状态：
  - 开始时置 `in_progress`；
  - 完成后**立即**置 `completed`（不要批量补记）；
  - 未真正完成（有失败/部分完成/未解决错误）**不得**标记为完成。

### 2.3 测试先行，功能与单元测试同步
- 任何功能开发/修复，**必须同步**产出对应的单元/离线测试，不允许"先写代码、测试以后补"。
- V0.3 测试三级制（详见现行开发计划 0.3 节）：
  `U-*` 单元/离线 Mock → `I-*` 离线集成闭合回路 → `R-*` 受控真机；逐级解锁，前级不全绿不进入后级。
- 新测试文件命名 `tests/unit/test_v03_<阶段>_<主题>.py`；假件统一扩展进 `tests/fakes.py`。

### 2.4 每次开发完成后，全部既有测试用例必须通过
- 每完成一个子任务，运行**全部**既有测试（`.venv/Scripts/python.exe -m pytest`），要求**全绿**。
- 禁止用永久 `xfail` 掩盖缺陷；回归用例在对应阶段出口前必须转为通过。
- 默认测试集合必须**零真实网络 / 零真实注册表写入 / 零真实凭据读取 / 零真实浏览器启动 / 零真实 sleep**（用 `VirtualClock`）；违反即视为测试缺陷。

### 2.5 不确定时触发 Question 机制
- 当遇到**需要完整训练/关键选择/方案取舍**而无法从文档或代码中确定时，**不要擅自假设**，
  使用提问（Question）机制向用户明确询问，给出候选选项与推荐项，等用户确认后再继续。
- 典型场景：解释器/环境选型、依赖版本策略、是否入库某些文档、真实环境验证授权、发布范围、
  双网卡并存策略调整、路由/系统策略修改授权等。

### 2.6 每个大阶段完成后执行验收与评估
- 阶段内所有子任务完成后，**逐条核对**现行开发计划中该阶段的验收标准，输出**验收报告**
  （每条标准：结论 + 证据/命令输出）。
- 验收报告命名沿用既有约定：`docs/阶段<N>验收报告_<日期>.md`。
- 确认该阶段代码与逻辑无问题、出口条件达成后，才进入下一阶段。

### 2.7 每个阶段完成后推送到远端本分支（强制）
- **每完成一个阶段的任务后，把该阶段的提交推送（push）到远端本分支**
  `origin/fix/reconnect-stability-20261013`。
- 推送前按本机约定**清空代理环境变量**，否则会走本地代理导致 TLS 拦截：
  ```bash
  http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY= git push origin fix/reconnect-stability-20261013
  ```
- **本机实测**：`github.com` 的 git 推送会被网络限速/重置而挂起（`git ls-remote` 可用，但 pack 上传卡死）；
  `api.github.com` 曾畅通，但 `~/.git-credentials` 的 PAT 已于 2026-10-14 实测失效（`401 Bad credentials`）。
- **现行推送方式（2026-10-14 起，经用户确认）**：远端已切换为 **SSH**
  `git@github.com:lcq666666/SNNU-school-network-reconnect-auto.git`
  （`~/.ssh/config` 已将其映射到 `ssh.github.com:443`，密钥 `id_ed25519`）。推送一次成功：
  ```bash
  http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY= git push origin fix/reconnect-stability-20261013
  ```
- 备用方式：`tools/api_push.py <远端分支当前SHA>`（GitHub Git Data API，需 `~/.git-credentials` 中的 PAT 有效；
  该工具会把本地分支重建为与远端一致的 SHA，保证 local == remote）。PAT 失效期间不可用。
- **绝不推送 `main`**；不 force push；不覆盖既有版本产物。
- 注：只读查询远端（`git ls-remote`、`git fetch`）走 SSH 即可；如需走 https 则需系统代理 `127.0.0.1:7897`，见第 4 节。

## 3. V0.3 双网卡专项工作规则

以下规则在 V0.3 全阶段有效，与第 4 节安全边界叠加执行：

1. **认证引擎不动**：V0.2 的 `campus_core.repair_service` 只允许被封装调用，不得在调度逻辑中重写或复制。
2. **职责分离**：路由调度不负责 Portal 登录；校园认证不负责选择主网卡。两者只通过明确的请求/结果对象交互，禁止互相触发形成循环。
3. **单一副作用调度器**：Wi-Fi 重连、Portal POST、Metric 写入、Clash 触碰四类动作互斥串行，一轮检测不得同时触发两类以上。
4. **三表分离（P0-T6 已取证缺陷）**：网络检测计时器持续运行；Portal 认证冷却只限制认证；网卡切换冷却只限制路由变更。**认证冷却期间公网探测不得暂停**（含 `watch()` 与托盘 `_next_probe_time()`）。
5. **有线正常时禁止自动重登 SNNU**：确认 Ethernet 承载公网流量时，后台检测不得触发 Portal POST；不得因一次无线探测失败反复重认证。
6. **SNNU 请求限定接口**：Portal 解析/状态/认证/验证四步均须经 SNNU 网卡；无法可靠约束出口时，不发送真实凭据，返回"无线认证路径未确认"。
7. **先证实再切换**：切换前必须验证 Windows 实际出口接口已改变；IPv4 与 IPv6 默认路由都要检查（不得只切 IPv4）。
8. **滞回与冷却数值**（现行方案建议值，可按验证结果调整并记录理由）：检测间隔 5–15s；主链路故障连续 3 次可靠失败；Portal 重试冷却 ≥180s；切换最短间隔 300s；Ethernet 恢复判据 = 连续稳定 ≥120s 且 ≥5 次成功探测。
9. **Metric 初始建议值**（仅建议，须结合实际路由/RouteMetric/IPv6 验证，不得假定设置即生效）：Ethernet InterfaceMetric=10，Wi-Fi=50。
10. **备用模式策略**：先按"温后备"（不主动维持 SNNU 认证）开展受控测试，证据充分后再评估切"热后备"；默认模式未经受控真机验收不得启用。
11. **权限分离**：监测与认证用普通权限；路由/Metric 修改集中到独立 Windows 适配层并单独授权；托盘程序**不默认提权**管理员运行；权限缺失时提示而非反复重试。
12. **默认危险操作黑名单**（V0.3 重申并扩展）：禁用可用网卡、重置网卡、删除全部默认路由、清空 DNS、重启 Windows 网络服务、断开所有连接测切换、强杀远控软件、修改 Clash TUN/代理（仅用户显式授权后按事务改动）。

## 4. 环境约定（本机）

- 项目隔离 venv：`.venv/`（**实测 = CPython 3.14.0**；`pyvenv.cfg` 的 `home` 指向
  `...\Programs\Python\Python314`。早期文档写的"3.13"已过时，勿据此推断）。
- **已知环境噪声（未修，非产品缺陷）**：全量 `pytest` 偶发 `Windows fatal exception:
  code 0x80000003`（STATUS_BREAKPOINT）**中断整轮**；`faulthandler` 堆栈两处均落在
  `tests/unit/test_f13_save.py::test_t25_concurrent_saves_leave_a_valid_file`
  → `campus_reconnect_tray.save_config`（多线程并发原子保存的**压力用例**）。
  单独跑该用例、或 `gc.disable()` 后跑全量均**不复现**；产品运行路径（托盘 UI 线程保存）未见。
  证据：`snapshots/v04_1_pytest_stdout.txt`（含堆栈）、`v04_1_pytest_gcon.txt`、`gc_repro_attempt1.txt`。
  定性为解释器/环境层问题（3.14.0 为 3.14 首版），**未改产品代码**；是否固定回 3.13.x 待用户决定。
- **全量回归的可靠命令**（出"全绿"证据一律用这条，避免被上述噪声打断）：
  `.venv/Scripts/python.exe -c "import gc; gc.disable(); import pytest, sys; sys.exit(pytest.main(['-q','--junitxml=snapshots/<名称>.xml']))"`
- 运行/测试/静态检查一律使用 venv 内显式路径：
  - 运行测试：`.venv/Scripts/python.exe -m pytest`
  - 静态检查：`.venv/Scripts/ruff.exe check .`
- 依赖清单：`requirements.txt`（requests/keyring + 托盘 pystray/pillow）、
  `requirements-dev.txt`（pytest/pytest-cov/ruff/pyinstaller）。
- 网络访问：
  - 远端 git 现行走 **SSH**（`git@github.com`，映射 `ssh.github.com:443`）：`git ls-remote` / `git fetch` / `git push` 均可直连，无需代理。
  - 若需回退 https：只读（fetch/ls-remote）走系统代理 `https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897 git ...`；推送须**清空**代理变量（见 2.7）。
  - 校园网测试地址 `202.117.144.205` 保持 Direct/Bypass，不经 Clash。
- Wi-Fi SSID = `SNNU`；脚本勿带 shebang 行；托管 Python 路径见项目记忆。

## 5. 提交规范

- **小步提交**：一个提交只处理一类问题，message 用约定前缀：
  `test:` / `fix:` / `refactor:` / `build:` / `docs:` / `chore:`。
- **只 `git add` 明确的源码/测试/文档文件**，**不要用 `git add .`**。
- 以下内容一律不提交（`.gitignore` 已覆盖，保持）：`logs/`、`tray_config.json`、`build/`、`dist/`、
  `CampusReconnect.exe`、`.venv/`、`.pytest_cache/`、`.ruff_cache/`、`*.egg-info/`。
- 提交前本地运行默认离线测试集 + `ruff check`，全绿才提交。

## 6. 安全边界（全阶段有效，不得倒退）

- 不执行 Portal 注销（logoff）；不用踢会话方式测试恢复。
- 不主动修改 Clash 配置/订阅/TUN；系统代理仅在用户显式授权后按事务方式改动。
- 密码只留在 Windows 凭据管理器；不改为 JSON/环境变量/云端/日志，不明文保存。
- 不设置 `verify=False`；核心探测与登录保持 `trust_env=False`。
- 不在日志/报告/产物中泄露密码与 Cookie。
- **V0.3 附加**：所有有副作用的测试（真实切换、真实认证、路由修改）必须在离线 Mock 全绿后，
  经用户明确授权才在本机受控执行；执行前保存网络快照；单人值守，禁止远程会话中无人值守执行切换测试。
- 不干扰用户现有托盘实例。

## 7. 记忆位置

- 项目长期记忆：`.workbuddy-ai/memory/MEMORY.md`（项目约定、环境、交付物）。
- 每日工作日志：`.workbuddy-ai/memory/YYYY-MM-DD.md`（追加，不覆盖）。
- 跨项目个人偏好：用户级 `MEMORY.md`。
