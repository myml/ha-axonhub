# AxonHub for Home Assistant

**English** | [简体中文](README.md)

A Home Assistant custom integration that surfaces the **provider quota** AxonHub
already tracks for each channel — the subscription windows of **Claude Code**,
**Codex**, **GitHub Copilot** and **NanoGPT** — as Home Assistant entities, so you
can watch "how much quota is left" on a dashboard or automate on it.

The integration only reads AxonHub's *cached* quota state. It never talks to the
upstream AI providers itself, so a short polling interval does not cost you any
provider API calls.

> AxonHub itself lives at [looplj/axonhub](https://github.com/looplj/axonhub).

## Requirements

- **AxonHub v0.8.7 or newer.** `Channel.providerQuotaStatus` (added in
  [PR #669](https://github.com/looplj/axonhub/pull/669)) only exists from that release on;
  older instances reject the query and the integration tells you to upgrade.
- AxonHub with at least one enabled channel of type `claudecode`, `codex`,
  `github_copilot`, `nanogpt` or `nanogpt_responses`.
- An AxonHub account allowed to read channels: the owner account, or a
  role/membership holding the `read:channels` scope. Seeing the instance wide
  request/token statistics as well needs the `read:dashboard` scope (the owner
  account has both).
- Home Assistant 2024.12 or newer.

Quota data is collected by AxonHub's own background check, which runs every
`provider_quota.check_interval` (20 minutes by default, see the
[AxonHub configuration reference](https://github.com/looplj/axonhub/blob/main/config.example.yml)).
Entities show the last known state until that check runs again.

## Installation

### HACS (custom repository)

1. In Home Assistant open **HACS → Integrations**.
2. Click the **⋮** menu → **Custom repositories**.
3. Add `https://github.com/myml/ha-axonhub` with category **Integration**.
4. Install **AxonHub** and restart Home Assistant.

### Manual

Copy the `custom_components/axonhub` directory into your Home Assistant
configuration directory and restart:

```bash
cp -r custom_components/axonhub /path/to/homeassistant/config/custom_components/
```

## Configuration

Go to **Settings → Devices & Services → Add Integration → AxonHub** and fill in:

| Field | Description |
|-------|-------------|
| AxonHub URL | Base URL of your instance, for example `http://homeassistant.local:8090`. A scheme is optional (`http://` is assumed). |
| Email / Password | Credentials of an account allowed to read channels. |
| Verify SSL certificate | Turn off for self-signed certificates. |

The integration signs in through `POST /admin/auth/signin`, caches the JWT that
AxonHub issues (valid for 7 days) and re-authenticates automatically — proactively
before it expires and reactively if AxonHub rejects it. If the credentials stop
working, Home Assistant starts a re-authentication flow instead of failing silently.

### Options

**Settings → Devices & Services → AxonHub → Configure**:

| Option | Default | Description |
|--------|---------|-------------|
| Poll interval | 300 s | How often Home Assistant reads the cached quota state. Minimum 30 s. |
| Verify SSL certificate | on | TLS verification for the AxonHub URL. |

## Entities

Every quota-enabled channel becomes its own Home Assistant device named after the
channel.

| Entity | Description |
|--------|-------------|
| `sensor.<channel>_quota_status` | Overall status: `available`, `warning`, `exhausted` or `unknown`. |
| `sensor.<channel>_next_quota_reset` | Timestamp of the next reset of the primary quota window. |
| `binary_sensor.<channel>_quota_ready` | `on` while the channel can still serve requests (status `available` or `warning`). |

Provider specific window sensors:

| Provider | Entities | Meaning |
|----------|----------|---------|
| Claude Code | `sensor.<channel>_5h_window_used`, `sensor.<channel>_7d_window_used`, `sensor.<channel>_overage_window_used` | Utilization of the 5-hour, 7-day and overage rate limit windows, in percent. |
| Codex | `sensor.<channel>_primary_window_used`, `sensor.<channel>_secondary_window_used` | Utilization of the primary and secondary rate limit windows, in percent. |
| GitHub Copilot | `sensor.<channel>_<quota>_remaining` (for example `sensor.copilot_premium_interactions_remaining`) | Remaining percentage per quota snapshot. Accounts without snapshots get the value computed from `limited_user_quotas` / `total_quotas`. |
| NanoGPT | `sensor.<channel>_weekly_input_tokens_used`, `sensor.<channel>_daily_input_tokens_used`, `sensor.<channel>_daily_images_used` | Utilization of each subscription window, in percent. |

Notes:

- `<channel>` is the slugified channel name shown in AxonHub, for example
  `sensor.claude_max_5h_window_used`.
- Window sensors only appear when the provider actually reports that window. An
  empty Claude Code `overage` window, for instance, produces no entity.
- The status sensor carries the raw AxonHub payload in its `quota_data` attribute,
  plus `error` when the last provider check failed, which makes it a good source for
  template sensors.

### Instance statistics (requests, tokens)

Next to the channel devices, the integration puts a set of instance wide statistics on
the **AxonHub device** — the device with model `AI gateway` whose name is the URL you
configured:

| Entity | Description |
|--------|-------------|
| `sensor.<instance>_total_requests` | Every request AxonHub has tracked, failures included. Attributes: `failed_requests`, `success_rate`. |
| `sensor.<instance>_failed_requests` | Requests that ended in a failure. |
| `sensor.<instance>_requests_today` / `_this_week` / `_this_month` | Request count of the current calendar day / week / month in AxonHub's timezone. |
| `sensor.<instance>_requests_last_week` | Previous week, for week-over-week comparisons. |
| `sensor.<instance>_tokens_today` / `_this_week` / `_this_month` / `_all_time` | Total tokens per window. Attributes: `input_tokens`, `output_tokens`, `cached_tokens`, `cache_hit_rate`. |
| `binary_sensor.<instance>_statistics_available` | Whether AxonHub returns the statistics. `off` means the account lacks the `read:dashboard` scope (or the AxonHub build is too old); the statistics entities then read `unavailable` while **every quota entity keeps working**. |

Notes:

- `<instance>` is the slugified config entry title. A URL of `192.168.0.100:18090`, for
  example, yields `sensor.192_168_0_100_18090_total_requests`.
- `total_requests` and the window counters (`requests_today`, …) come from two different
  AxonHub tables, so they are not supposed to match: the former counts AxonHub's request
  process rows (failures included), the latter counts requests that produced a result —
  the numbers the AxonHub dashboard charts.
- A window's total is input + output + cached. AxonHub keeps cached prompt tokens *out*
  of `prompt_tokens`, so the integration adds them back and exposes the derived
  `cache_hit_rate` (cached ÷ (input + cached)).
- The counters use the `total_increasing` state class, so Home Assistant can chart daily
  and weekly deltas even though the calendar windows reset. `requests_last_week` is a
  fixed past window and stays a plain measurement.
- These values come from AxonHub's `dashboardOverview` / `tokenStats` queries and need an
  account with the `read:dashboard` scope (the owner account has it). That scope is
  independent of the `read:channels` the quotas need, and the integration polls them with
  a separate coordinator, so a missing scope only hides the entities above.

Dashboard card example:

```yaml
type: entities
title: AxonHub
entities:
  - entity: sensor.192_168_0_100_18090_requests_today
  - entity: sensor.192_168_0_100_18090_requests_this_week
  - entity: sensor.192_168_0_100_18090_total_requests
  - entity: sensor.192_168_0_100_18090_tokens_today
  - entity: sensor.192_168_0_100_18090_tokens_this_week
  - type: attribute
    entity: sensor.192_168_0_100_18090_tokens_this_week
    attribute: cache_hit_rate
    name: Cache hit rate
```

### Service `axonhub.refresh_quotas`

Asks AxonHub to re-check every provider quota immediately instead of waiting for the
next background interval.

```yaml
service: axonhub.refresh_quotas
data:
  entry_id: 01J8Z6...   # optional; omit to refresh every configured instance
```

The call blocks until AxonHub has finished checking every channel, which may take a
while because AxonHub checks the channels sequentially. It is the only operation
that reaches out to the provider APIs.

## Examples

Alert when a Claude Code window is nearly exhausted:

```yaml
automation:
  - alias: Claude Code quota running low
    triggers:
      - trigger: numeric_state
        entity_id: sensor.claude_max_5h_window_used
        above: 80
    actions:
      - action: notify.persistent_notification
        data:
          message: >-
            Claude Code 5h window at
            {{ states('sensor.claude_max_5h_window_used') }}%,
            resets {{ states('sensor.claude_max_next_quota_reset') }}.
```

Show how long is left in a reset window:

```jinja
{{ (states('sensor.claude_max_next_quota_reset') | as_datetime - now()) }}
```

## How it works

AxonHub decides what to report; this integration only translates it. Two payload
shapes are supported, so both older and newer AxonHub releases work:

- **AxonHub v1.0.0-beta8 and newer** store every provider's limits in a normalized
  `quotaData._limits` array (`window`, `usageRatio`, provider-independent). The
  integration prefers this, which is why it also covers providers it has no
  bespoke parser for (Command Code, OpenCode Go, Cline, ZenMux, ...). Window
  sensors are `sensor.<channel>_<window>_used` in percent, and `period_cost` /
  `period_quota` from AxonHub show up as attributes.
- **Older releases** (from v0.8.7, which introduced `Channel.providerQuotaStatus`)
  store a per-provider raw payload instead (`windows`, `rate_limit`,
  `quota_snapshots`, ...), which the integration parses per provider type.

The query must select `providerQuotaStatus.providerType`: AxonHub's resolver reads that
field to decide whether quota collection is enabled for the provider, and ent only loads
selected columns. Omitting it makes the resolver see the zero value and fail for every
channel. `tests/test_api.py` has a regression test for this.


```
Home Assistant                        AxonHub
──────────────                        ───────
POST /admin/auth/signin        ──▶    email/password → 7-day JWT
POST /admin/graphql            ──▶    queryChannels → providerQuotaStatus
                                      (status, nextResetAt, ready, quotaData)
                               ──▶    dashboardOverview + tokenStats
                                      (request and token counters, instance wide)
```

The admin GraphQL endpoint is used because it is the only AxonHub surface that
exposes quota data; the API-key based `/openapi/v1/graphql` endpoint only allows
creating API keys. Per-provider `quotaData` shapes are documented in
`custom_components/axonhub/quota.py`, the instance statistics in
`custom_components/axonhub/stats.py`.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `invalid_auth` while adding the integration | Wrong email or password, or the account is not activated. |
| `cannot_connect` | Wrong URL/port, AxonHub not reachable from Home Assistant, or a TLS failure. Disable *Verify SSL certificate* for self-signed certificates. |
| `insufficient_permissions` | The account cannot read channels. Use the owner account or grant the `read:channels` scope. |
| `unsupported_version` | The instance is older than AxonHub v0.8.7 and has no provider quota API. Upgrade AxonHub. |
| `api_error` with a message | The form shows the exact error AxonHub returned; the same text is logged at WARNING level (`Settings → System → Logs`). |
| No channel devices appear | The channel is not enabled, or its type is not one of the quota-enabled types listed above. Channels without a quota checker are ignored on purpose. |
| Status stays `unknown` or window sensors are missing | AxonHub has not produced quota data yet (first check pending), or the provider check failed. Inspect the `error` attribute of the status sensor and the AxonHub logs. |
| Entities disappear | The channel was disabled, deleted or changed to a type without a quota checker; the integration removes stale entities automatically. |
| Statistics entities read `unavailable` and `binary_sensor.<instance>_statistics_available` is `off` | The account lacks the `read:dashboard` scope (or the AxonHub build is too old). Use the owner account or grant `read:dashboard`. Quota entities are unaffected. |

## Security notes

- Credentials are stored in the Home Assistant config entry, as with any
  username/password integration. Use an account with only the `read:channels`
  scope if your deployment supports it.
- Nothing is sent anywhere except to the AxonHub instance you configure.

## Development

The integration has no third-party dependencies beyond Home Assistant itself.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest -q
```

The test suite has two parts:

- `tests/test_integration.py` sets up a real Home Assistant instance with a mocked
  AxonHub client and asserts the config flow, entity/device creation, the
  `refresh_quotas` service and the re-authentication flow.
- `tests/test_api.py` runs a mock AxonHub server on localhost and drives the real
  `aiohttp` client through sign-in, the GraphQL request shape, transparent
  re-authentication, error mapping and the quota parsing of all four providers.

CI additionally runs `hassfest` and the HACS validation action, see
[`.github/workflows/validate.yml`](.github/workflows/validate.yml).

## Release

HACS installs the version declared in `custom_components/axonhub/manifest.json` and
detects updates through GitHub **releases**, so every version needs a matching tag
and release:

1. Bump `version` in `custom_components/axonhub/manifest.json` (for example
   `0.2.0`).
2. Commit and push.
3. Tag the commit and push the tag:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

4. Publish a GitHub release for that tag (with `gh release create v0.2.0` or via
   *Releases → Draft a new release*).

Steps 3 and 4 are both required: without a release HACS keeps reporting the version
it installed and never offers an update.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE) — the integration is written
for [AxonHub](https://github.com/looplj/axonhub) and mirrors its provider quota data
formats.
