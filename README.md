# 校园网自动重连助手（SNNU · Windows · Clash Verge Rev）

让校园网掉线后自动重新认证，**全程不需要关闭 Clash Verge 的系统代理**。

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
2. 连上 SNNU Wi-Fi，托盘图标变绿即开始保护；
3. 首次重连成功时会询问是否同意"以后自动开启 Clash 系统代理"；
4. 右键菜单 → 开机自启 → 开启。

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
