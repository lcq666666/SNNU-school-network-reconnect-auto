# 回归用例集合（`regression` 标记）

本目录记录《审查方案》（`docs/main代码审查与修复方案_2026-10-13.md`）**第 10 节已离线复现、
但尚未修复**的缺陷用例。修复前**必须失败**，修复后移出 `regression` 标记并并入默认集合。

## 使用方式

- 默认集合（`pytest`）**不运行**本目录：`pytest.ini` 中 `addopts = -m "not regression"`。
- 单独运行并观察“修复前失败”：

  ```bash
  .venv/Scripts/python.exe -m pytest -m regression
  ```

- 某条缺陷修复后，删除对应 `pytestmark = pytest.mark.regression`（或整个 `pytestmark`），
  并把文件移入 `tests/unit/`，用例即进入默认集合，必须保持全绿。

## 约定

- **修复前必须失败**，且失败原因应与《审查方案》第 10 节一致；**禁止**用永久 `xfail`
  或 `skip` 掩盖缺陷。
- 全部使用 `tests/fakes.py` 的假件与 `tests/conftest.py` 的 fixture；
  零真实网络 / 注册表写入 / 凭据读取 / 浏览器启动 / 真实 sleep。

## 当前用例索引（阶段 2 完成后剩余项）

| 用例 | 文件 | 关联问题 | 主责阶段 | 复现证据 |
|---|---|---|---|---|
| T11 / T12 | `test_f06_portal_login.py` | F06 | 阶段 3 | HTTP 500 + 在线文字被判成功；账号子串（123456 vs 1234567）误匹配 |
| T19 / T20 | `test_f09_cooldown.py` | F09 | 阶段 3 | 短暂恢复绕过 180s 冷却（0s/30s 各尝试一次） |
| T27 | `test_f15_empty_yys.py` | F15 | 阶段 3 | 空运营商被改写为 `mobile` |

## 阶段 2 已转绿并移出的用例

以下用例在阶段 2 修复后已并入默认离线集合（现位于 `tests/unit/`）：

| 用例 | 文件 | 关联问题 |
|---|---|---|
| T01 | `tests/unit/test_f01_concurrency.py` | F01（重连排他 / 单网络工作线程） |
| T05 / T06 / T07 | `tests/unit/test_f03_config.py` | F03（配置校验与授权三态） |
| T08 | `tests/unit/test_f04_proxy.py` | F04（代理权限与部分写入） |
| T23 / T24 | `tests/unit/test_f12_consent.py` | F12（授权版本 / 只保存变更项） |
| T25 | `tests/unit/test_f13_save.py` | F13（原子持久化与保存反馈） |

阶段 2 另新增（同样位于 `tests/unit/`）：`test_proxy_controller.py`（T08/T09/T10）、
`test_credential_store.py`（T22/T34）、`test_autostart_controller.py`、
`test_tray_worker.py`（T01/T02/T03/T04/T10/T21）。
