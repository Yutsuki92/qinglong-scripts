#!/usr/bin/env python3
"""cron: 10 0 * * *
new Env('WorkBuddy宠物探险');

先统一派出，再按账号顺序等待归来并领取，单次可能持续 1~4 小时；地点取自服务端配置。

环境变量：WORKBUDDY_TOKEN（uid:Token 或纯 Token，多账号每行一个）、WB_USER_ID（兜底头）、
RANDOM_SIGNIN（默认 true）。退出码：0 成功/跳过/已达上限；1 接口或鉴权异常；2 配置缺失。
"""

import sys
import json
import math
import random
import re
import time
import urllib.request

try:
    from ql_common import (  # noqa: E402
    account_delay, account_header, account_label, banner,
    bearer_accounts, env_str, fmt_clock, fmt_elapsed, get_logger, http_text,
    jwt_exp, jwt_left, notify_config_error, notify_result, start_delay,
)
except ImportError:  # 公共层没上传/没被订阅同步：给结论，不抛 Traceback
    print("❌ 缺少公共层 ql_common.py：它必须与本脚本放在同一目录")
    print("🔧 青龙「订阅」会同步整个仓库；手动上传时请把它一起传上去")
    sys.exit(2)

PLATFORM = "WorkBuddy宠物探险"
log = get_logger("workbuddy_buddytravel_tasks")


API_BASE = "https://copilot.tencent.com"
STATUS_EP = "/activity/growth/buddy/travel/status"
CONFIG_EP = "/activity/growth/buddy/travel/config"
DEPART_EP = "/activity/growth/buddy/travel/depart"
CLAIM_EP = "/activity/growth/buddy/travel/claim"
RECORDS_EP = "/activity/growth/buddy/travel/records"

# 禁用系统代理：容器内 SOCKS 代理常因缺依赖报错。requests 走 proxies，urllib 走自建 opener。
NO_PROXIES = {"http": None, "https": None}
URL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 请求超时、重试次数、退避基数（秒）。固定值，不开放配置。
HTTP_TIMEOUT = 20
HTTP_RETRIES = 2
HTTP_RETRY_BACKOFF = 2.0

POLL_INTERVAL = 180

# 领取宽限期，吸收服务端状态切换的延迟。需大于 2~3 个 POLL_INTERVAL。
CLAIM_GRACE_SECONDS = 600
MAX_TOTAL_WAIT = 6 * 3600  # 单账号最长等待，防止服务端一直顺延 arrive_at
# 整轮总预算：正常总耗时≈最长旅程；服务端持续顺延时每账号会各等满 MAX_TOTAL_WAIT
# （8 账号=48 小时，与次日 cron 重叠），故再夹一层全局截止，到点放弃、奖励仍保留。
GLOBAL_WAIT_BUDGET = 8 * 3600
_GLOBAL_DEADLINE = None  # main 里按 GLOBAL_WAIT_BUDGET 设置（单调时钟）
# 连续状态查询失败容忍次数：一次抖动就整轮放弃等于当天白等（401 不重试，立即返回）。
MAX_STATUS_ERRORS = 3

# 记录接口的字段名不保证固定，按常见叫法逐个尝试（顺序即优先级）。
CREDIT_KEYS = ("reward_credit", "credit", "credits", "points", "reward", "amount")
CLAIMED_TIME_KEYS = ("claimed_at", "claim_at", "claim_time", "claimed_time",
                     "created_at", "updated_at")
RECORD_ID_KEYS = ("id", "record_id", "travel_id")

# 本轮实际入账积分，供通知汇总。
EARNED_CREDITS = []


def get_accounts():
    """读取账号列表 [(uid, token)]，按 token 去重（解析细节见公共层）。"""
    return bearer_accounts("WORKBUDDY_TOKEN", "", "WB_USER_ID")


def http(method, url, token, uid, body="{}", retries=HTTP_RETRIES):
    """发起请求并返回 (status_code, text)，网络层彻底失败时 status_code 为 0。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "X-User-Id": uid or "",
        "User-Agent": "Mozilla/5.0",
    }
    return http_text(method, url, headers=headers, body=body,
                     timeout=HTTP_TIMEOUT, retries=retries,
                     backoff=HTTP_RETRY_BACKOFF,
                     opener=URL_OPENER, proxies=NO_PROXIES)


def token_exp_info(token):
    """JWT 到期信息，返回 (是否已过期, exp 字符串)；取不到返回 (None, "")。"""
    exp = jwt_exp(token)
    if not exp:
        return None, ""
    # 与日志其它时间统一按 TZ_OFFSET_HOURS 展示，避免容器 UTC 时区错位 8 小时
    return int(time.time()) > exp, fmt_time(exp)


def auth_fail_msg(token):
    """构造 401 提示，区分「已过期」与「未过期但被拒绝」。"""
    expired, exp_str = token_exp_info(token)
    if expired is True:
        return f"登录态已过期（401，exp={exp_str}），请重新登录后更新 Token"
    if expired is False:
        return (f"登录态被拒绝（401，Token 未过期 exp={exp_str}），"
                f"通常为已退出登录或 Token 被吊销，请重新登录后更新 Token")
    return "登录态失效（401），请重新登录后更新 Token"


def is_daily_limit_msg(msg):
    """判断是否为「今日已达上限」类文案，各地服务端措辞可能不同。

    限流（rate limit / too many requests）不算每日上限：一旦误判成本轮「已达上限」，
    通知会显示成正常的跳过，实际当天根本没派出去，等于静默少跑一天。
    """
    m = (msg or "").lower()
    if any(k in m for k in ("rate limit", "too many", "限流", "频率", "过于频繁")):
        return False
    return any(k in m for k in ("daily limit", "limit reached", "上限", "已达上限"))


def is_no_claim_msg(msg):
    """判断是否为「无可领取奖励」类文案。"""
    m = (msg or "").lower()
    return any(k in m for k in ("no unclaimed", "unclaimed", "already claim",
                                "已领取", "无未领取", "无可领取"))


def is_no_buddy_msg(msg):
    """判断是否为「账号尚未激活宠物」，属前置条件未满足，不计失败。"""
    m = (msg or "").lower().replace("_", " ")
    return any(k in m for k in ("no active buddy", "no buddy", "buddy not found",
                                "no active pet", "尚未激活", "没有宠物"))


def fmt_time(ts):
    """时间戳渲染为「年-月-日 时:分:秒」，非法或 0 返回 "-"。

    统一走 fmt_clock，保证日志行首、完成时间、通知三处同一时区；毫秒时间戳先归一。
    """
    secs = _norm_ts(ts)
    if not secs:
        return "-"
    try:
        return fmt_clock(secs)
    except (OSError, OverflowError, ValueError):
        return "-"


def describe_locations(locations):
    """将可选地点渲染为一行清单。"""
    return " / ".join(f"{loc.get('id')}={loc.get('code')}({loc.get('name')})"
                      for loc in _dict_list(locations))


def pick_location(config_data):
    """从服务端下发的地点列表中随机选一个，返回整个地点对象（调用方取 id）。"""
    locations = _dict_list((config_data or {}).get("locations"))
    if not locations:
        return None

    chosen = random.choice(locations)
    log(f"🎯 派出地点：{chosen.get('name')}（id={chosen.get('id')} / "
        f"{chosen.get('code')}，从服务端下发列表中随机选择）")
    log(f"📍 服务端下发地点：{describe_locations(locations)}")
    return chosen


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


def token_validity_line(token):
    """Token 有效期行：剩余时长 + 到期时刻。"""
    exp = jwt_exp(token)
    if not exp:
        return "🔑 Token 有效期：无法解析（Token 非 JWT，无 exp 字段）"

    left = jwt_left(token)
    exp_str = fmt_time(exp)
    if left <= 0:
        return f"🔑 Token 有效期：已过期（至 {exp_str}）⚠️ 请重新登录更新 WORKBUDDY_TOKEN"
    return f"🔑 Token 有效期：剩余 {left / 86400:.1f} 天（至 {exp_str}）"


def fetch_status(uid, token):
    """查询宠物状态，返回 (kind, payload)：ok 为 data，auth 为 401，fatal 为错误消息。"""
    code, text = http("GET", API_BASE + STATUS_EP, token, uid)
    if code == 401:
        return "auth", auth_fail_msg(token)
    if code == 0:
        return "fatal", f"状态接口网络请求失败：{text}"
    try:
        st = json.loads(text)
    except Exception:
        return "fatal", f"状态接口返回异常（{code}）：{_brief(text)}"
    if not isinstance(st, dict):
        return "fatal", f"状态接口响应非对象（{code}）：{_brief(text)}"
    if _biz_code(st) != 0:
        return "fatal", f"状态查询失败 code={st.get('code')} msg={st.get('msg', '')}"
    data = st.get("data")
    if not isinstance(data, dict):
        return "fatal", f"状态接口 data 结构异常：{_brief(data)}"
    _log_status(data)
    return "ok", data


def _biz_code(body):
    """业务返回码归一为 int，兼容 "0" 这类字符串；响应不是对象时返回 None。"""
    if not isinstance(body, dict):
        return None
    return _to_int(body.get("code"))


def _truthy(val):
    """把布尔字段解释成真值，兼容字符串 "false"/"0"（非空字符串直接 bool() 会得 True）。

    这条很关键：daily_limit_reached 若被网关序列化成字符串 "false"，
    裸 if 判定会当真 → 直接判「今日已达上限」跳过，等于静默少跑一天。
    """
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(val)


def _state_of(data):
    """取出归一后的状态值（小写去空白）；缺失或非字符串时返回空串。"""
    raw = (data or {}).get("state")
    return raw.strip().lower() if isinstance(raw, str) else ""


def _dict_list(val):
    """过滤出字典元素，非列表或混入非对象时返回空列表/干净列表。"""
    if not isinstance(val, list):
        return []
    return [x for x in val if isinstance(x, dict)]


def _to_int(val):
    """把数字或纯数字字符串转成 int，取不到返回 None。"""
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        # json 默认会解析 NaN / Infinity，直接 int() 会抛异常，把一次取数变成整轮失败。
        return int(val) if math.isfinite(val) else None
    if isinstance(val, str) and val.strip().lstrip("-").isdigit():
        return int(val.strip())
    return None


def _norm_ts(val):
    """时间戳统一到秒：兼容毫秒/微秒、数字字符串，以及 'Y-m-d H:M:S' 文本。"""
    ts = _to_int(val)
    if ts:
        while abs(ts) > 10 ** 11:  # 10^11 秒已远超合理范围，只可能是毫秒/微秒
            ts //= 1000
        return ts
    if isinstance(val, str) and val.strip():
        text = val.strip()
        try:  # "1760000000.9" 这类带小数的秒级时间戳（服务端按 float 序列化时会出现）
            return int(float(text))
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:  # 末尾的 Z 不参与格式匹配
                return int(time.mktime(time.strptime(text.rstrip("Z"), fmt)))
            except ValueError:
                continue
    return 0


def _compact(text, limit=700):
    """把 JSON 压成一行，超长截断；解析失败按原文截断。"""
    try:
        text = json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(text or "")
    return text[:limit]


def _brief(text, limit=120):
    """错误响应摘录：剥掉 HTML 标签并压成单行，避免把 <html><head> 塞进通知。

    网关异常（401/502 等）返回的是 openresty 的 HTML 错误页而不是 JSON，
    原样截断会让通知里出现一整串标签，看不出到底什么错。
    """
    raw = "" if text is None else str(text)
    s = re.sub(r"<[^>]{0,200}>", " ", raw)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] or "（空响应）"


def _log_resp(tag, text):
    """打印接口原始响应，始终开启，便于核对积分字段是否变化。"""
    log(f"🔍 {tag} 响应：{_compact(text)}")


def _log_records(tag, records):
    """打印记录条目的关键字段，避免照片/信件正文淹没日志。"""
    if not records:
        log(f"🔍 {tag}：空列表")
        return
    items = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        claimed = _record_claimed_at(rec)
        items.append(f"{_record_identity(rec)}"
                     f"|reward={_credit_of(rec)}"
                     f"|claimed={fmt_time(claimed) if claimed else '-'}"
                     f"|date={rec.get('travel_date') or '-'}")
    log(f"🔍 {tag} 共 {len(records)} 条：{' ／ '.join(items) or '无可解析字段'}")


def _log_claim(body, text):
    """打印 claim 响应关键字段；code 非 0 或结构异常时打全文，便于排错。"""
    if not isinstance(body, dict) or _biz_code(body) != 0:
        _log_resp("claim(异常)", text)
        return
    d = body.get("data") if isinstance(body.get("data"), dict) else {}
    log(f"🔍 claim：code=0 | reward_credit={d.get('reward_credit')}"
        f" | state={d.get('state')} | record_id={d.get('record_id')}")


def _log_status(data):
    """打印状态接口关键字段，含服务端下发的 reward_credit。

    探险中不再每轮刷屏（倒计时日志里已带本趟奖励），只在状态切换时打印。
    """
    d = data or {}
    if _state_of(d) == "traveling":
        return
    loc = d.get("location") or {}
    arrive = _norm_ts(d.get("arrive_at"))
    log(f"🔍 status：state={d.get('state')} | reward_credit={d.get('reward_credit')}"
        f" | record_id={d.get('record_id')} | daily_limit={d.get('daily_limit_reached')}"
        f" | location={loc.get('name') or '-'}"
        f" | arrive_at={fmt_time(arrive) if arrive else '-'}")


def reward_tag(data):
    """状态里带 reward_credit 时返回「，本趟奖励 +N」，否则空串。"""
    val = _to_int((data or {}).get("reward_credit"))
    return f"，本趟奖励 +{val}" if val and val > 0 else ""


def _credit_of(obj, depth=0):
    """从嵌套字典/列表里找积分数量，找到返回正整数，否则 None。"""
    if depth > 3:
        return None
    if isinstance(obj, list):
        for item in obj:
            got = _credit_of(item, depth + 1)
            if got:
                return got
        return None
    if not isinstance(obj, dict):
        return None
    for key in CREDIT_KEYS:
        val = obj.get(key)
        if isinstance(val, bool):
            continue
        num = _to_int(val)
        if num and num > 0:
            return num
    for val in obj.values():
        got = _credit_of(val, depth + 1)
        if got:
            return got
    return None


def _record_identity(rec):
    """记录的唯一标识：优先 id，否则用日期+时间+积分拼接，用于领取前后比对。"""
    for key in RECORD_ID_KEYS:
        val = rec.get(key)
        if val not in (None, ""):
            return f"{key}:{val}"
    parts = [str(rec.get(k) or "") for k in
             ("travel_date", "claimed_at", "claim_time", "reward_credit", "credit")]
    return "|".join(parts)


def _valid_record_id(rid):
    """record_id 是否可用于精确定位。

    服务端在 idle 状态下发 record_id=0（实测），0 不是有效记录号，
    必须当成「没有 id」处理，否则精确匹配永远失败、退回时间窗口猜。
    """
    if rid is None or rid == "":
        return False
    num = _to_int(rid)
    return not (num is not None and num <= 0)


def _record_matches(rec, rid):
    """记录的 id 是否等于 rid（兼容 id / record_id / travel_id 三种叫法）。"""
    if not _valid_record_id(rid):
        return False
    for key in RECORD_ID_KEYS:
        val = rec.get(key)
        if val not in (None, "") and str(val) == str(rid):
            return True
    return False


def _record_claimed_at(rec):
    """记录里的时间戳（已归一到秒），取不到返回 0。"""
    for key in CLAIMED_TIME_KEYS:
        ts = _norm_ts(rec.get(key))
        if ts:
            return ts
    return 0


def fetch_records(uid, token, page_size=5, tag="records"):
    """拉取探险记录并打印关键字段，返回记录列表；失败返回 []。"""
    url = f"{API_BASE}{RECORDS_EP}?page=1&page_size={page_size}"
    code, text = http("GET", url, token, uid)
    if code != 200:
        _log_resp(f"{tag}(HTTP {code})", text)
        return []
    try:
        body = json.loads(text)
    except Exception:
        _log_resp(f"{tag}(解析失败)", text)
        return []
    if not isinstance(body, dict):
        _log_resp(f"{tag}(结构异常)", text)
        return []
    if _biz_code(body) != 0:
        _log_resp(f"{tag}(code={body.get('code')})", text)
        return []
    data = body.get("data")
    records = _dict_list(data.get("records")) if isinstance(data, dict) else []
    _log_records(tag, records)
    return records


def latest_reward(uid, token, record_id=None, retries=3, wait=2.0):
    """按 record_id 精确查本趟记录，返回 (积分, 日期)；查不到返回 (None, "")。

    claim 响应一般直接带 reward_credit，本函数只兜底。不退回「时间窗口取最新」——
    实测 claimed_at 会漂，时间窗口会把上一趟的积分报成本趟的。宁可少报，不能报错数字。
    """
    if not _valid_record_id(record_id):
        return None, ""
    for attempt in range(max(1, retries)):
        records = fetch_records(uid, token, tag=f"records(第 {attempt + 1} 次)")
        for rec in records:
            if not isinstance(rec, dict) or not _record_matches(rec, record_id):
                continue
            # 优先精确字段，取不到才递归（记录里还嵌着 location / letter）
            credit = _to_int(rec.get("reward_credit")) or _credit_of(rec)
            if credit and credit > 0:
                return credit, str(rec.get("travel_date") or "")
        if attempt + 1 < retries:
            time.sleep(wait)
    return None, ""


def do_claim(uid, token, status_data=None):
    """领取奖励，返回 (status, msg)：ok 领到积分 / skip 无可领 / fail 接口失败。

    积分来源优先级：claim 响应的 reward_credit → 归来状态里的 reward_credit
    → 记录接口兜底（只在服务端没回传时才会用到，不额外发请求解析其他任务）。
    """
    expect = _to_int((status_data if isinstance(status_data, dict) else {})
                     .get("reward_credit"))
    if expect is not None and expect <= 0:
        expect = None  # 服务端可能用 0 / -1 表示「未知」，不能当成本趟奖励

    ck_code, ck_text = http("POST", API_BASE + CLAIM_EP, token, uid, "{}")
    if ck_code == 401:
        return "fail", auth_fail_msg(token)
    if ck_code == 0:
        return "fail", f"领取接口网络请求失败：{ck_text}"
    try:
        ck = json.loads(ck_text)
    except Exception:
        _log_resp(f"claim(解析失败 HTTP {ck_code})", ck_text)
        return "fail", f"领取接口返回异常（{ck_code}）：{_brief(ck_text)}"
    if not isinstance(ck, dict):
        _log_resp(f"claim(结构异常 HTTP {ck_code})", ck_text)
        return "fail", f"领取接口响应非对象（{ck_code}）：{_brief(ck_text)}"
    _log_claim(ck, ck_text)
    if _biz_code(ck) == 0:
        claim_data = ck.get("data") if isinstance(ck.get("data"), dict) else {}
        # 只认 claim 顶层 reward_credit，再退状态值，最后查记录；不递归，避免抓到累计积分。
        got = _to_int(claim_data.get("reward_credit"))
        credit = got if got and got > 0 else expect
        if credit is None:
            rid = (status_data if isinstance(status_data, dict) else {}).get("record_id")
            credit, _day = latest_reward(uid, token, record_id=rid)
            if credit is None:
                return "ok", "🎉 已领取奖励（服务端未回传积分数）"
            EARNED_CREDITS.append(credit)
            # 记录里的 travel_date 实测并不等于行程日期（曾出现 10-15 的旧值），不展示
            return "ok", f"🎉 已领取积分 +{credit}（据探险记录核对）"
        EARNED_CREDITS.append(credit)
        return "ok", f"🎉 已领取积分 +{credit}"
    if is_no_claim_msg(ck.get("msg")):
        return "skip", "宠物已归来，但无可领取奖励（可能已领取过）"
    return "fail", f"领取失败 code={ck.get('code')} msg={ck.get('msg', '')}"


def remaining_seconds(arrive, data):
    """距离归来还有多少秒，-1 表示未知（仅供一次性估算，等待循环不用它）。

    arrive_at / server_now 可能是毫秒时间戳，统一归一后再算，防止倒计时算成天文数字。
    优先用服务端时间相减，避免依赖本机时钟是否准确。
    """
    arrive = _norm_ts(arrive)
    if not arrive:
        return -1
    srv = _norm_ts((data or {}).get("server_now"))
    if srv:
        return arrive - srv
    return arrive - int(time.time())


def wait_and_claim(uid, token, data):
    """等待归来并领取，每 POLL_INTERVAL 秒刷新一次，返回 (status, msg)。

    截止为「归来 + 宽限期」，超时标 skip（奖励保留至下次）；无归来时间则不等待。
    计时只认首帧的服务端时间，之后用 monotonic 推进：server_now 不前进时不会空转，
    本机被 NTP 校时也不会白等；服务端改期时按 arrive_at 差值平移剩余时长。
    """
    d = data or {}
    loc = d.get("location") if isinstance(d.get("location"), dict) else {}
    loc_name = loc.get("name", "") or "未知地点"
    arrive = _norm_ts(d.get("arrive_at"))

    log("")
    if not arrive:
        log(f"🐾 探险中（{loc_name}），接口未返回归来时间")
        return "skip", (f"🐾 探险中（{loc_name}），接口未返回归来时间，本轮不等待；"
                        f"奖励保留，下次运行领取")

    t0 = time.monotonic()
    left_base = remaining_seconds(arrive, d)  # 首帧基准（t0 时刻的剩余秒数）
    # 截止时长跟随归来时刻：留出宽限期，确保倒计时归零后仍能领取。
    limit = min(max(left_base, 0) + CLAIM_GRACE_SECONDS, MAX_TOTAL_WAIT)
    # 再夹一层整轮总预算，避免多账号串行等待把总时长推过下一次 cron。
    if _GLOBAL_DEADLINE is not None:
        remain = _GLOBAL_DEADLINE - t0
        if remain <= 0:
            log("")
            return "skip", (f"⏰ 本轮总时长已达上限（{fmt_countdown(GLOBAL_WAIT_BUDGET)}），"
                            f"本账号不再等待；奖励保留，下次运行继续领取")
        limit = min(limit, remain)

    log(f"🐾 探险中（{loc_name}）{reward_tag(d)}")
    log(f"🕐 归来时间 {fmt_time(arrive)}")
    log(f"⏰ 倒计时 {fmt_countdown(left_base)} 开始，每 {POLL_INTERVAL} 秒刷新")

    tick = 0
    errs = 0
    while True:
        elapsed = time.monotonic() - t0
        budget = limit - elapsed
        if budget <= 0:
            log("")
            extra = f"；期间有 {errs} 次查询失败" if errs else ""
            return "skip", (f"⏰ 等待超时（已等 {fmt_countdown(elapsed)}）{extra}，"
                            f"奖励保留，下次运行继续领取")
        sleep_for = min(POLL_INTERVAL, budget)
        # 不把整段间隔睡过归来时刻，保证归零后立即领取。
        left_now = left_base - elapsed
        if left_now > 0:
            sleep_for = min(sleep_for, max(1.0, left_now))
        time.sleep(sleep_for)
        tick += 1

        kind, payload = fetch_status(uid, token)
        if kind != "ok":
            if kind == "auth":
                # Token 失效，继续等也不会变好
                log("")
                return "fail", payload
            errs += 1
            if errs >= MAX_STATUS_ERRORS:
                log("")
                return "fail", (f"{payload}（已连续 {errs} 次查询失败，"
                                f"本轮放弃等待；奖励保留，下次运行继续领取）")
            log(f"⚠️ 状态查询失败（第 {errs}/{MAX_STATUS_ERRORS} 次）：{payload}，"
                f"继续等待下次轮询")
            continue
        errs = 0
        d = payload
        # 服务端改口：按新旧 arrive_at 的差值平移基准（纯服务端时间轴，不碰本机时钟）
        new_arrive = _norm_ts(d.get("arrive_at"))
        if new_arrive and new_arrive != arrive:
            left_base += new_arrive - arrive
            arrive = new_arrive
            limit = min(max(left_base, 0) + CLAIM_GRACE_SECONDS, MAX_TOTAL_WAIT)
            if _GLOBAL_DEADLINE is not None:  # 改期也不能突破整轮总预算
                limit = min(limit, max(0.0, _GLOBAL_DEADLINE - t0))
        state = _state_of(d)
        left_now = left_base - (time.monotonic() - t0)

        if state == "arrived":
            log("🔔 倒计时归零，正在领取奖励…")
            return do_claim(uid, token, d)

        if state == "idle":
            # 探险结束且状态已回到空闲：奖励通常已被领取。
            log("")
            return "skip", "宠物探险已结束（状态空闲，奖励可能已领取）"

        if state == "traveling":
            if left_now > 0:
                log(f"⏰ 倒计时 {fmt_countdown(left_now)}"
                    f"（第 {tick} 次，预计 {fmt_time(arrive)} 归来）")
            else:
                log(f"⏰ 倒计时归零，等待服务端状态切换"
                    f"（第 {tick} 次，已等待 {fmt_countdown(time.monotonic() - t0)}）")
            continue

        log("")
        # 未知状态可能是接口结构变了，判失败而非跳过
        return "fail", f"等待中收到未知状态 {state!r}，本轮结束"


def prepare_one(uid, token):
    """阶段一：按 status 判定，空闲则派出。返回 {kind: wait/claim/done/fatal, ...}。"""
    kind, payload = fetch_status(uid, token)
    if kind != "ok":
        # 未激活宠物时状态接口也可能直接报错，属前置条件不满足，不算失败
        if is_no_buddy_msg(payload):
            return {"kind": "done",
                    "msg": ("🐣 该账号尚未激活宠物，跳过；"
                            "请先在 WorkBuddy 成长空间领养后再运行")}
        return {"kind": "fatal", "msg": payload}

    data = payload
    state = _state_of(data)

    if state == "arrived":
        log("🔔 已归来，待领取")
        return {"kind": "claim", "data": data}

    if state == "traveling":
        loc_name = (data.get("location") or {}).get("name") or "未知地点"
        arrive = _norm_ts(data.get("arrive_at"))
        log((f"🐾 探险中（{loc_name}），预计 {fmt_time(arrive)} 归来" if arrive
             else f"🐾 探险中（{loc_name}），归来时间未知") + reward_tag(data))
        return {"kind": "wait", "data": data}

    if state != "idle":
        # 接口没给出已知状态（含 state 缺失），属结构异常而非正常跳过
        return {"kind": "fatal", "msg": f"宠物状态未知：{state!r}"}
    if _truthy(data.get("daily_limit_reached")):
        return {"kind": "done", "msg": "😴 今日派出已达上限，跳过"}

    cfg_code, cfg_text = http("GET", API_BASE + CONFIG_EP, token, uid)
    if cfg_code == 401:
        return {"kind": "fatal", "msg": auth_fail_msg(token)}
    if cfg_code != 200:
        return {"kind": "fatal", "msg": f"地点配置接口请求失败（{cfg_code}）：{_brief(cfg_text)}"}
    try:
        cfg = json.loads(cfg_text)
    except Exception:
        return {"kind": "fatal", "msg": f"地点配置接口返回异常：{_brief(cfg_text)}"}
    # 业务错误要先报出来，否则会被后面的「未下发地点」掩盖掉真正的 code
    if not isinstance(cfg, dict):
        return {"kind": "fatal", "msg": f"地点配置接口响应非对象：{_brief(cfg_text)}"}
    if _biz_code(cfg) != 0:
        return {"kind": "fatal",
                "msg": f"地点配置失败 code={cfg.get('code')} msg={cfg.get('msg', '')}"}
    loc = pick_location(cfg.get("data") if isinstance(cfg.get("data"), dict) else {})
    if not loc:
        return {"kind": "fatal", "msg": "服务端未下发可选的探险地点"}

    # depart 的 location_id 必须为数字 id，传 code 字符串会被判为 invalid request。
    try:
        loc_id = int(loc.get("id"))
    except (TypeError, ValueError):
        return {"kind": "fatal",
                "msg": f"服务端下发的地点 id 非数字（{loc.get('id')!r}），无法派出"}

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
                "msg": f"派出接口返回异常（{dp_code}）：{_brief(dp_text)}"}

    if not isinstance(dp, dict):
        return {"kind": "fatal",
                "msg": f"派出接口响应非对象（{dp_code}）：{_brief(dp_text)}"}

    if _biz_code(dp) == 0:
        ok_msg = (f"🐾 已派出（{loc.get('name', '')} id={loc.get('id')}"
                  f"/{loc.get('code')}），{fmt_duration(loc)}，"
                  f"归来后自动领取{fmt_reward(loc)}")
        log(ok_msg)
        # 派出后复查状态，确认已进入探险中。
        kind2, payload2 = fetch_status(uid, token)
        if kind2 == "ok" and _state_of(payload2) == "traveling":
            return {"kind": "wait", "data": payload2}
        if kind2 == "ok" and _state_of(payload2) == "arrived":
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
    """阶段二：执行阶段一的任务，返回 (status, 消息)；只有真正领到积分才算 ok。"""
    kind = job.get("kind")
    if kind == "fatal":
        return "fail", job.get("msg", "")
    if kind == "done":
        return "skip", job.get("msg", "")
    if kind == "claim":
        return do_claim(uid, token, job.get("data"))

    # 待等待的账号：以最新状态为准，避免沿用阶段一的旧响应。
    st_kind, payload = fetch_status(uid, token)
    if st_kind != "ok":
        return "fail", payload
    state = _state_of(payload)
    if state == "arrived":
        return do_claim(uid, token, payload)
    if state == "traveling":
        return wait_and_claim(uid, token, payload)
    if state == "idle":
        return "skip", "宠物探险已结束（状态空闲，奖励可能已领取）"
    # 未知状态（含 state 缺失）说明接口结构异常，判失败而不是跳过
    return "fail", f"等待前收到未知状态：{state!r}"


def main():
    """入口：依赖 → 配置 → 延迟 → 逐账号执行 → 汇总通知。

    与签到脚本的差别只在第 4 段：先统一派出，再依次等归来领取（账号间延迟只加在派出阶段）。
    """
    accounts = get_accounts()
    if not accounts:
        configured = bool(env_str("WORKBUDDY_TOKEN"))
        if configured:
            return notify_config_error(
                PLATFORM, "Token 内容为空",
                "变量已配置，但清洗后没有有效 Token",
                hints=[
                    "📝 可能只有空白行，或缺少 uid:Token 的冒号",
                    "🔑 格式：uid:Token（uid 可省略），多账号每行一个",
                ])
        return notify_config_error(
            PLATFORM, "缺少 Token",
            "未检测到可用账号，请设置 WORKBUDDY_TOKEN",
            hints=[
                "🔑 格式：uid:Token（uid 可省略），多账号每行一个",
                "💡 与签到脚本共用变量，配置一次即可",
            ])

    started_at = time.time()
    EARNED_CREDITS.clear()  # 幂等：同进程重复调用 main 时不残留上一轮积分
    global _GLOBAL_DEADLINE
    _GLOBAL_DEADLINE = None  # 幂等：上一轮若因异常提前返回，不能把旧截止带过来
    banner(PLATFORM, len(accounts))
    start_delay(PLATFORM)

    log("")
    log("📤 第 1 步：统一派出")
    jobs = []
    total = len(accounts)
    for idx, (uid, token) in enumerate(accounts, 1):
        account_header(idx, account_label(uid, idx), total)
        try:
            job = prepare_one(uid, token)
        except Exception as e:
            job = {"kind": "fatal", "msg": f"异常：{e}"}
        jobs.append((uid, token, job))
        account_delay(idx, total, PLATFORM)

    log("")
    log("⏳ 第 2 步：依次等待领取")
    # 整轮等待预算从这一步算起：账号统一派出，正常总耗时≈最长旅程。
    _GLOBAL_DEADLINE = time.monotonic() + GLOBAL_WAIT_BUDGET
    ok, skip, fail = 0, 0, 0
    skip_msgs = []
    icon = {"ok": "✅", "skip": "⏭️", "fail": "❌"}
    details = []
    for idx, (uid, token, job) in enumerate(jobs, 1):
        # 第 2 步可能跨数小时，重新打账号分隔行，避免日志看不出在处理谁
        tag = account_label(uid, idx)
        account_header(idx, tag, total)
        t0 = time.time()
        try:
            status, msg = finish_one(uid, token, job)
        except Exception as e:
            status, msg = "fail", f"异常：{e}"
        if status not in icon:
            status = "fail"
        elapsed = time.time() - t0
        if status == "ok":
            ok += 1
        elif status == "skip":
            skip += 1
            skip_msgs.append(msg)
        else:
            fail += 1
        log(f"{icon[status]} {msg}")
        log(f"⏱️  本账号耗时 {fmt_elapsed(elapsed)}")
        details.append(
            f"{icon[status]} [{tag}] {msg}\n"
            f"    🕐 完成时间：{fmt_time(time.time())}（本账号耗时 {fmt_elapsed(elapsed)}）\n"
            f"    {token_validity_line(token)}"
        )

    total_elapsed = time.time() - started_at
    extra = []
    if EARNED_CREDITS:
        extra.append(f"🎁 本次共领积分 +{sum(EARNED_CREDITS)}"
                     f"（{len(EARNED_CREDITS)} 个账号有入账）")
    # 全部账号「今日已达上限」是正常状态，不该再叠一条「没领到奖励」的告警
    if ok == 0 and skip > 0 and not all("上限" in msg for msg in skip_msgs):
        extra.append("⚠️ 本轮没有领到奖励，原因见账号明细")

    title = None
    if fail == 0 and ok > 0 and skip == 0:
        title = f"{PLATFORM}成功｜领取 {ok}/{total}"
    elif fail == 0 and ok > 0:
        title = f"{PLATFORM}完成｜领取 {ok}/{total}（跳过 {skip}）"
    elif fail == 0 and skip and all("上限" in m for m in skip_msgs):
        title = f"{PLATFORM}今日已达上限｜跳过 {skip}/{total}"
    elif fail == 0:
        title = f"{PLATFORM}无可领取｜跳过 {skip}/{total}"

    return notify_result(
        PLATFORM, total, ok, fail, skip=skip,
        title=title, extra=extra, details=details,
        elapsed=total_elapsed, ok_label="领取",
        footers=["💡 未领到的奖励仍保留在服务端，下次运行自动补领；"
                 "单轮可能持续 1~4 小时，请放宽任务超时"])


if __name__ == "__main__":
    sys.exit(main())
