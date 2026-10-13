# 校园网自动重连助手（SNNU · Windows · Clash Verge Rev）

让校园网掉线后自动重新认证，**全程不需要关闭 Clash Verge 的系统代理**。

本目录文件：

| 文件 | 作用 |
|---|---|
| `campus_reconnect.py` | 主程序/引擎（掉线检测 / 自动认证 / 状态恢复验证，命令行版） |
| `campus_reconnect_tray.py` | 托盘版（任务栏图标 + 气泡通知 + 设置面板，推荐日常使用） |
| `run.bat` | 无窗口启动命令行版常驻监测 |
| `run_tray.bat` | 启动托盘版（优先用 exe，没有 exe 时退回脚本） |
| `CampusReconnect.exe` | 打包版托盘应用（单文件，约 33MB，无需 Python，见下） |
| `docs/` | 方案流程图 |
| `logs/` | 运行日志（自动轮转，排查问题看这里；不纳入仓库） |

## 0. 环境实测结论（2026-10-13，在本机实践验证）

- Portal 首页 `http://202.117.144.205/` 会 JS 跳转到认证页，认证端口在
  **8601 / 8602 / 8603 / 8605 / 8609 之间轮询**，因此脚本每次动态解析跳转端口，不写死。
- 登录接口：`POST http://202.117.144.205:<端口>/snnuportal/login`，
  字段 `sourceurl=null, account, password, yys, issave=`。登录页当前无验证码。
- 认证按 **IP** 生效；失败的登录**不会**影响已有在线会话（已用假账号验证）。
- 本机环境：Clash Verge **Rev**、仅开启系统代理（127.0.0.1:7897）、**未开 TUN**、规则模式；
  Wi-Fi 为 `SNNU`（开放式）。Python 3.14（`py` 启动器默认版本）。
- 首次使用可执行 `py campus_reconnect.py fix-bypass`，把 Portal 地址即时写入系统代理
  绕过列表（重启 Verge 或切换代理开关后会丢失，仍需按第 1 步在界面里配置）。

## 1. 第一步：让 Edge 在系统代理开启时也能打开登录页（永久生效）

这是"必须手动关代理"的根因解法。在 Clash Verge Rev 界面配置一次：

1. 打开 Verge → **设置** → **系统代理** → 找到"代理绕过 / Bypass"输入框；
2. 在已有内容**末尾追加**（英文分号分隔，**不要删除默认条目**，不要加 `http://`）：

   ```
   202.117.144.205;net.snnu.edu.cn
   ```

3. 保存后，把系统代理开关关一次再打开，使其写入注册表。

> `fix-bypass` 只是即时补丁：Verge 在切换系统代理开关时会用自身设置重写注册表，
> 所以请在界面里按上面步骤配置一次，才能永久生效。未开启 TUN 时以上即足够；
> 若以后开启 TUN，需在分流规则中加 `IP-CIDR,202.117.144.205/32,DIRECT`。

## 2. 安装依赖并保存账号密码

在命令行终端进入本目录：

```bat
py -m pip install requests keyring
py campus_reconnect.py configure
```

`configure` 会交互式询问账号和密码，只写入**当前 Windows 用户的凭据管理器**
（控制面板 → 凭据管理器 → Windows 凭据，名称 `SNNU-Campus-Reconnect`），
不写入任何文件，也不会在日志中留下密码。

## 3. 自检（先跑这个）

```bat
py campus_reconnect.py status
```

正常应看到：`无线网卡状态: 已连接（SSID: SNNU）`、`Portal 可达: 是`、
两个公网探测 `通过`、`代理绕过列表: 已包含 Portal 地址`。

## 4. 预检与测试（按此顺序，不要跳步）

**第一步：预检（不发送任何认证请求，安全）**

```bat
py campus_reconnect.py once --dry-run
```

应看到：`SSID/状态: SNNU / 已连接`、`认证端口` 及其识别来源、`登录页可达: 是`、
`凭据已保存: 是`，以及将提交的载荷（密码显示为 `***已隐藏***`）。

**第二步：前台常驻监测（正常上网时只探测，不发认证请求）**

```bat
py campus_reconnect.py watch --ssid SNNU --yys mobile
```

让它保持运行。日常上网期间它只做公网探测；**只有在真实掉线时才会自动认证**，
这才是对接口最真实的验证。成功标志：日志出现 `连续 3 次失败` → `Portal 认证端口` →
`登录结果: 成功` → `网络恢复成功`。

**第三步（可选，进阶）：--force 在线强制认证**

```bat
py campus_reconnect.py once --force --browser-fallback
```

`--force` 会在**网络正常时也提交一次真实凭据**，仅用于接口联调，平时不需要；
脚本会打印醒目警告。若返回 `Portal 返回失败信息: 密码错误...`：先确认密码，
再试 `--account-format isp`（以 `学号@mobile` 形式提交账号）。
`--browser-fallback`：认证失败时自动打开一个**不走系统代理**的 Edge 登录页，可人工完成。

## 5. 常驻监测（命令行版）

```bat
py campus_reconnect.py watch --ssid SNNU --yys mobile
```

工作逻辑：每 15 秒做一次**不走系统代理**的公网探测（Microsoft + Apple 双探测），
连续 3 次失败判定掉线 → 确认 Wi-Fi 是 SNNU → 解析认证端口 → 检查登录页可达 →
用凭据管理器中的账号按运营商（默认 mobile 移动）重新认证 → 以状态页 + 公网探测
双重确认恢复。两次尝试至少间隔 180 秒，防止校园网维护期间反复重登。

端口解析优先级（脚本会记录实际来源，不会静默处理）：
**用户 `--port` 指定 > Portal 首页跳转动态识别 > 已知端口（8601/8602/8603/8605/8609）试探 > 回退 8601（明确告警）**。

单实例保护：脚本内置互斥体，重复启动的第二个 watch 会直接退出并提示，
因此 `run.bat` 重复双击、或与任务计划叠加运行都不会造成双重监控。
`Ctrl+C` 停止；日志见 `logs/campus_reconnect.log`。

## 6. 托盘版（推荐日常使用）

不满足于黑窗口，就用托盘版：任务栏右下角一个小图标，**右键菜单 + 气泡通知 + 设置面板**，
掉线→重连→恢复的每一步都看得见。

```bat
py -m pip install pystray pillow
py campus_reconnect_tray.py
```

或双击 `run_tray.bat`（无窗口启动）。图标四态：**绿 = 在线 / 黄 = 重连中 / 红 = 已断开 /
灰 = 已暂停**，悬停显示当前状态与认证端口。

气泡通知覆盖三个关键时刻：

| 时机 | 通知内容 |
|---|---|
| 启动 | 校园网助手已启动 · Wi-Fi 与网络状态 |
| 掉线 | 校园网已断开 · 正在尝试重新连接，请稍候… |
| 成功 | 重连成功 · 用时约 N 秒 · （如开启联动）已自动开启 Clash 系统代理 |
| 失败 | 重连失败 · 具体原因 · （如开启）已打开 Edge 兜底 |

右键菜单：状态 / 立即重连 / 暂停·恢复 / 开机自启 / 重连后自动开启 Clash 系统代理 /
Edge 兜底 / 设置面板 / 打开日志 / 退出。

**重连后自动开启 Clash 系统代理**：首次重连成功时会弹窗询问"是否同意以后每次重连成功后
自动开启 Clash 的系统代理"，选择记入 `tray_config.json`，之后随时可在菜单或设置面板更改。
实现方式是写注册表 `ProxyEnable`（Clash Verge 本身就是这么切的），即时生效；唯一局限：
Verge 界面上的开关显示可能滞后到你手动切一次，功能不受影响。

**开机自启开关**：菜单一键切换，写 `HKCU\...\Run` 注册表项 `CampusReconnectTray`，
登录 Windows 后自动启动托盘版；关闭即删除该项。设置面板里可改检测间隔、失败阈值、
冷却时间、Wi-Fi 名称与运营商。

> 托盘版与命令行版共用同一个单实例互斥体，同时只有一个在跑；用了托盘版就不必再手动开 `watch`。

## 7. 开机自启：两种方式

| 方式 | 适用 | 操作 |
|---|---|---|
| Run 注册表项（推荐） | 托盘版 | 托盘菜单 → 开机自启 → 开启 |
| 任务计划程序 | 命令行版 | 见下方步骤 |

如需用任务计划自启命令行版：

1. `Win+S` 搜索"任务计划程序" → **创建任务**（不要用"基本任务"）；

1. `Win+S` 搜索"任务计划程序" → **创建任务**（不要用"基本任务"）；
2. 常规：名称 `CampusReconnect`，**仅当用户登录时运行**，勾选**使用最高权限**；
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
7. 设置：勾选**"如果任务已在运行，则不启动新实例"**，并去掉"如果任务运行超过..."
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

## 9. 安全与边界

- **不执行注销（logoff）**：不会把已有会话踢掉，也不影响同账号其他设备；
- **不修改 Clash Verge 配置、不动系统代理开关**：脚本的探测流量用 `trust_env=False` 强制直连；
- **密码只在 Windows 凭据管理器**，不落盘、不进日志、不出现在本仓库任何文件；
- 学校 Portal 是明文 HTTP，认证报文本身不加密——这是学校系统的固有限制，脚本无法改变，
  也因此**不要把账号密码、Cookie 发给任何人**；
- 若不再使用，在凭据管理器中删除 `SNNU-Campus-Reconnect` 两条记录即可。

## 10. 故障排查

| 现象 | 处理 |
|---|---|
| status 里探测未通过但能正常上网 | 探测地址被限制：改 `DEFAULT_PROBES`（任意返回 200 且内容匹配的地址） |
| 登录总是失败、提示失败信息 | 试 `--account-format isp`；确认 `--yys` 与你在页面选的运营商一致 |
| 认证端口全部不通 | `--port 8601` 或 `8602` 等手动指定（可选值 8601/8602/8603/8605/8609） |
| 提示"已有一个监控实例在运行" | 正常保护：已有 watch 在跑；如需重启，先结束旧实例 |
| Portal 改版（字段/验证码变化） | Edge F12 → 网络，手动登录一次，对照 `login.js` 修改 `portal_login` |
| Verge 切换代理开关后 Edge 又打不开登录页 | 按第 1 步在 Verge 界面补回绕过地址，或重跑 `fix-bypass` |
| 后台无窗口运行无任何反应 | 看 `logs/campus_reconnect.log`（`pythonw` 不显示控制台） |
