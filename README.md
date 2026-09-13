# AxonHub for Home Assistant

[English](README.en.md) | **简体中文**

把 AxonHub 已经采集的**渠道供应商额度**——Claude Code、Codex、GitHub Copilot、NanoGPT
的订阅窗口——变成 Home Assistant 实体，这样你就能在仪表盘上看到"还剩多少额度"，
或者基于额度做自动化。

集成只读取 AxonHub **缓存**的额度状态，自己不会去请求上游 AI 供应商，因此轮询间隔调短
也不会消耗供应商的 API 调用。

> AxonHub 本体：[looplj/axonhub](https://github.com/looplj/axonhub)

## 前置条件

- **AxonHub v0.8.7 或更高版本。** `Channel.providerQuotaStatus` 自
  [PR #669](https://github.com/looplj/axonhub/pull/669) 引入，更早的版本不存在该字段，
  集成会明确提示需要升级。
- AxonHub 至少有一个已启用、类型为 `claudecode`、`codex`、`github_copilot`、
  `nanogpt` 或 `nanogpt_responses` 的渠道。
- 一个可以读取渠道的 AxonHub 账号：owner 账号，或拥有 `read:channels` 权限的角色/成员。
- Home Assistant 2024.12 或更高版本。

额度数据由 AxonHub 自己的后台任务采集，默认每 `provider_quota.check_interval`（20 分钟）
执行一次。在下次检查之前，实体显示的是最近一次的状态。

## 安装

### HACS（自定义仓库）

1. 打开 **HACS → 集成**。
2. 点击右上角 **⋮** 菜单 → **自定义仓库**。
3. 添加 `https://github.com/myml/ha-axonhub`，类别选择 **Integration**。
4. 安装 **AxonHub**，然后重启 Home Assistant。

### 手动安装

```bash
cp -r custom_components/axonhub /path/to/homeassistant/config/custom_components/
```

重启 Home Assistant。

## 配置

进入 **设置 → 设备与服务 → 添加集成 → AxonHub**，填写：

| 字段 | 说明 |
|------|------|
| AxonHub 地址 | 实例的根地址，例如 `http://homeassistant.local:8090`。可以不写协议（默认按 `http://` 处理）。 |
| 邮箱 / 密码 | 有权读取渠道的账号凭据。 |
| 校验 SSL 证书 | 使用自签名证书时关闭。 |

集成通过 `POST /admin/auth/signin` 登录，缓存 AxonHub 签发的 JWT（有效期 7 天）并自动
重新认证：过期前主动续期，被拒绝时（例如改过密码）立刻重新登录。凭据失效时 Home Assistant
会启动重新认证流程，而不是静默失败。

### 选项

**设置 → 设备与服务 → AxonHub → 配置**：

| 选项 | 默认值 | 说明 |
|------|--------|------|
| 轮询间隔 | 300 秒 | 读取缓存额度状态的频率，最小 30 秒。 |
| 校验 SSL 证书 | 开启 | 访问 AxonHub 时是否校验 TLS 证书。 |

## 实体

每个启用额度检查的渠道会成为一个独立的 Home Assistant 设备（名称即渠道名）。

| 实体 | 说明 |
|------|------|
| `sensor.<渠道>_quota_status` | 总体状态：`available`、`warning`、`exhausted` 或 `unknown`。 |
| `sensor.<渠道>_next_quota_reset` | 主要额度窗口的下次重置时间。 |
| `binary_sensor.<渠道>_quota_ready` | 渠道是否仍可服务（状态为 `available` 或 `warning` 时为 `on`）。 |

各供应商的窗口传感器：

| 供应商 | 实体 | 含义 |
|--------|------|------|
| Claude Code | `sensor.<渠道>_5h_window_used`、`sensor.<渠道>_7d_window_used`、`sensor.<渠道>_overage_window_used` | 5 小时、7 天与 overage 限流窗口的已用百分比。 |
| Codex | `sensor.<渠道>_primary_window_used`、`sensor.<渠道>_secondary_window_used` | 主/次限流窗口的已用百分比。 |
| GitHub Copilot | `sensor.<渠道>_<quota>_remaining`（例如 `sensor.copilot_premium_interactions_remaining`） | 各配额快照的剩余百分比。没有快照的账号按 `limited_user_quotas` / `total_quotas` 计算。 |
| NanoGPT | `sensor.<渠道>_weekly_input_tokens_used`、`sensor.<渠道>_daily_input_tokens_used`、`sensor.<渠道>_daily_images_used` | 各订阅窗口的已用百分比。 |

说明：

- `<渠道>` 是 AxonHub 中渠道名转成的 slug，例如 `sensor.claude_max_5h_window_used`。
- 只有当供应商真的返回了某个窗口时才会创建对应实体。例如 Claude Code 的 `overage`
  窗口若为空，就不会产生实体。
- 状态实体在 `quota_data` 属性里带有 AxonHub 返回的原始数据；最近一次检查失败时还会有
  `error` 属性，适合用来做模板传感器。

### 服务 `axonhub.refresh_quotas`

让 AxonHub 立刻重新检查所有供应商额度，而不必等下一次后台周期：

```yaml
service: axonhub.refresh_quotas
data:
  entry_id: 01J8Z6...   # 可选；留空表示刷新全部已配置实例
```

该调用会一直阻塞到 AxonHub 检查完所有渠道为止；由于 AxonHub 是逐个渠道串行检查的，
渠道较多时需要等一会儿。这是唯一会真正请求供应商 API 的操作。

## 自动化示例

Claude Code 窗口快用满时告警：

```yaml
automation:
  - alias: Claude Code 额度偏低
    triggers:
      - trigger: numeric_state
        entity_id: sensor.claude_max_5h_window_used
        above: 80
    actions:
      - action: notify.persistent_notification
        data:
          message: >-
            Claude Code 5 小时窗口已用
            {{ states('sensor.claude_max_5h_window_used') }}%，
            将于 {{ states('sensor.claude_max_next_quota_reset') }} 重置。
```

Claude Code 额度耗尽时告警：

```yaml
automation:
  - alias: Claude Code 额度耗尽
    triggers:
      - trigger: state
        entity_id: binary_sensor.claude_max_quota_ready
        to: "off"
        for: "5m"
    actions:
      - action: notify.persistent_notification
        data:
          message: Claude Code 渠道已不可用，请检查额度。
```

查看距离重置还有多久：

```jinja
{{ (states('sensor.claude_max_next_quota_reset') | as_datetime - now()) }}
```

## 工作原理

额度内容由 AxonHub 决定，集成只做翻译。同时支持两种数据结构，因此新旧 AxonHub 都能用：

- **AxonHub v1.0.0-beta8 及以上**会把各 provider 的额度统一归一化到 `quotaData._limits`
  数组（`window`、`usageRatio`，与 provider 无关）。集成优先使用它，这也是它能覆盖没有专门
  解析器的 provider（Command Code、OpenCode Go、Cline、ZenMux 等）的原因。窗口传感器为
  `sensor.<渠道>_<窗口>_used`（百分比），AxonHub 的 `period_cost` / `period_quota` 会作为属性暴露。
- **更早的版本**（自 v0.8.7 引入 `Channel.providerQuotaStatus` 起）存的是各 provider 的原始
  payload（`windows`、`rate_limit`、`quota_snapshots` 等），集成按 provider 类型分别解析。

集成查询里必须带上 `providerQuotaStatus.providerType`：AxonHub 的解析器会读取该字段来判断
该 provider 是否启用了额度采集，而 ent 只会加载被查询选中的列——不请求它时解析器读到零值，
会让所有渠道的该字段一起失败。这是实测踩到的坑，`tests/test_api.py` 里有对应的回归测试。


```
Home Assistant                        AxonHub
──────────────                        ───────
POST /admin/auth/signin        ──▶    邮箱/密码 → 有效期 7 天的 JWT
POST /admin/graphql            ──▶    queryChannels → providerQuotaStatus
                                      (status, nextResetAt, ready, quotaData)
```

之所以调用管理端 GraphQL，是因为它是 AxonHub 唯一暴露额度数据的入口——基于 API Key 的
`/openapi/v1/graphql` 只允许创建 API Key。各供应商 `quotaData` 的字段结构记录在
`custom_components/axonhub/quota.py` 的模块注释里。

## 故障排查

| 现象 | 原因 / 处理 |
|------|-------------|
| 添加集成时报 `invalid_auth` | 邮箱或密码错误，或账号未激活。 |
| 报 `cannot_connect` | 地址/端口不对，Home Assistant 访问不到 AxonHub，或 TLS 校验失败。自签名证书请关闭"校验 SSL 证书"。 |
| 报 `insufficient_permissions` | 该账号没有读取渠道的权限。请使用 owner 账号，或授予 `read:channels` 权限。 |
| 报 `unsupported_version` | 该实例低于 AxonHub v0.8.7，没有供应商额度接口。请升级 AxonHub。 |
| 报 `api_error` 并附带文字 | 表单会显示 AxonHub 返回的原始错误，同一内容也会以 WARNING 级别写入日志（**设置 → 系统 → 日志**）。 |
| 没有任何渠道设备 | 渠道未启用，或类型不属于上面列出的可查额度类型。没有额度检查器的渠道会被有意忽略。 |
| 状态一直是 `unknown`，或缺少窗口传感器 | AxonHub 还没产出额度数据（首次检查未执行），或供应商检查失败。请看状态实体的 `error` 属性和 AxonHub 日志。 |
| 实体消失 | 渠道被禁用、删除，或类型改成了没有额度检查器的类型；集成会自动移除过期实体。 |

## 安全说明

- 凭据保存在 Home Assistant 的配置项中（与其它用户名/密码型集成一致）。如果部署支持，
  建议使用只有 `read:channels` 权限的账号。
- 除了你配置的 AxonHub 实例，不会向任何其它地方发送数据。

## 开发

除 Home Assistant 自身外，集成没有第三方运行时依赖。

```bash
python -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest -q
```

测试分为两部分：

- `tests/test_integration.py`：用真实 Home Assistant 实例 + mock 的客户端，验证配置流、
  实体/设备创建、`refresh_quotas` 服务以及重新认证流程。
- `tests/test_api.py`：在 localhost 起一个 mock AxonHub 服务端，用真实 `aiohttp` 客户端
  跑通登录、GraphQL 请求体、JWT 失效后自动重登、错误映射与四个供应商的额度解析。

CI 还会跑 `hassfest` 和 HACS 校验，见
[`.github/workflows/validate.yml`](.github/workflows/validate.yml)。

## 发布

HACS 安装的是 `custom_components/axonhub/manifest.json` 里声明的 `version`，而**检查更新**
依赖 GitHub Release。所以每次发版都要有对应的 tag 和 release：

1. 修改 `custom_components/axonhub/manifest.json` 中的 `version`（例如 `0.2.0`）。
2. 提交并推送。
3. 打 tag 并推送：

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

4. 为该 tag 发布 GitHub Release（`gh release create v0.2.0`，或在网页
   *Releases → Draft a new release* 中操作）。

第 3、4 步缺一不可：只有 tag 没有 release 时，HACS 会一直显示已安装的版本，不会提示更新。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。本项目为
[AxonHub](https://github.com/looplj/axonhub) 编写并镜像其供应商额度数据格式。
