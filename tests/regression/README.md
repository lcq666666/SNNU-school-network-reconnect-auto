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

## 当前状态（阶段 3 完成后）

**本目录已无待修复的回归用例**：阶段 1 登记的全部 `regression` 用例均已修复并并入
默认离线集合（现位于 `tests/unit/`），`pytest -m regression` 现在选中 0 条。

| 用例 | 现位置 | 关联问题 | 修复阶段 |
|---|---|---|---|
| T11 / T12 / T13 / T16 | `tests/unit/test_f06_portal_login.py` | F06（登录结构化校验） | 阶段 3 |
| T19 / T20 | `tests/unit/test_f09_cooldown.py` | F09（统一冷却） | 阶段 3 |
| T27 | `tests/unit/test_f15_empty_yys.py` | F15（空运营商保留） | 阶段 3 |
| T01 | `tests/unit/test_f01_concurrency.py` | F01（重连排他） | 阶段 2 |
| T05 / T06 / T07 | `tests/unit/test_f03_config.py` | F03（配置校验） | 阶段 2 |
| T08 | `tests/unit/test_f04_proxy.py` | F04（代理权限） | 阶段 2 |
| T23 / T24 | `tests/unit/test_f12_consent.py` | F12（授权版本） | 阶段 2 |
| T25 | `tests/unit/test_f13_save.py` | F13（原子持久化） | 阶段 2 |

阶段 2/3 新增的默认离线用例：`test_proxy_controller.py`（T08/T09/T10）、
`test_credential_store.py`（T22/T34）、`test_autostart_controller.py`、
`test_tray_worker.py`（T01/T02/T03/T04/T10/T21）、`test_f07_redirect.py`（T14/T15）、
`test_f08_classification.py`（T17/T18）、`test_f14_shared_service.py`（T26）、
`test_f16_port_resolution.py`（T28）、`test_f19_edge.py`（T31）、`test_f20_cli.py`（T32）。
