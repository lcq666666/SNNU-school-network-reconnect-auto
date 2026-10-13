# 回归用例集合（`regression` 标记）

本目录记录《审查方案》（`docs/main代码审查与修复方案_2026-10-13.md`）**第 10 节已离线复现的缺陷**，
即阶段 1「任务 1.4」要求转为失败用例的首批回归项。

## 使用方式

- 默认集合（`pytest`）**不运行**本目录：`pytest.ini` 中 `addopts = -m "not regression"`。
- 单独运行并观察“修复前失败”：

  ```bash
  .venv/Scripts/python.exe -m pytest -m regression
  ```

- 某条缺陷修复后，删除对应 `@pytest.mark.regression`（或整个 `pytestmark`），
  用例即进入默认集合，必须保持全绿。

## 约定

- **修复前必须失败**，且失败原因应与《审查方案》第 10 节一致；**禁止**用永久 `xfail`
  或 `skip` 掩盖缺陷。
- 全部使用 `tests/fakes.py` 的假件与 `tests/conftest.py` 的 fixture；
  零真实网络 / 注册表写入 / 凭据读取 / 浏览器启动 / 真实 sleep。

## 用例索引

| 用例 | 文件 | 关联问题 | 复现证据 |
|---|---|---|---|
| T05 | `test_f03_config.py` | F03 | 未知键被原样并入配置（未隔离） |
| T06 | `test_f03_config.py` | F03 | `interval="bad"`、bool 充当 int 被原样接受 |
| T07 | `test_f03_config.py` | F03 | 授权 `"false"`（字符串）被当作真值 |
| T08 | `test_f04_proxy.py` | F04 | 查询权限拒绝仍写入 `ProxyEnable=1`（部分写入） |
| T11 | `test_f06_portal_login.py` | F06 | HTTP 500 + 在线文字被判成功 |
| T12 | `test_f06_portal_login.py` | F06 | 账号子串（123456 vs 1234567）误匹配 |
| T27 | `test_f15_empty_yys.py` | F15 | 空运营商被改写为 `mobile` |
| T19 / T20 | `test_f09_cooldown.py` | F09 | 短暂恢复绕过 180s 冷却（0s/30s 各尝试一次） |
| T01 | `test_f01_concurrency.py` | F01 | 10 次手动点击产生多个并行认证任务 |
| T23 | `test_f12_consent.py` | F12 | 过期 consent 覆盖新的拒绝 |
| T24 | `test_f12_consent.py` | F12 | 保存无关设置把未知授权 bool 化并重写自启 |
| T25 | `test_f13_save.py` | F13 | 保存失败被静默吞掉、仍显示“已保存” |
