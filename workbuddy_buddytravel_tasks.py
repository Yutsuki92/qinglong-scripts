#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 宠物探险与积分领取自动化（青龙面板版）。

cron: 30 0 * * *
new Env('WorkBuddy 宠物探险');

功能
    查询宠物探险状态，空闲时自动派出，归来后自动领取积分。
    单次运行内完成「派出 -> 等待 -> 领取」闭环，不依赖运行频率。

运行逻辑
    分两步执行，多账号同理：
        第 1 步 统一派出
            逐个查询 travel/status，按 state 分派：
                arrived    已归来，记为待领取
                traveling  已在探险中，记为待领取
                idle       未达上限则自动派出，并复查状态确认已出发
            今日已达上限、账号尚未激活宠物（depart 返回 no active buddy）等
            前置条件未满足的情况在此步跳过，不计为失败。
        第 2 步 依次等待领取
            按账号顺序逐个倒计时，归来后立即领取，一个完成再处理下一个。
    先统一派出使多账号的总时长取决于最晚归来的那个账号，而非各账号之和。

    幂等性：重复运行无副作用。空闲时调用 claim 返回 400，属预期结果。

等待与倒计时
    接口返回 arrive_at 时，以响应中的 server_now 计算剩余时间，每 60 秒打印一次
    倒计时，归零后立即领取。倒计时基于服务端时钟，不受容器本地时间影响。

    等待截止时间为「归来时刻 + 10 分钟」，保证倒计时归零后仍有机会领取；超时不计为
    失败，奖励保留至下次运行领取。接口未返回归来时间时本轮不等待，直接退出。

可选地点（WB_TRAVEL_LOCATION 支持 id 或 code）
    1 / coffee        咖啡馆
    2 / mall          商场店铺
    3 / gym           健身房
    4 / ancient_town  古镇客栈
    填写值不在列表内时，输出完整列表并回退到第一个地点。

注意事项
    单次运行可能持续 1~4 小时（取决于各账号归来时间中最晚的一个）。青龙面板需相应
    放宽任务超时，并避免 cron 间隔短于单次运行时长，以免任务叠加。

    退出码仅在网络异常、鉴权失效、接口异常时返回 1；未配置 Token、账号未激活宠物、
    今日已达上限等前置条件情况一律返回 0，不会把青龙任务标记为失败。

环境变量
    WORKBUDDY_TOKEN       Bearer Token，单账号
    WORKBUDDY_TOKENS      多账号，用换行分隔；支持 "uid:Token" 或纯 Token
    WB_USER_ID            兜底 X-User-Id，仅在无法从 token 解析时使用
    WB_TRAVEL_LOCATION    派出地点，id 或 code
    RANDOM_SIGNIN         延迟开关，默认 true；置 false 关闭全部等待（启动与账号间延迟）
    MAX_RANDOM_DELAY      启动随机延迟上限（秒），默认 600

用法
    WORKBUDDY_TOKEN=xxxx python3 workbuddy_buddytravel_tasks.py

输出示例
    🐱 WorkBuddy 宠物探险开始，共 2 个账号
       先统一派出，再按账号顺序依次等待归来并领取；倒计时每 60 秒刷新

    📤 第 1 步：统一派出
    [uid-A…] 🐾 已派出（咖啡馆 id=1/coffee），约 1~4 小时后归来，归来后自动领取（+5~10）
    [uid-B…] 🐾 已派出（咖啡馆 id=1/coffee），约 1~4 小时后归来，归来后自动领取（+5~10）

    ⏳ 第 2 步：依次等待领取

    [uid-A…] 🐾 探险中（咖啡馆）
    [uid-A…] 🕐 归来时间 2026-10-15 20:25:41
    [uid-A…] ⏰ 倒计时 02:00:00 开始，每 60 秒刷新
    [uid-A…] ⏰ 倒计时 01:59:00（第 1 次，预计 2026-10-15 20:25:41 归来）
    ...
    [uid-A…] 🔔 倒计时归零，正在领取奖励…
    [uid-A…] ✅ 🎉 已领取积分 +8
    [uid-B…] ✅ 🎉 已领取积分 +6

    🏁 完成：成功 2 / 失败 0 / 共 2
"""

import os
import sys
import json
import time
import random
import base64

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    HAVE_REQUESTS = False
    # 直连：urllib 默认读取系统代理环境变量，这里显式绕过。
    URL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------------- 通知模块：面板内置通知 ----------------
# 通知属于附属能力，其未配置、异常或超时状态均不得影响主流程。
#   环境缺失（QLAPI 未注入）  → 跳过
#   调用异常（未配置 / 参数非法）→ 忽略，返回 False
#   调用无响应（面板阻塞）      → 达到超时即返回，不阻塞主流程
import threading

NOTIFY_TIMEOUT = 10.0  # 单次通知最长等待时间（秒）


def notify(title, content, timeout=NOTIFY_TIMEOUT):
    """通过面板「系统设置 → 通知设置」发送通知。

    :param title:   通知标题
    :param content: 通知正文，为空时使用占位符
    :param timeout: 最长等待时间（秒），超时即放弃等待
    :return: True 表示提交成功；False 表示跳过、失败或超时，不抛出异常
    """
    # 环境校验：QLAPI 仅在面板任务执行器运行时注入
    if not hasattr(__builtins__, "QLAPI"):
        return False

    payload = {
        "title": str(title or "青龙通知"),
        "content": str(content or "（空）"),
    }
    result = {"ok": False}

    def _send():
        """实际发送逻辑；异常在此收敛，不向调用方传播。"""
        try:
            resp = QLAPI.systemNotify(payload)
            if isinstance(resp, dict) and "code" in resp:
                result["ok"] = resp.get("code") == 200
            else:
                result["ok"] = True
        except Exception:
            result["ok"] = False

    # 超时保护：以守护线程执行，超过 timeout 即返回，避免阻塞主流程
    t = threading.Thread(target=_send, name="ql-notify", daemon=True)
    t.start()
    t.join(timeout)
    return result["ok"]

# 随机延迟配置
max_random_delay = int(os.getenv("MAX_RANDOM_DELAY", "600"))
random_signin = os.getenv("RANDOM_SIGNIN", "true").lower() == "true"


API_BASE = "https://copilot.tencent.com"
STATUS_EP = "/activity/growth/buddy/travel/status"
CONFIG_EP = "/activity/growth/buddy/travel/config"
DEPART_EP = "/activity/growth/buddy/travel/depart"
CLAIM_EP = "/activity/growth/buddy/travel/claim"

LOCATION_ARG = os.environ.get("WB_TRAVEL_LOCATION", "").strip()

# 一律直连，显式禁用系统代理（容器内常见的 SOCKS 代理会因缺少依赖而报错）。
NO_PROXIES = {"http": None, "https": None}

# 网络参数：单次请求超时、重试次数、重试退避基数（秒）。固定值，不开放配置。
HTTP_TIMEOUT = 20
HTTP_RETRIES = 2
HTTP_RETRY_BACKOFF = 2.0

# 倒计时刷新间隔（秒）。
POLL_INTERVAL = 60

# 归来时刻之后的领取宽限期，用于吸收服务端状态切换的延迟。
CLAIM_GRACE_SECONDS = 600

# 日志时间的时区偏移（小时）。青龙容器通常为 UTC，固定按 UTC+8 展示。
TZ_OFFSET_HOURS = 8


# 当前处理的账号标签。非空时作为日志前缀，用于区分多账号输出。
ACCOUNT_TAG = ""


def log(msg):
    if not msg:
        print()
        return
    print(f"[{ACCOUNT_TAG}] {msg}" if ACCOUNT_TAG else msg)


def normalize_token(raw):
    """清洗 Token：去掉 Bearer 前缀、引号与空白。"""
    tok = (raw or "").strip().strip('"').strip("'").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    return tok


def decode_jwt_sub(token):
    """从 JWT 的 payload 中取出 sub，失败返回空串。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
        return payload.get("sub") or ""
    except Exception:
        return ""


def get_accounts():
    """读取账号列表，返回 [(uid, token), ...]，自动去重。

    WORKBUDDY_TOKENS 多账号用换行分隔（兼容逗号），每项可为：
        - 纯 Token
        - uid:Token         （uid 不限数字，用户名也可以）
    WORKBUDDY_TOKEN 为单账号的纯 Token。

    "uid:Token" 的判定不依赖 eyJ 前缀（并非所有 Token 都是标准 JWT），
    而是要求冒号左侧不含点、右侧含点或长度大于 40。
    """
    raw = os.environ.get("WORKBUDDY_TOKENS", "").strip()
    single = os.environ.get("WORKBUDDY_TOKEN", "").strip()

    tokens = []
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue
        uid, tok = "", item
        if item.count(":") == 1:
            left, right = item.split(":", 1)
            left, right = left.strip(), right.strip()
            if left and right and "." not in left and (
                    "." in right or len(right) > 40):
                uid, tok = left, right
        tokens.append((uid, normalize_token(tok)))
    if single:
        tokens.append(("", normalize_token(single)))

    if not tokens:
        log("⚠️ 未检测到 Token，请设置 WORKBUDDY_TOKEN 或 WORKBUDDY_TOKENS")
        return []

    seen, fixed = set(), []
    fallback_uid = os.environ.get("WB_USER_ID", "").strip()
    for uid, tok in tokens:
        if not tok or tok in seen:
            continue
        seen.add(tok)
        # uid 优先级：显式 "uid:Token" > JWT 的 sub > WB_USER_ID
        if not uid:
            uid = decode_jwt_sub(tok)
        if not uid:
            uid = fallback_uid
        fixed.append((uid, tok))
    return fixed


def http(method, url, token, uid, body="{}", retries=HTTP_RETRIES):
    """发起 HTTP 请求并返回 (status_code, text)，网络层彻底失败时 status_code 为 0。

    仅对网络异常与 5xx 重试；4xx 属确定性结果，直接返回。
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "X-User-Id": uid or "",
        "User-Agent": "Mozilla/5.0",
    }
    last_err = ""
    for attempt in range(retries + 1):
        try:
            if HAVE_REQUESTS:
                resp = requests.request(method, url, headers=headers, data=body,
                                        timeout=HTTP_TIMEOUT, proxies=NO_PROXIES)
                code, text = resp.status_code, resp.text
            else:
                req = urllib.request.Request(url, data=body.encode("utf-8"),
                                             headers=headers, method=method)
                try:
                    with URL_OPENER.open(req, timeout=HTTP_TIMEOUT) as r:
                        code, text = r.status, r.read().decode("utf-8", "ignore")
                except urllib.error.HTTPError as e:
                    code, text = e.code, e.read().decode("utf-8", "ignore")

            if code < 500:
                return code, text
            last_err = f"HTTP {code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < retries:
            time.sleep(HTTP_RETRY_BACKOFF * (attempt + 1))

    return 0, last_err


def token_exp_info(token):
    """解析 JWT 的 exp，返回 (是否已过期, exp 字符串)；解析失败返回 (None, "")。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
        exp = payload.get("exp")
        if not exp:
            return None, ""
        exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(exp)))
        return int(time.time()) > int(exp), exp_str
    except Exception:
        return None, ""


def auth_fail_msg(token):
    """构造 401 的提示，区分「已过期」与「未过期但被拒绝」两种成因。"""
    expired, exp_str = token_exp_info(token)
    if expired is True:
        return f"登录态已过期（401，exp={exp_str}），请重新登录后更新 Token"
    if expired is False:
        return (f"登录态被拒绝（401，Token 未过期 exp={exp_str}），"
                f"通常为已退出登录或 Token 被吊销，请重新登录后更新 Token")
    return "登录态失效（401），请重新登录后更新 Token"


def is_daily_limit_msg(msg):
    """判断是否为「今日已达上限」类文案，各地服务端措辞可能不同。"""
    m = (msg or "").lower()
    return any(k in m for k in ("daily limit", "limit reached", "上限", "已达上限"))


def is_no_claim_msg(msg):
    """判断是否为「无可领取奖励」类文案。"""
    m = (msg or "").lower()
    return any(k in m for k in ("no unclaimed", "unclaimed", "already claim",
                                "已领取", "无未领取", "无可领取"))


def is_no_buddy_msg(msg):
    """判断是否为「账号尚未激活宠物」类文案。

    账号未在成长空间激活宠物时，depart 返回 HTTP 400 {"msg":"no active buddy"}。
    该情况属账号侧前置条件未满足，不计为任务失败。
    """
    m = (msg or "").lower().replace("_", " ")
    return any(k in m for k in ("no active buddy", "no buddy", "buddy not found",
                                "no active pet", "尚未激活", "没有宠物"))


def fmt_time(ts):
    """将时间戳渲染为指定时区的「年-月-日 时:分:秒」字符串。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.gmtime(float(ts) + TZ_OFFSET_HOURS * 3600))
    except Exception:
        return str(ts)


def describe_locations(locations):
    """将可选地点渲染为一行清单。"""
    return " / ".join(f"{loc.get('id')}={loc.get('code')}({loc.get('name')})"
                      for loc in (locations or []))


def pick_location(config_data):
    """按 WB_TRAVEL_LOCATION 选择地点，未指定或未匹配则取第一个。

    支持填数字 id 或 code。服务端只接受数字 id，故返回整个地点对象，
    由调用方取 id 字段。
    """
    locations = (config_data or {}).get("locations") or []
    if not locations:
        return None
    if LOCATION_ARG:
        for loc in locations:
            if str(loc.get("id")) == LOCATION_ARG:
                return loc
            if loc.get("code") == LOCATION_ARG:
                return loc
        log(f"⚠️ 地点 '{LOCATION_ARG}' 不在可选列表：{describe_locations(locations)}")
        log(f"⚠️ 已回退到第一个地点（{locations[0].get('name')}）")
    return locations[0]


def fmt_duration(loc):
    """渲染地点时长文案。实际时长由服务端在区间内随机决定。"""
    lo, hi = loc.get("duration_hours_min"), loc.get("duration_hours_max")
    if lo and hi:
        return f"约 {lo} 小时后归来" if lo == hi else f"约 {lo}~{hi} 小时后归来"
    one = hi or lo
    return f"约 {one} 小时后归来" if one else "归来时间以服务端为准"


def fmt_reward(loc):
    """渲染地点积分区间文案。"""
    lo, hi = loc.get("reward_credit_min"), loc.get("reward_credit_max")
    if lo and hi:
        return f"（+{lo}）" if lo == hi else f"（+{lo}~{hi}）"
    one = hi or lo
    return f"（+{one}）" if one else ""


def fmt_countdown(seconds):
    """将秒数渲染为 HH:MM:SS 倒计时，负数按 0 处理。"""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def fmt_elapsed(seconds):
    """将秒数渲染为可读耗时，用于在通知里区分并行实例。"""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m = rem // 60
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分"
    return f"{total} 秒"


def fetch_status(uid, token):
    """查询宠物状态，返回 (kind, payload)。

    kind == "ok"    -> payload 为 data 字典
    kind == "fatal" -> payload 为失败消息，本轮应记为失败
    """
    code, text = http("GET", API_BASE + STATUS_EP, token, uid)
    if code == 401:
        return "fatal", auth_fail_msg(token)
    if code == 0:
        return "fatal", f"状态接口网络请求失败：{text}"
    try:
        st = json.loads(text)
    except Exception:
        return "fatal", f"状态接口返回异常（{code}）：{text[:120]}"
    if st.get("code") != 0:
        return "fatal", f"状态查询失败 code={st.get('code')} msg={st.get('msg', '')}"
    return "ok", (st.get("data") or {})


def do_claim(uid, token, status_data=None):
    """领取归来奖励，返回 (ok, msg)。

    「无可领取奖励」视为正常结果：多账号或多次运行下重复调用 claim 必然遇到。
    """
    ck_code, ck_text = http("POST", API_BASE + CLAIM_EP, token, uid, "{}")
    if ck_code == 401:
        return False, auth_fail_msg(token)
    if ck_code == 0:
        return False, f"领取接口网络请求失败：{ck_text}"
    try:
        ck = json.loads(ck_text)
    except Exception:
        return False, f"领取接口返回异常（{ck_code}）：{ck_text[:120]}"
    if ck.get("code") == 0:
        d = ck.get("data") or {}
        src = status_data or {}
        credit = (d.get("credit") or d.get("reward_credit")
                  or src.get("reward_credit") or 0)
        return True, f"🎉 已领取积分 +{credit}"
    if is_no_claim_msg(ck.get("msg")):
        return True, "宠物已归来，但无可领取奖励（可能已领取过）"
    return False, f"领取失败 code={ck.get('code')} msg={ck.get('msg', '')}"


def remaining_seconds(arrive, data):
    """计算距离归来还有多少秒，返回 -1 表示未知。

    优先用响应中的 server_now 相减，使倒计时不受本机时钟影响；
    响应未带 server_now 时退回本机时钟。
    """
    if not arrive:
        return -1
    srv = int((data or {}).get("server_now") or 0)
    if srv:
        return arrive - srv
    return arrive - int(time.time())


def wait_and_claim(uid, token, data):
    """等待宠物归来并领取奖励，期间每 POLL_INTERVAL 秒输出一次倒计时。

    适用于本轮开始时宠物已在探险中，或刚由本脚本派出。

    截止时间为「归来时刻 + 10 分钟」，超时不计为失败，奖励保留至下次运行领取。
    接口未返回归来时间时本轮不等待，直接退出。
    """
    d = data or {}
    loc = d.get("location") or {}
    loc_name = loc.get("name", "") or "未知地点"
    arrive = int(d.get("arrive_at") or 0)

    log("")
    if not arrive:
        log(f"🐾 探险中（{loc_name}），接口未返回归来时间")
        return True, (f"🐾 探险中（{loc_name}），接口未返回归来时间，本轮不等待；"
                      f"奖励保留，下次运行领取")

    started = time.time()
    left = remaining_seconds(arrive, d)
    # 截止时间跟随归来时刻：留出宽限期，确保倒计时归零后仍能领取。
    deadline = started + max(left, 0) + CLAIM_GRACE_SECONDS

    log(f"🐾 探险中（{loc_name}）")
    log(f"🕐 归来时间 {fmt_time(arrive)}")
    log(f"⏰ 倒计时 {fmt_countdown(left)} 开始，每 {POLL_INTERVAL} 秒刷新")

    tick = 0
    while True:
        budget = deadline - time.time()
        if budget <= 0:
            log("")
            return True, (f"⏰ 等待超时（已等 {fmt_countdown(time.time() - started)}），"
                          f"奖励保留，下次运行继续领取")
        sleep_for = min(POLL_INTERVAL, budget)
        # 不把整段间隔睡过归来时刻，保证归零后立即领取。
        left = remaining_seconds(arrive, d)
        if left > 0:
            sleep_for = min(sleep_for, max(1.0, left))
        time.sleep(sleep_for)
        tick += 1

        kind, payload = fetch_status(uid, token)
        if kind != "ok":
            log("")
            return False, payload
        data = payload
        d = data
        if data.get("arrive_at"):
            arrive = int(data["arrive_at"])
        state = data.get("state")
        left = remaining_seconds(arrive, data)

        if state == "arrived":
            log("🔔 倒计时归零，正在领取奖励…")
            return do_claim(uid, token, data)

        if state == "idle":
            # 探险结束且状态已回到空闲：奖励通常已被领取。
            log("")
            return True, "宠物探险已结束（状态空闲，奖励可能已领取）"

        if state == "traveling":
            if left > 0:
                # 服务端推迟归来时刻时，同步放宽截止时间。
                deadline = max(deadline, time.time() + left + CLAIM_GRACE_SECONDS)
                log(f"⏰ 倒计时 {fmt_countdown(left)}"
                    f"（第 {tick} 次，预计 {fmt_time(arrive)} 归来）")
            else:
                log(f"⏰ 倒计时归零，等待服务端状态切换"
                    f"（第 {tick} 次，已等待 {fmt_countdown(time.time() - started)}）")
            continue

        log("")
        return True, f"等待中收到未知状态 {state!r}，本轮结束"


def prepare_one(uid, token):
    """阶段一：判定账号当前状态，必要时派出，返回待领取任务。

    返回 dict，kind 取值：
        wait    阶段二等待归来并领取，data 为状态响应
        claim   已归来，阶段二直接领取，data 为状态响应
        done    无需动作，msg 为说明（属正常结果）
        fatal   失败，msg 为原因
    """
    kind, payload = fetch_status(uid, token)
    if kind != "ok":
        return {"kind": "fatal", "msg": payload}

    data = payload
    state = data.get("state")

    if state == "arrived":
        log("🔔 已归来，待领取")
        return {"kind": "claim", "data": data}

    if state == "traveling":
        loc_name = (data.get("location") or {}).get("name") or "未知地点"
        arrive = int(data.get("arrive_at") or 0)
        log(f"🐾 探险中（{loc_name}），预计 {fmt_time(arrive)} 归来" if arrive
            else f"🐾 探险中（{loc_name}），归来时间未知")
        return {"kind": "wait", "data": data}

    if state != "idle":
        return {"kind": "done", "msg": f"宠物状态未知：{state}"}
    if data.get("daily_limit_reached"):
        return {"kind": "done", "msg": "😴 今日派出已达上限，跳过"}

    cfg_code, cfg_text = http("GET", API_BASE + CONFIG_EP, token, uid)
    try:
        cfg = json.loads(cfg_text) if cfg_code == 200 else {}
    except Exception:
        cfg = {}
    loc = pick_location(cfg.get("data") if isinstance(cfg, dict) else {})
    if not loc:
        return {"kind": "fatal", "msg": "未获取到可选的探险地点配置"}

    # depart 的 location_id 必须为数字 id，传 code 字符串会被判为 invalid request。
    try:
        loc_id = int(loc.get("id"))
    except (TypeError, ValueError):
        return {"kind": "fatal",
                "msg": (f"地点 id 非数字（{loc.get('id')!r}），无法派出；"
                        f"请检查 WB_TRAVEL_LOCATION 是否填了 code")}

    dp_code, dp_text = http("POST", API_BASE + DEPART_EP, token, uid,
                            json.dumps({"location_id": loc_id}))
    if dp_code == 401:
        return {"kind": "fatal", "msg": auth_fail_msg(token)}
    if dp_code == 0:
        return {"kind": "fatal", "msg": f"派出接口网络请求失败：{dp_text}"}
    try:
        dp = json.loads(dp_text)
    except Exception:
        return {"kind": "fatal",
                "msg": f"派出接口返回异常（{dp_code}）：{dp_text[:120]}"}

    if dp.get("code") == 0:
        ok_msg = (f"🐾 已派出（{loc.get('name', '')} id={loc.get('id')}"
                  f"/{loc.get('code')}），{fmt_duration(loc)}，"
                  f"归来后自动领取{fmt_reward(loc)}")
        log(ok_msg)
        # 派出后复查状态，确认已进入探险中。
        kind2, payload2 = fetch_status(uid, token)
        if kind2 == "ok" and payload2.get("state") == "traveling":
            return {"kind": "wait", "data": payload2}
        if kind2 == "ok" and payload2.get("state") == "arrived":
            # 探险时长极短、立即归来的情况。
            return {"kind": "claim", "data": payload2}
        return {"kind": "done",
                "msg": ok_msg + "（未确认归来时间，本轮不等待，下次运行将自动领取）"}

    # 竞态：查状态时未达上限，派出时刚好达到。属正常结果。
    if is_daily_limit_msg(dp.get("msg")):
        return {"kind": "done", "msg": "😴 今日派出已达上限（派出时判定），跳过"}
    # 账号尚未激活宠物：属前置条件未满足，跳过并给出提示。
    if is_no_buddy_msg(dp.get("msg")):
        return {"kind": "done",
                "msg": ("🐣 该账号尚未激活宠物，无法派出；"
                        "请先在 WorkBuddy 成长空间领养后再运行")}
    return {"kind": "fatal",
            "msg": f"派出失败 code={dp.get('code')} msg={dp.get('msg', '')}"}


def finish_one(uid, token, job):
    """阶段二：执行阶段一派发的任务，返回 (是否正常, 消息)。"""
    kind = job.get("kind")
    if kind == "fatal":
        return False, job.get("msg", "")
    if kind == "done":
        return True, job.get("msg", "")
    if kind == "claim":
        return do_claim(uid, token, job.get("data") or {})

    # 待等待的账号：以最新状态为准，避免沿用阶段一的旧响应。
    st_kind, payload = fetch_status(uid, token)
    if st_kind != "ok":
        return False, payload
    state = payload.get("state")
    if state == "arrived":
        return do_claim(uid, token, payload)
    if state == "traveling":
        return wait_and_claim(uid, token, payload)
    if state == "idle":
        return True, "宠物探险已结束（状态空闲，奖励可能已领取）"
    return True, f"等待前收到未知状态：{state}"


def _wait_with_countdown(seconds, task_name):
    """带倒计时的延迟等待。"""
    if seconds <= 0:
        return
    log(f"{task_name} 需要等待 {int(seconds)} 秒")
    remaining = int(seconds)
    while remaining > 0:
        if remaining <= 10 or remaining % 60 == 0:
            log(f"{task_name} 倒计时: {remaining} 秒")
        sleep_time = 1 if remaining <= 10 else min(10, remaining)
        time.sleep(sleep_time)
        remaining -= sleep_time


def account_tag(idx, uid):
    """账号日志前缀。"""
    return (uid[:8] + "…") if uid else f"账号{idx}"


def main():
    global ACCOUNT_TAG
    started_at = time.time()
    accounts = get_accounts()
    if not accounts:
        # 未配置 Token 属配置缺失而非运行故障：正常结束，避免被青龙标记为失败。
        log("🏁 未检测到可用账号，任务结束")
        notify("WorkBuddy 宠物探险",
               "未检测到可用账号，请设置 WORKBUDDY_TOKEN 或 WORKBUDDY_TOKENS")
        return

    # 启动随机延迟
    if random_signin and max_random_delay > 0:
        delay_seconds = random.randint(0, max_random_delay)
        if delay_seconds > 0:
            log(f"🎲 随机模式: 延迟 {delay_seconds} 秒后开始")
            _wait_with_countdown(delay_seconds, "WorkBuddy 宠物探险")

    log(f"🐱 WorkBuddy 宠物探险开始，共 {len(accounts)} 个账号")
    log(f"   先统一派出，再按账号顺序依次等待归来并领取；倒计时每 {POLL_INTERVAL} 秒刷新")

    # 第 1 步：逐个派出。此步只做派出，不等待，故总耗时与账号数基本无关。
    log("")
    log("📤 第 1 步：统一派出")
    jobs = []
    for idx, (uid, token) in enumerate(accounts, 1):
        ACCOUNT_TAG = account_tag(idx, uid)
        if idx > 1 and random_signin:
            delay = random.uniform(10, 20)
            log(f"⏱️  随机等待 {delay:.1f} 秒后处理下一个账号")
            time.sleep(delay)
        try:
            job = prepare_one(uid, token)
        except Exception as e:
            job = {"kind": "fatal", "msg": f"异常：{e}"}
        jobs.append((uid, token, job))

    # 第 2 步：按账号顺序依次等待归来并领取，一个完成再处理下一个。
    ACCOUNT_TAG = ""
    log("")
    log("⏳ 第 2 步：依次等待领取")
    ok, fail = 0, 0
    for idx, (uid, token, job) in enumerate(jobs, 1):
        ACCOUNT_TAG = account_tag(idx, uid)
        try:
            success, msg = finish_one(uid, token, job)
        except Exception as e:
            success, msg = False, f"异常：{e}"
        if success:
            ok += 1
            log(f"✅ {msg}")
        else:
            fail += 1
            log(f"❌ {msg}")

    ACCOUNT_TAG = ""
    log("")
    log(f"🏁 完成：成功 {ok} / 失败 {fail} / 共 {len(accounts)}")
    notify(
        "WorkBuddy 宠物探险",
        f"📊 共 {len(accounts)} 个账号\n"
        f"✅ 成功: {ok}\n"
        f"❌ 失败: {fail}\n"
        f"⏱️ 耗时 {fmt_elapsed(time.time() - started_at)}"
        f"（{fmt_time(started_at)[11:16]} 启动）"
    )
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
