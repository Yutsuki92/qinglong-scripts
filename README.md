# 青龙面板签到脚本集

自用的青龙面板（QingLong 2.23）自动化脚本集合，包含每日签到与活动任务。
所有脚本均为**单文件、可独立运行**。

---

## 脚本清单

脚本按用途分为两类：**签到脚本**（每日例行签到）与**活动脚本**（活动任务、互动玩法）。
两类都放在仓库根目录，订阅后青龙会按各自的 `new Env('...')` 建立独立任务。

### 签到脚本

| 脚本 | 青龙任务名 | 默认 cron | 必需环境变量 | 示例（单账号） |
|---|---|---|---|---|
| `aliyunpan_checkin.py` | 阿里云盘签到 | `0 0 * * *` | `ALIYUN_REFRESH_TOKEN` | `eyJhbGciOiJSUzI1NiIs...`（阿里云盘 refresh_token） |
| `quark_checkin.py` | 夸克自动签到 | `0 0 * * *` | `QUARK_COOKIE` | `user=张三; kps=abc123; sign=def456; vcode=123456;` |
| `ty_netdisk_checkin.py` | 天翼云盘签到 | `0 0 * * *` | `TY_ACCOUNTS` | `13800138000:你的密码` |
| `enshan_checkin.py` | 恩山论坛签到 | `0 0 * * *` | `ENSHAN_COOKIE` | `cdb_sid=xxxx; cdb_auth=xxxx; saltkey=xxxx` |
| `smzdm_checkin.py` | 什么值得买签到 | `0 0 * * *` | `SMZDM_COOKIE` | `sess=xxxx; user=zhangsan` |
| `workbuddy_checkin.py` | WorkBuddy每日签到 | `0 0 * * *` | `WORKBUDDY_TOKEN` / `WORKBUDDY_TOKENS` | `123456789:eyJhbGciOiJIUzI1NiJ9...`（uid:Token） |

### 活动脚本

| 脚本 | 青龙任务名 | 默认 cron | 必需环境变量 | 示例（单账号） |
|---|---|---|---|---|
| `smzdm_tasks.py` | 什么值得买活动 | `0 0 * * *` | `SMZDM_COOKIE` | `sess=xxxx; user=zhangsan` |
| `workbuddy_buddy_travel.py` | WorkBuddy 宠物探险 | `30 0 * * *` | `WORKBUDDY_TOKEN` / `WORKBUDDY_TOKENS` | `123456789:eyJhbGciOiJIUzI1NiJ9...`（uid:Token） |

> 两张表的示例均为**单个账号**写法。配置**多账号时每行一个**，例如：
>
> ```
> user=张三; kps=abc123; sign=def456; vcode=123456;
> user=李四; kps=xyz789; sign=uvw012; vcode=654321;
> ```

除 `workbuddy_buddy_travel.py`（宠物探险，安排在 **00:30**）外，其余脚本统一在每天 **00:00** 触发。
脚本内置启动随机延迟（`MAX_RANDOM_DELAY`，默认 600 秒），会把实际请求自动分散到 00:00 ~ 00:10 之间，无需手动错峰。

> `workbuddy_checkin.py` 与 `workbuddy_buddy_travel.py` 共用同一组 Token 变量
> （`WORKBUDDY_TOKEN` / `WORKBUDDY_TOKENS`），只需配置一次。
> `smzdm_checkin.py` 与 `smzdm_tasks.py` 共用 `SMZDM_COOKIE`，同样只需配置一次。

每个脚本头部都带 `cron:` 与 `new Env('...')` 注释，青龙订阅后会自动识别并创建对应任务。
如需调整执行时间，直接改脚本头部注释中的 cron，或订阅后在面板「定时任务」里改。

---

## 青龙面板订阅（公开仓库）

本仓库为**公开仓库**，青龙拉取无需 Token，也不需要配置任何访问凭证。

青龙提供两种拉库方式，任选其一。

### 1. 方式一：订阅管理（图形界面）

「订阅管理」→「新建订阅」，按下表填写：

| 字段 | 值 |
|---|---|
| 名称 | 随意，例如 `checkin` |
| 类型 | **公开仓库** |
| 链接 | `https://github.com/Yutsuki92/qinglong-scripts.git` |
| 分支 | `main` |
| 白名单 | 留空（拉取全部 8 个脚本）；只订阅签到脚本可填 `_checkin\.py`，只订阅活动脚本可填 `smzdm_tasks\.py\|workbuddy_buddy_travel\.py` |
| 黑名单 | 留空 |
| 依赖文件 | 留空（本仓库不依赖公共库文件） |
| 定时类型 | `crontab` |
| 定时规则 | `0 0 * * *`（每天 0 点自动同步仓库更新） |

### 2. 方式二：拉库命令（ql repo）

青龙的订阅也支持直接粘贴命令：填进「新建订阅」的**名称**栏，或在「定时任务」里新建一条任务执行。

```bash
# 拉取全部 8 个脚本
ql repo https://github.com/Yutsuki92/qinglong-scripts.git "" "" "" "main" "py"

# 只拉签到脚本
ql repo https://github.com/Yutsuki92/qinglong-scripts.git "_checkin\.py" "" "" "main" "py"

# 只拉活动脚本
ql repo https://github.com/Yutsuki92/qinglong-scripts.git "smzdm_tasks\.py|workbuddy_buddy_travel\.py" "" "" "main" "py"

# 定时拉取（每天 0 点自动同步仓库更新）：进入「定时任务」新建一条，执行上面的命令，定时规则填 0 0 * * *
```

参数顺序为：

```
ql repo <仓库地址> <白名单> <黑名单> <依赖文件> <分支> <文件后缀>
```

| 参数 | 本仓库填法 |
|---|---|
| 仓库地址 | `https://github.com/Yutsuki92/qinglong-scripts.git` |
| 白名单 | 留空＝全部；`_checkin\.py`＝只签到；`smzdm_tasks\.py\|workbuddy_buddy_travel\.py`＝只活动 |
| 黑名单 | 留空 |
| 依赖文件 | 留空（本仓库不依赖公共库文件） |
| 分支 | `main`（**必须显式指定**：不填会去找默认的 master，本仓库没有该分支） |
| 文件后缀 | `py` |
| 定时规则 | `0 0 * * *`（每天 0 点自动同步仓库更新） |

> 定时规则（如 `0 0 * * *`）在订阅管理界面或任务的定时字段单独设置，不写在命令里。

### 3. 国内加速（可选）

若面板所在网络访问 GitHub 较慢或超时，在仓库地址前拼接加速代理即可：

```bash
# 订阅管理界面「链接」栏填：
https://gh-proxy.org/https://github.com/Yutsuki92/qinglong-scripts.git

# ql repo 命令写法：
ql repo https://gh-proxy.org/https://github.com/Yutsuki92/qinglong-scripts.git "" "" "" "main" "py"
```

公开仓库用加速站是安全的：请求不携带任何凭证，不存在 Token 泄露风险。
加速站为第三方公益服务，稳定性不保证，若拉取失败换回直连地址即可。

单文件订阅（`ql raw`）同样可以套加速，例如：

```
https://gh-proxy.org/https://raw.githubusercontent.com/Yutsuki92/qinglong-scripts/main/aliyunpan_checkin.py
```

### 4. 配置环境变量

订阅会把脚本同步到面板，但**账号信息必须自己配**。
「环境变量」→「新建」逐个添加，名称见上方两张清单表。

多账号统一用**换行**分隔，例如：

```
ALIYUN_REFRESH_TOKEN = 账号1的token
                       账号2的token
```

### 5. 依赖

青龙「依赖管理」→「安装依赖」（批量导入同理），类型选 **Python**，粘贴以下清单：

```
requests
urllib3
rsa
```

---

## 公共环境变量

以下变量所有脚本通用，可只在面板里配一次：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RANDOM_SIGNIN` | `true` | 延迟开关。置 `false` 会关闭脚本内**全部等待**：启动随机延迟 + 账号间延迟 |
| `MAX_RANDOM_DELAY` | `600` | 启动随机延迟上限（秒）。脚本启动时在 `0 ~ 该值` 之间随机等待，用于错开请求高峰 |

> 推荐保持默认。调试脚本时若不想等，可临时设 `RANDOM_SIGNIN=false`——
> 但此时多账号会**连续发请求**，有触发平台风控的风险，仅建议调试时使用。
> 各脚本的账号间延迟统一为 10~20 秒。

---

## 各脚本专属可选变量

以下变量**全部可选**，不填即用默认值。公共变量（`RANDOM_SIGNIN` / `MAX_RANDOM_DELAY`）对所有脚本都生效，此处不再重复。

| 脚本 | 变量 | 默认值 | 说明 |
|---|---|---|---|
| `aliyunpan_checkin.py` | `AUTO_UPDATE_TOKEN` | `true` | 签到后自动把刷新过的 refresh_token 写回青龙环境变量；置 `false` 关闭 |
| `aliyunpan_checkin.py` | `PRIVACY_MODE` | `true` | 日志中的 Token 脱敏；置 `false` 显示完整值（排查问题时用） |
| `aliyunpan_checkin.py` | `SHOW_TOKEN_IN_NOTIFICATION` | `false` | 推送通知的正文里是否包含 Token |
| `enshan_checkin.py` | `PRIVACY_MODE` | `true` | 日志中的用户名脱敏；置 `false` 显示完整用户名 |
| `quark_checkin.py` | `QUARK_TIMEOUT` | `15` | 单次请求超时秒数；网络慢可调大 |
| `workbuddy_checkin.py` | `WB_USER_ID` | 空 | 兜底 `X-User-Id`，仅当无法从 Token 解析出 uid 时使用 |
| `workbuddy_checkin.py` | `WB_WARN_DAYS` | `3` | Token 剩余天数低于该值时，日志与通知中提示尽快更新 |
| `smzdm_tasks.py` | `SMZDM_TASK_MODULES` | `both` | 只跑指定模块：`zhongce`（众测）/ `interactive`（互动）/ `both`（全部） |
| `workbuddy_buddy_travel.py` | `WB_TRAVEL_LOCATION` | 空 | 派出地点，填 id 或 code：`1`/`coffee`、`2`/`mall`、`3`/`gym`、`4`/`ancient_town`；留空取第一个 |
| `workbuddy_buddy_travel.py` | `WB_USER_ID` | 空 | 同 `workbuddy_checkin.py`，两个脚本共用即可 |

> 以上变量的默认值即推荐值，通常无需配置。

---

## 通知

所有脚本统一使用**青龙面板内置通知**（`QLAPI.systemNotify`），无需 `notify.py`。

- 通知渠道在面板「系统设置 → 通知设置」里配置，脚本侧不做任何渠道判断。
- 通知属于附属能力：未配置、调用异常或面板无响应时一律静默跳过，
  **不会影响签到主流程**，也不会把任务标记为失败。

---

## 注意事项

- `workbuddy_buddy_travel.py`（宠物探险）单次运行可能持续 1~4 小时，取决于宠物归来时间。
  请确保面板任务超时足够长，且 cron 间隔大于单次运行时长，避免任务叠加。
- 所有脚本的账号信息一律通过环境变量传入，**源码中不含任何密钥**，可安全纳入版本控制。
- 本仓库为公开仓库，源码中不含任何账号凭据。请注意：**你的 Cookie / Token 只写在青龙环境变量里**，
  不要提交进任何分支或文件。
