# 校园网自动重连助手（SNNU · Windows · Clash Verge Rev）

## v0.9.0 全自动持续在线

发布同时提供 `CampusReconnectSetup.exe` 安装版和 `CampusReconnect.exe` 便携版。安装版写入当前用户的开始菜单、桌面快捷方式和登录后自启，升级时保留 `%LOCALAPPDATA%\CampusReconnect` 下的账号凭据、配置、日志和恢复记录；便携版继续使用单文件更新。

启动后助手会自动检查并配置可逆的在线守护、登录后自启和电源防睡眠设置，自动验证校园网 Wi‑Fi、UU 进程及其外部连接。掉线恢复按三层执行：先只重连 Wi‑Fi，再保存并恢复代理后完成校园认证，持续 15 分钟且 Windows 自动登录与自启均可用时才安排 60 秒倒计时重启电脑；网络恢复会立即取消倒计时，每 6 小时最多重启一次。UU 是否允许无人值守访问仍由用户在 UU 客户端确认，助手会把它单独显示为“无法自动确认”。

主界面使用自绘标题栏，支持拖动、双击最大化、最小化、关闭和 Alt-Tab；窗口按显示器工作区和 DPI 自适应，概览页首屏显示核心状态，详细页只在内容超出时滚动。主题切换立即生效，不需要重启。

[下载 v0.9.0](https://github.com/lcq666666/SNNU-school-network-reconnect-auto/releases/tag/v0.9.0)。

## v0.7.0 在线守护与新界面

日常使用只需填写校园网账号，开启“在线守护”。全新安装默认开启守护和登录后运行；升级保留原设置。助手在后台检查网络、恢复 UU，并自动处理已识别 Clash Verge 的校园网兼容配置，不再弹出代理修复向导或要求用户先退出客户端。

自动处理前保存本机配置；需要时短暂重启已识别的 Clash Verge 客户端，完成后保留原端口、订阅、模式和其他规则。客户端恢复记录先写盘，助手中断后下次启动继续恢复。直连正常但代理连续三次失败时再恢复客户端，设置冷却避免频繁重启。未安装 UU 时仅守护网络。

界面参考 Magpie：浅灰侧栏、白色内容区、紫色按钮，主窗口和诊断窗口使用统一中文字体与 DPI 缩放。普通页面只显示连接状态、在线守护、账号、开机运行和更新；技术报告、手动备份恢复和规则导出移到高级选项。状态按真实验证结果显示，删除圆环映射百分比和闪烁。

强制重启电脑需要另外开启，并需完成 Windows 重启后的登录恢复设置；本版保留原有授权。自动识别多个配置目录、未知 VPN/PAC 或权限冲突时保留现状，诊断不宣称这些配置已生效。校园网停服、断电、硬件故障或系统完全死机无法保证持续在线。

[下载 v0.7.0](https://github.com/lcq666666/SNNU-school-network-reconnect-auto/releases/tag/v0.7.0)，也可使用软件内更新。

## v0.6.10 跨电脑代理兼容

支持 Windows 10/11 x64。首次运行提供本机代理兼容检查，设置 → 网络或托盘菜单可再次打开。检查区分 Windows 系统代理、PAC、Clash 模式与 TUN，不假定代理端口是 7897。

“修复系统绕过”只追加 `202.117.144.205` 与 `net.snnu.edu.cn`，保留原开关、端口和 PAC URL。“修复所选 Clash Verge”会备份并追加持久绕过与全局脚本，动态保留订阅中的原分流、DNS 和 TUN 列表；不编辑订阅缓存。脚本增加认证 IP 的 TUN 路由排除、校园域名真实 DNS 和前置 DIRECT 规则。修改前请暂停助手并等待恢复任务结束；持久修复还需退出 Clash Verge，完成后重新启动客户端、检查结果并恢复助手监测。

标准安装路径自动识别，便携版可选择含 `verge.yaml`、`profiles.yaml` 的配置目录。多配置目录需明确选择。其他 Clash/Mihomo 客户端可导出 YAML 片段、全局脚本与中文说明；片段需合并进客户端持久覆写，不能替换完整配置或当订阅导入。PAC 脚本及其他 VPN 的 TUN 需要按对应客户端配置，助手不擅自改写。

每台电脑独立备份，提供“恢复最近备份”。检测到后续用户修改时停止覆盖。普通 DIRECT 规则在 Clash 全局模式下不生效，TUN 需另外核对路由排除；检查无法确认时不会显示为已完成。旧自愈流程也不再猜测固定端口或强行停止代理客户端。

## v0.6.8 更新与三层恢复

更新按钮提供本地下载进度、SHA256 校验、替换后重启助手和启动失败回滚。配置使用固定用户数据目录，首次启动迁移旧配置和开机自启路径。

在设置的“恢复”页开启三层恢复，可以依次执行 UU 重启、关闭代理后重连认证并恢复代理、持续故障后的电脑重启。运行期间阻止系统自动睡眠。强制重启需要单独开启，并接受未保存文件丢失风险。

两台设备都需要在本机完成 Windows 自动登录设置。恢复页提供微软官方 Autologon 入口，密码由用户在微软工具窗口输入。助手在开机自启时锁定屏幕。自动登录凭据保存在 LSA 中，管理员可能读取。

四位验证码在本机识别并与登录会话一起提交。详细流程与验收范围见 [v0.6.7 说明](docs/V0.6.7恢复与更新_2026-11-12.md)。

让校园网掉线后自动重新认证，**全程不需要关闭 Clash Verge 的系统代理**。

以下保留 v0.5.0 及更早的开发记录和技术说明；普通用户按本文顶部 v0.7.0 说明使用，无需执行下方的手动代理配置步骤。

> **历史版本：`V0.5.0`（2026-10-16）** —— 从「自动重连校园网工具」升级为
> **校园网双链路自动故障恢复控制器**：Ethernet 主用 / SNNU Wi-Fi 温备用、
> **接口受限**真实上网验证（`Connected ≠ Internet`）、验证通过后才清退重启 UU Remote。
> 能力清单见 [§12 V0.5.0 版本能力](#12-v050-版本能力2026-10-16)。

本目录文件：

| 文件                         | 作用                                   |
| -------------------------- | ------------------------------------ |
| `campus_reconnect.py`      | 主程序/引擎（掉线检测 / 自动认证 / 状态恢复验证，命令行版）    |
| `campus_reconnect_tray.py` | 托盘版（任务栏图标 + 气泡通知 + 设置面板，推荐日常使用）      |
| `campus_core.py`           | 共享契约与重连服务（配置/结果/错误码/纯解析，CLI 与托盘共用）   |
| `campus_windows.py`        | Windows 副作用适配层（Wi-Fi / 代理 / 凭据 / 自启 / 互斥体） |
| `run.bat`                  | 无窗口启动命令行版常驻监测                        |
| `run_tray.bat`             | 启动托盘版（优先源码 venv，其次 exe）               |
| `CampusReconnect.exe`      | 打包版托盘应用（单文件，无需 Python，见下）            |
| `用户手册.md`               | 面向最终用户的完整使用手册（exe 版）                 |
| `pyproject.toml`           | 项目元数据与依赖分组（运行 / 托盘 / 开发 / 打包）        |
| `requirements*.txt`        | 依赖清单：`-lock` 为精确版本快照，用于可追溯重建         |
| `tools/`                   | 构建与运维工具（版本/commit 资源、受控构建、API 推送）    |
| `.github/workflows/`       | CI（离线测试 + ruff + 构建）与受控发布工作流          |
| `docs/`                    | 方案流程图与各阶段验收报告                        |
| `logs/`                    | 运行日志（自动轮转，排查问题看这里；不纳入仓库） |

## 0. 环境实测结论（2026-10-13，在本机实践验证）

- Portal 首页 `http://202.117.144.205/` 会 JS 跳转到认证页，认证端口在
    
  **8601 / 8602 / 8603 / 8605 / 8609 之间轮询**，因此脚本每次动态解析跳转端口，不写死。
- 登录接口：`POST http://202.117.144.205:<端口>/snnuportal/login`，
    
  字段 `sourceurl=null, account, password, yys, issave=`。登录页当前无验证码。
- 认证按 **IP** 生效；失败的登录**不会**影响已有在线会话（已用假账号验证）。
- 本机环境：Clash Verge **Rev**、仅开启系统代理（127.0.0.1:7897）、**未开 TUN**、规则模式；
    
  Wi-Fi 为 `SNNU`（开放式）。**源码模式**使用项目内隔离环境 `.venv`（Python 3.13.7），
  依赖的精确版本见 `requirements-lock.txt`（F22）。
- 首次使用可执行 `.venv\Scripts\python.exe campus_reconnect.py fix-bypass`，把 Portal 地址即时写入系统代理
    
  绕过列表（重启 Verge 或切换代理开关后会丢失，仍需按第 1 步在界面里配置）。
- **支持范围**：本工具适配**陕西师范大学**的 snnuportal；把 Wi-Fi 名称改成别的 SSID 只是改监测目标，
    
  **不会**让它自动适配其他学校的 Portal（字段/端口/结构都不同）。

## 1. 第一步：让 Edge 在系统代理开启时也能打开登录页（永久生效）

这是"必须手动关代理"的根因解法。在 Clash Verge Rev 界面配置一次：

1. 打开 Verge → **设置** → **系统代理** → 找到"代理绕过 / Bypass"输入框；
2. 在已有内容**末尾追加**（英文分号分隔，**不要删除默认条目**，不要加 `http://`）：
   ```
   202.117.144.205;net.snnu.edu.cn
   ```
3. 保存后，把系统代理开关关一次再打开，使其写入注册表。

> `fix-bypass` 只是即时补丁：Verge 在切换系统代理开关时会用自身设置重写注册表，
>   
> 所以请在界面里按上面步骤配置一次，才能永久生效。未开启 TUN 时以上即足够；
>   
> 若以后开启 TUN，需在分流规则中加 `IP-CIDR,202.117.144.205/32,DIRECT`。

## 2. 安装依赖并保存账号密码

**推荐**：使用项目内隔离环境（不污染全局 Python，版本可控）。在命令行终端进入本目录：

```bat
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe campus_reconnect.py configure
```

> `requirements-lock.txt` 是已知可构建、可通过全部离线测试的**精确版本快照**（F22）；
> 只想跟随版本范围时也可用 `requirements.txt`。
> 下文命令中的 `.venv\Scripts\python.exe` 可按需替换为你自己的解释器路径。

`configure` 会交互式询问账号和密码，只写入**当前 Windows 用户的凭据管理器**
  
（控制面板 → 凭据管理器 → Windows 凭据，名称 `SNNU-Campus-Reconnect`），
  
**不写入任何文件、不进日志**。注意：凭据管理器的存储是持久化的安全存储（可被本用户/本机读取），
  
“不落盘”指的是**不以明文写入本项目文件**，两者不要混为一谈。

## 3. 自检（先跑这个）

```bat
.venv\Scripts\python.exe campus_reconnect.py status
```

正常应看到：`无线网卡状态: 已连接（SSID: SNNU）`、`Portal 可达: 是`、
  
两个公网探测按**顺序尝试**（首个匹配成功即视为在线）、`代理绕过列表: 已包含 Portal 地址`。

## 4. 预检与测试（按此顺序，不要跳步）

**第一步：预检（不发送任何认证请求，安全）**

```bat
.venv\Scripts\python.exe campus_reconnect.py once --dry-run
```

应看到：`SSID/状态: SNNU / 已连接`、`认证端口` 及其识别来源、`登录页可达: 是`、
  
`凭据已保存: 是`，以及将提交的载荷（密码显示为 `***已隐藏***`）。

**第二步：前台常驻监测（正常上网时只探测，不发认证请求）**

```bat
.venv\Scripts\python.exe campus_reconnect.py watch --ssid SNNU --yys mobile
```

让它保持运行。日常上网期间它只做公网探测；**只有在真实掉线时才会自动认证**，
  
这才是对接口最真实的验证。成功标志：日志出现 `连续 3 次失败` → `Portal 认证端口` →
  
`登录结果: 成功` → `网络恢复成功`。

**第三步（可选，进阶）：--force 在线强制认证**

```bat
.venv\Scripts\python.exe campus_reconnect.py once --force --browser-fallback
```

`--force` 会在**网络正常时也提交一次真实凭据**，仅用于接口联调，平时不需要；
  
脚本会打印醒目警告。若返回 `Portal 返回失败信息: 密码错误...`：先确认密码，
  
再试 `--account-format isp`（以 `学号@mobile` 形式提交账号）。
  
`--browser-fallback`：认证失败时自动打开一个**不走系统代理**的 Edge 登录页，可人工完成。
  
注意：`once`（含 `--force`）与 `watch` / 托盘**共用同一个单实例互斥体**，若托盘或 watch 已在运行，
  
`once` 会拒绝执行并退出（避免并发重复认证）；如需手动联调请先退出托盘。`once --dry-run` 不认证，不受此限制。

## 5. 常驻监测（命令行版）

```bat
.venv\Scripts\python.exe campus_reconnect.py watch --ssid SNNU --yys mobile
```

工作逻辑：每 15 秒做一次**不走系统代理**的公网探测（Microsoft + Apple，**顺序尝试、首个成功即返回**），
  
连续 3 次失败判定掉线 → 确认 Wi-Fi 是 SNNU（**完整匹配 SSID**）→ 解析认证端口 → 检查登录页可达 →
  
用凭据管理器中的账号按运营商（默认 mobile 移动）重新认证 → 以状态页 + 公网探测
  
双重确认恢复。两次尝试至少间隔 180 秒，防止校园网维护期间反复重登。

端口解析优先级（脚本会记录实际来源，不会静默处理）：
  
**用户 `--port` 指定 > Portal 首页跳转（Location / JS）动态识别 > 已知端口（8601/8602/8603/8605/8609）试探**；
  
全部失败则如实报告“Portal 不可达”，**不假装已选端口**，也不提交凭据。

单实例保护：脚本内置互斥体，重复启动的第二个 watch 会直接退出并提示，
  
因此 `run.bat` 重复双击、或与任务计划叠加运行都不会造成双重监控；
  
`once` 认证与托盘认证也遵守同一互斥体（F18）。互斥体创建失败时按 **fail-closed** 处理（拒绝启动后台认证），
  
不会在保护失效的情况下继续运行。
  
`Ctrl+C` 停止；日志见 `logs/campus_reconnect.log`。

## 6. 托盘版（推荐日常使用）

不满足于黑窗口，就用托盘版：任务栏右下角一个小图标，**右键菜单 + 气泡通知 + 设置面板**，
  
掉线→重连→恢复的每一步都看得见。

```bat
.venv\Scripts\python.exe -m pip install pystray pillow
.venv\Scripts\python.exe campus_reconnect_tray.py
```

或双击 `run_tray.bat`（无窗口启动）。图标四态：**绿 = 在线 / 黄 = 重连中 / 红 = 已断开 /
  
灰 = 已暂停**，悬停显示当前状态与认证端口。

气泡通知覆盖三个关键时刻：

| 时机 | 通知内容                                     |
| -- | ---------------------------------------- |
| 启动 | 校园网助手已启动 · Wi-Fi 与网络状态                   |
| 掉线 | 校园网已断开 · 正在尝试重新连接，请稍候…                   |
| 成功 | 重连成功 · 用时约 N 秒 · （如开启联动）已自动开启 Clash 系统代理 |
| 失败 | 重连失败 · 具体原因 · （如开启）已打开 Edge 兜底           |

右键菜单：状态 / 立即重连 / 暂停·恢复 / 开机自启 / 重连后自动开启 Clash 系统代理 /
  
Edge 兜底 / 设置面板 / 打开日志 / 退出。

**重连后自动开启 Clash 系统代理**：首次重连成功时会弹窗询问"是否同意以后每次重连成功后
  
自动开启 Clash 的系统代理"，选择记入 `tray_config.json`，之后随时可在菜单或设置面板更改。
  
**这是需要显式授权的系统副作用**：只有在得到授权、且代理端点（`host:port`）经验证为本机监听地址时才会
  
写入注册表 `ProxyEnable`/`ProxyServer`；写入失败会按事务回滚，回滚与系统刷新结果分别报告。
  
仅写注册表**不等于**所有应用 / Clash 界面立即采用——Verge 界面开关显示可能滞后到你手动切一次，
  
功能不受影响。命令行核心（`watch`/`once`）默认**不**改动系统代理。

**开机自启开关**：菜单一键切换，写 `HKCU\...\Run` 注册表项 `CampusReconnectTray`，
  
登录 Windows 后自动启动托盘版；关闭即删除该项。设置面板里可改检测间隔、失败阈值、
  
冷却时间、Wi-Fi 名称与运营商。自启命令**对应当前安装路径**；若检测到已有自启项指向其他位置，
  
会先询问再接管，不静默覆盖（F21）。

> 托盘版与命令行版共用同一个单实例互斥体，同时只有一个在跑；用了托盘版就不必再手动开 `watch`。
> 该互斥体同时覆盖 `once` 的一次性认证（F18）。

## 7. 开机自启：两种方式

| 方式           | 适用   | 操作               |
| ------------ | ---- | ---------------- |
| Run 注册表项（推荐） | 托盘版  | 托盘菜单 → 开机自启 → 开启 |
| 任务计划程序       | 命令行版 | 见下方步骤            |

如需用任务计划自启命令行版：

1. `Win+S` 搜索"任务计划程序" → **创建任务**（不要用"基本任务"）；
2. 常规：名称 `CampusReconnect`，**仅当用户登录时运行**（本工具不需要管理员权限，普通当前用户即可）；
3. 触发器 1：**登录时**（可延迟 30 秒等 Wi-Fi 就绪）；
4. 触发器 2（笔记本关键）：**发生事件时** → 自定义 XML，让连上 SNNU 后也跑一次：
   ```xml
   <QueryList>
     <Query Id="0" Path="Microsoft-Windows-WLAN-AutoConfig/Operational">
       <Select Path="Microsoft-Windows-WLAN-AutoConfig/Operational">
         *[System[Provider[@Name='Microsoft-Windows-WLAN-AutoConfig'] and (EventID=8001)]]
         and *[EventData[Data[@Name='SSID']='SNNU']]
       </Select>
     </Query>
   </QueryList>
   ```
   （若事件查看器里 `WLAN-AutoConfig/Operational` 未启用，先右键启用日志。）
5. 操作：程序填 `<Python目录>\pythonw.exe`（用 `where pythonw` 查实际路径），
     
   参数填 `"<本目录绝对路径>\campus_reconnect.py" watch --ssid SNNU --yys mobile`，
     
   起始于填 `<本目录绝对路径>`；
6. 条件：笔记本建议取消"仅在交流电源时启动"；
7. 设置：勾&#x9009;**"如果任务已在运行，则不启动新实例"**，并去掉"如果任务运行超过..."
     
   自动终止的时限（脚本自身也有单实例互斥保护，双保险）。

日常也可以直接双击 `run.bat` 手动启动常驻监测。

## 8. exe 版与换电脑使用

release（或 `dist/` 目录）里的 `CampusReconnect.exe` 是 PyInstaller 打包的单文件版本，
  
**不需要安装 Python**，拷到任何 Windows 10/11 电脑双击即可运行：

1. 首次运行会弹出"校园网账号设置"窗口，输入账号密码（只存本机凭据管理器）；
2. 手动双击会打开主界面，开机自启则在托盘后台运行。连上 SNNU Wi-Fi，托盘图标变绿即开始保护；
3. 首次重连成功时会询问是否同意"以后自动开启 Clash 系统代理"；
4. 右键菜单 → 开机自启 → 开启。

从旧版升级时，先在任务栏时钟旁找到旧版托盘图标（可能藏在 `^` 里），右键选择「退出」，
再双击新版。如果旧版启用了开机自启，在新版里重新开启并确认接管旧路径。

注意：账号密码按 Windows 用户隔离，**不会**随 exe 转移到其他电脑，所以每台新电脑都要
  
重新输入一次；若目标电脑不用 Clash，本工具同样适用（它从不修改 Clash，只是保证自己
  
不走代理）。

完整使用说明（下载、首次运行、图标状态、菜单详解、FAQ、卸载步骤）见 `用户手册.md`。

## 9. 安全与边界

- **不执行注销（logoff）**：程序自身不会把已有会话踢掉；但**是否影响你同账号的其他设备，
  还取决于学校对同账号并发的策略**，本工具无法保证——不作绝对承诺；
- **命令行核心默认不改动系统代理**（探测流量用 `trust_env=False` 强制直连，也不绕过 TUN / 路由层代理）；
  **托盘版经你显式授权后**才会在重连成功时写系统代理注册表，且仅限已验证的本机端点；
- **不修改 Clash Verge 配置文件、订阅或 TUN 设置**；
- **密码只经 Windows 凭据管理器读写**：不以明文写入本项目任何文件，不进日志，不出现在本仓库；
  注意凭据管理器是**持久化的安全存储**（本用户/本机可读取），并非“完全不落盘”；
- 学校 Portal 是明文 HTTP，认证报文本身不加密——这是学校系统的固有限制，脚本无法改变，
    
  也因此**不要把账号密码、Cookie 发给任何人**；
- **网络目的地**：除学校 Portal 外，仅访问公开连通性探测地址（Microsoft / Apple，按顺序尝试）；
  不上传账号给作者或任何云端，但探测与登录时**网络服务方可能看到你的源 IP**；
- 若不再使用，在凭据管理器中删除 `SNNU-Campus-Reconnect` 两条记录即可。

## 10. 故障排查

| 现象                         | 处理                                                         |
| -------------------------- | ---------------------------------------------------------- |
| status 里探测未通过但能正常上网        | 探测地址被限制：改 `DEFAULT_PROBES`（顺序尝试，首个结构匹配即通过）                   |
| 登录总是失败、提示失败信息              | 试 `--account-format isp`；确认 `--yys` 与你在页面选的运营商一致           |
| 认证端口全部不通                   | `--port 8601` 或 `8602` 等手动指定（可选值 8601/8602/8603/8605/8609） |
| 提示"已有一个监控实例在运行"            | 正常保护：已有 watch/托盘在跑（`once` 也会被拦）；如需手动联调，先退出托盘           |
| 显示非目标网络 / 不重连              | Wi-Fi 名称需与系统显示的 SSID **完全一致**（含大小写与空格）；可在设置面板核对     |
| Portal 改版（字段/验证码变化）        | Edge F12 → 网络，手动登录一次，对照 `login.js` 修改 `portal_login`       |
| Verge 切换代理开关后 Edge 又打不开登录页 | 按第 1 步在 Verge 界面补回绕过地址，或重跑 `fix-bypass`                    |
| 后台无窗口运行无任何反应               | 看 `logs/campus_reconnect.log`（`pythonw` 不显示控制台）            |

## 11. 构建与可追溯（开发者）

```bat
rem 运行离线测试与静态检查（默认测试集零真实网络 / 零真实注册表 / 零真实凭据 / 零真实浏览器）
.venv\Scripts\python.exe -m pytest
.venv\Scripts\ruff.exe check .

rem 受控构建：测试 + ruff + PyInstaller（自动写入版本资源）
.venv\Scripts\python.exe tools\build_release.py
```

构建产物：

- `dist\CampusReconnect.exe`：内嵌 **版本号与源码 commit**（可用文件属性查看）；
- `dist\build_info.json`：版本 / commit / Python 与架构 / 依赖清单；
- `dist\CampusReconnect.exe.sha256`：SHA256 与字节大小。

CI（`.github/workflows/ci.yml`）在 Windows 上执行「离线测试 + ruff + 构建」，默认 `contents: read`；
发布（`release.yml`）仅在推送 `v*` 标签时运行，才提升为 `contents: write`。
第三方 action 全部锁定到不可变 commit。构建**不注入用户凭据**，不上传日志 / 配置。
注意：重建产物的字节哈希不必然相同（时间戳 / UPX 版本等），因此只承诺“按锁定依赖可重建并通过测试”，
不夸大为 bit-for-bit 复现。

## 12. V0.5.0 版本能力（2026-10-16）

### 12.1 双链路主备

**Ethernet = PRIMARY**、**SNNU Wi-Fi = STANDBY**（温备用）。二者各自独立判定健康，
主链路故障且备用链路**被证实可用**后才切换，恢复则需连续稳定满足滞回窗口才切回。

### 12.2 Internet 验证（`Connected ≠ Internet`）

"接口已连接 / DHCP 成功 / Gateway 通"**都不等于能上网**。真值 = 指定物理接口经
`InterfaceTransport`（`IP_UNICAST_IF` + `bind(source_ip)` + 接口内 DNS）**绕过系统代理**
（`trust_env=False`）的直连 HTTP 探测：

- **Baidu 闸门**：`http://www.baidu.com/` 严格 `<title>` 匹配，必须 PASS；
- **交叉站点**：`qq.com` / `163.com` / `sohu.com` 三站，**至少 2 站 PASS**；
- 两者同时满足才判 `VERIFIED`，否则本次验证不通过。

验证**绑定接口**：`verify(Ethernet)` 只走 Ethernet socket，`verify(WiFi)` 只走 Wi-Fi socket，
不会因为 Windows 默认出口是 Ethernet 就把 Wi-Fi 判成在线。接口约束无法证实、或会话可能经
环境代理时判 `PROBE_UNRELIABLE`，**fail-closed**：不触发切换、不触发 UU 重启。
因此 Clash / 浏览器能上网**不能**证明校园网直连正常（Chrome 可、Edge 不可时，
状态是「网络健康 + 应用层问题」，不该重新认证校园网）。

### 12.3 UU Remote 恢复

UU 重启是恢复流水线的**最后一步**，三个条件同时成立才允许：`ACTIVE PATH` +
`DIRECT INTERNET VERIFIED` + `Recovery Event`。Wi-Fi 单纯 Connected / DHCP 成功 /
网关通 / 单站 PASS **一律不触发**。执行前还会再做一次轻量确认（活动路径仍可用 + Baidu 闸门仍 PASS），
避免"验证刚过、网络又断、却已把 UU 杀掉"的双重故障。

- 只清退**当前用户**已核实用户态进程（`GameViewer.exe` / `GameViewerLauncher.exe`），
  **不动** `GameViewerService` 等服务，不重启 Windows 网络服务；
- `UU_WAS_RUNNING = false` ⇒ **不启动**，不会替你把已关闭的 UU 拉起来；
- 一次网络恢复事件最多一次重启，冷却 `UU_RESTART_COOLDOWN = 300s`；
- 开关 `remote_restart_enabled` **默认关闭**，需显式开启。

### 12.4 UI

主窗口仪表盘：总体状态灯（Healthy / Degraded / Recovering / Offline / Unknown）+
**Ethernet / SNNU Wi-Fi 双 Network Card**（主备角色、IP、Gateway、ifIndex、SSID、Portal、
Internet 是否真已验证、最近验证时间）+ **恢复流水线**（检测 → Wi-Fi Ready → Portal →
Internet Verify → Route Switch → UU Restart）+ 按钮（立即检测 / **立即验证**（分网卡展示逐站结果）/
手动重连 / 重启 UU Remote）。设置页改为 Notebook 五页（基本 / 网络 / 恢复 / UU Remote / 诊断）。
所有网络操作在后台线程，UI 线程不发起网络 I/O。新增「网络时间线」只读区域，
回答「什么时候断的网、多久恢复」，一次断网→恢复记为一个故障事件。

### 12.5 安全边界（不变）

不注销 Portal、不修改 Clash 配置/订阅/TUN、密码只留 Windows 凭据管理器、
不设置 `verify=False`、日志不写账号/密码/Cookie/MAC、不覆盖既有版本产物。

## 13. V0.6.0 版本能力（2026-11-11）

针对用户实测反馈的四个问题（详见 `docs/V0.6开发计划_2026-11-11.md`）：

### 13.1 双轴探测 + Clash 假死自愈

探测从单轴（直连）变成双轴：直连轴仍按 V0.5 的接口受限验证，直连通但代理轴 timeout/connection
失败即判 **Clash 假死**，不判在线。假死自愈分两步：先关开一次系统代理，还不行就重启
Clash Verge Rev 进程；两步都失败则建议人工处理。探测默认值改快：interval 15→5 秒、
threshold 3→2 次、cooldown 180→60 秒（配置仍可改，有限幅）。

### 13.2 有线转 Wi-Fi（自启链）

首次启动弹一次“开启智能自动切换？”确认，同意后 `failover_mode=AUTO` +
`auto_switch_authorized=true`（拒绝则保持仅监测，不再弹）。有线故障时自启链按顺序执行：
查网卡状态 → 禁用则启用（需管理员，失败如实报告）→ 射频关了就打开 →
没有 SNNU profile 就建开放式 profile → `netsh wlan connect SNNU` → 等 IP 就绪 → 再走 Portal 认证。
切回有线需连续稳定 120 秒。

### 13.3 UU Remote 恢复（两档）

触发条件改为：发生过断网再恢复、且恢复前 UU 在运行（`was_running`）、验证通过后后台重启，
不弹窗。第一档只清退 `GameViewer.exe` / `GameViewerLauncher.exe`，重启后等 10 秒确认进程回来 +
一次直连验证，通过算成功；60 秒内仍不通升第二档，连 `GameViewerServer.exe` /
`GameViewerHealthd.exe` 一起清退，服务 `GameViewerService` 永远不动。保留 300 秒冷却
（可配置，下限 60 秒），一次恢复事件最多重启一次（第二档计为同一次事件的升级，不另计）。

### 13.4 界面：CustomTkinter 深色仪表盘

仪表盘改用 CustomTkinter 深色主题：圆角卡片、状态灯呼吸动画（恢复中时闪烁）、
恢复流水线进度条、网络时间线时间轴、最近一小时双链路状态图（Canvas 手绘）。
无 CustomTkinter 时回退原生 ttk，保证仍能启动；设置仍是 5 页。
**注意：Linux 上 PyInstaller 打不出 Windows exe**，打包须在 Windows 上跑
`tools/build_release.py`，详见 V0.6 开发计划 §6。

### 13.5 V0.6.1：A+D+E 融合仪表盘（2026-11-11，用户敲定）

顶部大状态区（大字状态词 + 一句结论 + 呼吸灯）+ 中部三环仪表（以太网 / SNNU Wi-Fi /
UU 远程，Canvas 手绘圆环，百分比是状态映射不是实测速率，无数据不猜）+ 下部历史时间线
（真实事件最新在上，最多 5 条，空时显示占位文案）；双网卡 10 行卡片默认收起，
点“展开详情”才展开；流水线、一小时状态图与 8 个按钮全部保留。详见 V0.6 开发计划 §8。

### 13.6 V0.6.2：自动更新提醒（2026-11-11）

装好 exe 后不是死的：启动 60 秒后首次检查 GitHub 最新 Release，之后每 6 小时一次。
查到新版本时三处同时提醒——仪表盘顶部琥珀色横幅（“下载更新”打开浏览器到 Releases
页面，“以后再说”关闭横幅）、托盘气泡通知一次、设置页“更新”区显示当前与最新版本。
气泡同一版本只弹一次（`update_notified_tag`），横幅在用户点“以后再说 / 下载更新”前
一直留着（`update_dismissed_tag`）；检查失败时静默，只写日志。**只提醒，不下载、
不静默安装、不自动重启**；开关默认开启，可在设置里关闭。详见 V0.6 开发计划 §9。
