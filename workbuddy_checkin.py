#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
WorkBuddy 每日自动签到

cron: 0 0 * * *
new Env('WorkBuddy每日签到');

环境变量
  WORKBUDDY_TOKEN   单账号 Token（与 WORKBUDDY_TOKENS 至少填一个）
  WORKBUDDY_TOKENS  多账号 Token，用换行分隔，支持 uid:Token
  WB_USER_ID        可选，X-User-Id 头；缺省时自动从 Token 解析
  WB_WARN_DAYS      可选，Token 剩余天数预警阈值，默认 3
  RANDOM_SIGNIN     可选，延迟开关，默认 true；置 false 关闭全部等待（启动 + 账号间）
  MAX_RANDOM_DELAY  可选，启动随机延迟上限（秒），默认 600
'''

import os
import sys
import json
import time
import random
import base64
import logging
import traceback
from datetime import datetime

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    HAVE_REQUESTS = False

API_BASE = "https://copilot.tencent.com/v2/billing/meter"
STATUS_EP = API_BASE + "/checkin-activity-status"
CHECKIN_EP = API_BASE + "/daily-checkin"

MSG_TOKEN_EXPIRED = "登录态已失效，请在 WorkBuddy 客户端重新登录后更新 Token"
MSG_NO_TOKEN = "未检测到 Token，请设置 WORKBUDDY_TOKEN 或 WORKBUDDY_TOKENS"

# 随机延迟配置
max_random_delay = int(os.getenv("MAX_RANDOM_DELAY", "600"))
random_signin = os.getenv("RANDOM_SIGNIN", "true").lower() == "true"
# Token 剩余天数预警阈值
WARN_DAYS = float(os.environ.get("WB_WARN_DAYS", "3") or 3)


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


# ---------------- 日志 ----------------
LOGGER = logging.getLogger("workbuddy_checkin")
LOGGER.setLevel(logging.INFO)
if not LOGGER.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    LOGGER.addHandler(_handler)
    LOGGER.propagate = False


def log(msg, level=logging.INFO):
    """带时间戳输出，青龙会捕获 stdout 作为任务日志。"""
    LOGGER.log(level, msg)


# ---------------- Token 工具 ----------------
def decode_jwt(token):
    """无需密钥解码 JWT payload，失败返回空字典。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
    except Exception:
        return {}


def token_remaining(token):
    """解析 JWT exp，返回剩余秒数；无法解析返回 None。"""
    try:
        exp = decode_jwt(token).get("exp")
        return int(exp) - int(time.time()) if exp else None
    except Exception:
        return None


def format_remaining(seconds):
    """把剩余秒数格式化为可读文本。"""
    if seconds is None:
        return "未知（Token 非 JWT，无法解析）"
    n = abs(seconds)
    prefix = "已过期 " if seconds <= 0 else "剩余 "
    for unit, secs in (("年", 365 * 86400), ("天", 86400), ("小时", 3600)):
        if n >= secs:
            return f"{prefix}{n / secs:.1f} {unit}"
    return f"{prefix}{n // 60} 分钟"


def token_expire_at(token):
    """解析 JWT exp，返回本地到期时刻（YYYY-MM-DD HH:MM）；无法解析返回空串。"""
    try:
        exp = decode_jwt(token).get("exp")
        if not exp:
            return ""
        return datetime.fromtimestamp(int(exp)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def fmt_elapsed(seconds):
    """耗时渲染：1.2 秒 / 3 分 05 秒 / 1 小时 02 分 09 秒。"""
    total = max(0.0, float(seconds or 0))
    if total < 60:
        return f"{total:.1f} 秒"
    minutes, secs = divmod(int(round(total)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes:02d} 分 {secs:02d} 秒"
    return f"{minutes} 分 {secs:02d} 秒"


def fmt_clock(ts=None):
    """时间戳渲染为 YYYY-MM-DD HH:MM:SS。"""
    return datetime.fromtimestamp(ts if ts else time.time()).strftime("%Y-%m-%d %H:%M:%S")


def token_expiry_warning(token):
    """返回过期预警文案，正常则返回空串。"""
    left = token_remaining(token)
    if left is None:
        return ""
    if left <= 0:
        return "Token 已过期，请立即更新 WORKBUDDY_TOKEN"
    if left / 86400.0 <= WARN_DAYS:
        return f"Token {format_remaining(left)}，请及时更新 WORKBUDDY_TOKEN"
    return ""


def normalize_token(raw):
    """清洗 Token：去掉 Bearer 前缀、引号与空白。"""
    tok = (raw or "").strip().strip('"').strip("'").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    return tok


def account_tag(uid, idx=None):
    """账号显示名：优先 uid 前 8 位，其次序号。"""
    if uid:
        return uid[:8] + "…"
    return f"账号{idx}" if idx is not None else "账号"


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
        log(f"❌ {MSG_NO_TOKEN}")
        return []

    seen, fixed = set(), []
    fallback_uid = os.environ.get("WB_USER_ID", "").strip()
    for uid, tok in tokens:
        if not tok or tok in seen:
            continue
        seen.add(tok)
        # uid 优先级：显式 "uid:Token" > JWT 的 sub > WB_USER_ID
        if not uid:
            uid = decode_jwt(tok).get("sub") or ""
        if not uid:
            uid = fallback_uid
        fixed.append((uid, tok))
    return fixed


# ---------------- HTTP ----------------
def http_request(url, token, uid, method="POST", body=None, retries=2):
    """统一 HTTP 请求，仅对网络异常与 5xx 做指数退避重试，4xx 不重试。"""
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "X-User-Id": uid or "",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    code, text, last_err = 0, "", None
    for attempt in range(retries + 1):
        try:
            if HAVE_REQUESTS:
                resp = requests.request(
                    method, url, headers=headers,
                    data=body.encode("utf-8") if body is not None else None,
                    timeout=20,
                )
                code, text = resp.status_code, resp.text
            else:
                data = body.encode("utf-8") if body is not None else None
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        code, text = r.status, r.read().decode("utf-8", "ignore")
                except urllib.error.HTTPError as e:
                    code, text = e.code, e.read().decode("utf-8", "ignore")
        except Exception as e:
            last_err, code, text = e, 0, ""
        if 200 <= code < 500:
            return code, text
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return code, text if text else (0, f"请求异常: {last_err}")


def post_json(url, token, uid):
    """POST 并解析 JSON，返回 (ok, data_or_err)。"""
    code, text = http_request(url, token, uid, method="POST", body="{}")
    if code == 401:
        return False, "token_expired"
    try:
        payload = json.loads(text)
    except Exception:
        return False, f"接口返回异常({code}): {text[:120]}"
    return True, payload


def query_status(token, uid):
    """查询签到状态，返回 (ok, status, err)。err 为 token_expired 时表示登录态失效。"""
    ok, res = post_json(STATUS_EP, token, uid)
    if not ok:
        return False, {}, res
    if res.get("code") != 0:
        return False, {}, f"状态查询失败 code={res.get('code')} msg={res.get('msg', '')}"
    return True, (res.get("data") or {}), ""


# ---------------- 单账号签到 ----------------
def new_result(token):
    """构造空结果结构，保证字段口径唯一。"""
    return {
        "success": False,
        "message": "",
        "already_checked": False,
        "credit": None,
        "streak_days": None,
        "total_credits": None,
        "is_streak_day": False,
        "status_info": {},
        "token_remaining": token_remaining(token),
        "token_exp_at": token_expire_at(token),
        "checked_at": None,   # 真正执行签到的时刻
        "finished_at": None,  # 该账号处理结束的时刻
        "elapsed": None,      # 该账号耗时（秒）
        "tag": "",
    }


def _apply_status(result, status):
    """把状态接口字段写入结果。"""
    result["status_info"] = status
    result["streak_days"] = status.get("streak_days", 0)
    result["total_credits"] = status.get("total_credits")
    result["is_streak_day"] = bool(status.get("is_streak_day"))


def _mark_already(result, status, message):
    """统一标记「今日已签到」。"""
    result["success"] = True
    result["already_checked"] = True
    result["message"] = message
    result["credit"] = status.get("today_credit")


def checkin_one(uid, token):
    """对单个账号执行签到，返回结构化结果。"""
    result = new_result(token)

    # 1) 查状态（官方接口仅接受 POST）
    ok, status, err = query_status(token, uid)
    if not ok:
        result["message"] = MSG_TOKEN_EXPIRED if err == "token_expired" else err
        return result
    _apply_status(result, status)

    # 2) 活动未开启属正常状态，不算失败
    if not status.get("active", True):
        result["success"] = True
        result["message"] = "签到活动未开启或不适用"
        return result

    # 3) 今日已签到，幂等跳过
    if status.get("today_checked_in"):
        _mark_already(result, status, f"今日已签到（连续 {result['streak_days']} 天）")
        return result

    # 4) 执行签到
    ok, res = post_json(CHECKIN_EP, token, uid)
    if not ok:
        result["message"] = MSG_TOKEN_EXPIRED if res == "token_expired" else res
        return result

    code = res.get("code")
    if code == 0:
        d = res.get("data") or {}
        # 签到接口字段优先，缺失时回退到状态接口
        result["success"] = True
        result["checked_at"] = time.time()
        result["credit"] = d.get("credit", status.get("daily_credit"))
        result["streak_days"] = d.get("streak_days", status.get("streak_days"))
        result["total_credits"] = d.get("total_credits", status.get("total_credits"))
        result["is_streak_day"] = bool(d.get("is_streak_day", status.get("is_streak_day")))
        result["message"] = "签到成功"
        return result

    # 接口以 HTTP 400 + code 10001 表示今日已签
    if code == 10001 or "已签" in (res.get("msg") or ""):
        _mark_already(result, status, "今日已签到（接口确认）")
        return result

    # 5) 失败后复查，兼容并发场景下实际已签成功
    ok2, latest, _ = query_status(token, uid)
    if ok2 and latest.get("today_checked_in"):
        _mark_already(result, latest, "今日已签到（失败后复查确认）")
        return result

    result["message"] = f"签到失败 code={code} msg={res.get('msg', '')}"
    return result


# ---------------- 结果渲染 ----------------
def count_results(results):
    """统一统计口径，返回 (total, success, already, failed)。"""
    total = len(results)
    success = sum(1 for r in results if r["success"])
    already = sum(1 for r in results if r["already_checked"])
    return total, success, already, total - success


def format_detail(r):
    """单账号明细：积分 / 连签 / 累计 / 连签奖励。"""
    detail = []
    if r.get("credit") is not None:
        detail.append(f"积分 +{r['credit']}")
    if r.get("streak_days") is not None:
        detail.append(f"连签 {r['streak_days']} 天")
    if r.get("total_credits") is not None:
        detail.append(f"累计 {r['total_credits']}")
    if r.get("is_streak_day"):
        detail.append("连签奖励")
    return " | ".join(detail)


def format_token_line(r):
    """Token 有效期行：剩余时长 + 到期时刻 + 预警。"""
    left = r.get("token_remaining")
    exp_at = r.get("token_exp_at") or ""
    warn = ""
    if left is not None:
        if left <= 0:
            warn = "  ⚠️ 已过期"
        elif left / 86400.0 <= WARN_DAYS:
            warn = "  ⚠️ 即将过期"
    text = f"🔑 Token 有效期：{format_remaining(left)}{warn}"
    if exp_at:
        text += f"（至 {exp_at}）"
    return text


def result_lines(r, indent=""):
    """单个账号的完整展示行，日志与通知共用。"""
    lines = [f"{indent}{'✅' if r['success'] else '❌'} [{r['tag']}] {r['message']}"]

    detail = format_detail(r)
    if detail:
        lines.append(f"{indent}    🎁 获得：{detail}")

    when = r.get("checked_at") or r.get("finished_at")
    if when:
        elapsed = r.get("elapsed")
        suffix = f"（耗时 {fmt_elapsed(elapsed)}）" if elapsed is not None else ""
        label = "🕐 签到时间" if r.get("checked_at") else "🕐 完成时间"
        lines.append(f"{indent}    {label}：{fmt_clock(when)}{suffix}")

    if r.get("token_remaining") is not None or r.get("token_exp_at"):
        lines.append(f"{indent}    {format_token_line(r)}")
    return lines


def print_summary(results):
    """打印执行结果统计。"""
    total, success, already, failed = count_results(results)
    log("=" * 50)
    log("执行结果统计")
    log("=" * 50)
    log(f"总账号数：{total}")
    log(f"本次新签：{success - already}")
    log(f"今日已签：{already}")
    log(f"签到失败：{failed}")
    log("详细结果：")
    for r in results:
        for line in result_lines(r, indent="  "):
            log(line)
    log("=" * 50)
    return total, success, already, failed


def build_notification(results, counts, warnings=None, started_at=None):
    """构造通知标题与正文。"""
    total, success, already, failed = counts
    if failed == 0:
        title = "WorkBuddy 签到成功"
    elif success == 0:
        title = "WorkBuddy 签到失败"
    else:
        title = "WorkBuddy 签到部分失败"

    lines = [
        f"📊 {total} 个账号｜本次新签 {success - already}｜"
        f"今日已签 {already}｜失败 {failed}"
    ]
    if started_at:
        lines.append(f"⏱️ 总耗时：{fmt_elapsed(time.time() - started_at)}")
    lines += ["", "详细结果："]
    for r in results:
        lines.extend(result_lines(r))

    if warnings:
        lines += ["", "提醒："] + [f"  · {w}" for w in warnings]

    content = "\n".join(lines)
    if len(content) > 1800:
        content = content[:1800] + "\n…（内容过长已截断）"
    return title, content


def main():
    accounts = get_accounts()
    if not accounts:
        notify("WorkBuddy 签到异常", MSG_NO_TOKEN)
        sys.exit(2)

    if random_signin and max_random_delay > 0:
        delay = random.randint(0, max_random_delay)
        log(f"🎲 随机延迟 {delay} 秒后开始")
        time.sleep(delay)

    log("=" * 50)
    log(f"WorkBuddy 自动签到开始，共 {len(accounts)} 个账号")
    log("=" * 50)

    # Token 有效期预检，仅提醒不刷新
    warnings = []
    for uid, token in accounts:
        warn = token_expiry_warning(token)
        if warn:
            warnings.append(f"[{account_tag(uid)}] {warn}")
    for w in warnings:
        log(f"⚠️ {w}", logging.WARNING)

    results = []
    started_at = time.time()
    for idx, (uid, token) in enumerate(accounts, 1):
        tag = account_tag(uid, idx)
        t0 = time.time()
        try:
            r = checkin_one(uid, token)
        except Exception as e:
            traceback.print_exc()
            r = new_result(token)
            r["message"] = f"异常：{type(e).__name__}: {e}"
        r["tag"] = tag
        r["finished_at"] = time.time()
        r["elapsed"] = r["finished_at"] - t0
        results.append(r)

        status = r["status_info"]
        log(f"📊 [{tag}] 连签天数：{status.get('streak_days', 0)} 天 | "
            f"每日积分：{status.get('daily_credit', 0)} | "
            f"累计积分：{status.get('total_credits', 0)}")
        left = r.get("token_remaining")
        overdue = left is not None and left <= WARN_DAYS * 86400
        log(f"🔑 [{tag}] Token {format_remaining(left)}"
            f"{'（至 ' + r['token_exp_at'] + '）' if r.get('token_exp_at') else ''}"
            + ("  ⚠️ 请尽快更新" if overdue else ""))
        log(("✅ " if r["success"] else "❌ ") + f"[{tag}] {r['message']}")
        log(f"⏱️ [{tag}] 耗时 {fmt_elapsed(r['elapsed'])}")

        if idx < len(accounts) and random_signin:
            delay = random.uniform(10, 20)
            log(f"⏱️  等待 {delay:.1f} 秒后处理下一个账号")
            time.sleep(delay)

    counts = count_results(results)
    print_summary(results)
    notify(*build_notification(results, counts, warnings, started_at))

    if counts[3] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
