#!/usr/bin/env python3
"""cron: 0 0 * * *
new Env('WorkBuddy每日签到');

环境变量
  WORKBUDDY_TOKEN   Token；单账号直接填，多账号每行一个，支持 uid:Token
  WB_USER_ID        可选，X-User-Id 头；缺省时自动从 Token 解析
  WB_WARN_DAYS      可选，Token 剩余天数预警阈值，默认 3
  RANDOM_SIGNIN     默认 true，false 关闭全部延迟

依赖：requests、ql_common.py（需同目录）
"""

import sys
import json
import time
import traceback
from datetime import datetime

try:
    from ql_common import (  # noqa: E402
        EXIT_CONFIG, account_delay, account_header, account_label, banner,
        bearer_accounts, count_results, env_int, env_str, fmt_elapsed, fmt_left,
        get_logger, http_text, jwt_exp, jwt_left, notify_config_error,
        notify_result, print_summary, require_dep, result_time_line, start_delay,
    )
except ImportError:  # 公共层没上传/没被订阅同步：给结论，不抛 Traceback
    print("❌ 缺少公共层 ql_common.py：它必须与本脚本放在同一目录")
    print("🔧 青龙「订阅」会同步整个仓库；手动上传时请把它一起传上去")
    sys.exit(2)

PLATFORM = "WorkBuddy每日签到"
log = get_logger("workbuddy_checkin")

API_BASE = "https://copilot.tencent.com/v2/billing/meter"
STATUS_EP = API_BASE + "/checkin-activity-status"
CHECKIN_EP = API_BASE + "/daily-checkin"

MSG_TOKEN_EXPIRED = "登录态已失效，请在 WorkBuddy 客户端重新登录后更新 Token"
MSG_NO_TOKEN = "未检测到 Token，请设置 WORKBUDDY_TOKEN"

# Token 剩余天数预警阈值
WARN_DAYS = env_int("WB_WARN_DAYS", 3, 0, 30)


def token_expire_at(token):
    """Token 到期时刻（YYYY-MM-DD HH:MM），取不到返回空串。"""
    exp = jwt_exp(token)
    return datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M") if exp else ""


def token_expiry_warning(token):
    """返回过期预警文案，正常则返回空串。"""
    left = jwt_left(token)
    if left is None:
        return ""
    if left <= 0:
        return "Token 已过期，请立即更新 WORKBUDDY_TOKEN"
    if left / 86400.0 <= WARN_DAYS:
        return f"Token {fmt_left(left)}，请及时更新 WORKBUDDY_TOKEN"
    return ""


def get_accounts():
    """读取账号列表，返回 [(uid, token), ...]，按 token 去重。

    格式区分、分隔符与 uid 回落规则都在公共层 bearer_accounts 里，
    这里只补上 WB_USER_ID 作为 uid 的最后一级回落。
    """
    return bearer_accounts("WORKBUDDY_TOKEN", "", "WB_USER_ID")


def api_request(url, token, uid, method="POST", body=None, retries=2):
    """请求管理接口，返回 (状态码, 响应文本)；连不上时状态码为 0。"""
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "X-User-Id": uid or "",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    return http_text(method, url, headers=headers, body=body,
                     timeout=20, retries=retries)


def post_json(url, token, uid):
    """POST 并解析 JSON，返回 (ok, data_or_err)。"""
    code, text = api_request(url, token, uid, method="POST", body="{}")
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
        "token_remaining": jwt_left(token),
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
        result["not_applicable"] = True
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
    text = f"🔑 Token 有效期：{fmt_left(left)}{warn}"
    if exp_at:
        text += f"（至 {exp_at}）"
    return text


def result_lines(r, indent=""):
    """单个账号的完整展示行（输出到日志）。"""
    lines = [f"{indent}{'✅' if r['success'] else '❌'} [{r['tag']}] {r['message']}"]

    detail = format_detail(r)
    if detail:
        lines.append(f"{indent}    🎁 获得：{detail}")

    when = result_time_line(r, indent=indent + "    ")
    if when:
        lines.append(when)

    if r.get("token_remaining") is not None or r.get("token_exp_at"):
        lines.append(f"{indent}    {format_token_line(r)}")
    return lines


def main():
    """入口，固定五段：依赖 → 配置 → 延迟 → 逐账号执行 → 汇总通知。"""
    if not require_dep("requests", PLATFORM):
        return EXIT_CONFIG

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
            PLATFORM, "缺少 Token", MSG_NO_TOKEN,
            hints=[
                "🔑 格式：uid:Token（uid 可省略），多账号每行一个",
                "💡 变量名 WORKBUDDY_TOKEN，多账号每行一个",
            ])

    banner(PLATFORM, len(accounts))
    start_delay(PLATFORM)

    # Token 有效期预检：只提醒不刷新，免得临到期才发现
    warnings = []
    for uid, token in accounts:
        warn = token_expiry_warning(token)
        if warn:
            warnings.append(f"[{account_label(uid)}] {warn}")
    for w in warnings:
        log(f"⚠️ {w}")

    run_start = time.time()
    total = len(accounts)
    results = []

    for idx, (uid, token) in enumerate(accounts, 1):
        tag = account_label(uid, idx)
        account_header(idx, tag, total)
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
        log(f"🔑 [{tag}] Token {fmt_left(left)}"
            f"{'（至 ' + r['token_exp_at'] + '）' if r.get('token_exp_at') else ''}"
            + ("  ⚠️ 请尽快更新" if overdue else ""))
        log(("✅ " if r["success"] else "❌ ") + f"[{tag}] {r['message']}")
        log(f"⏱️  [{tag}] 耗时 {fmt_elapsed(r['elapsed'])}")
        account_delay(idx, total, PLATFORM)

    counts = count_results(results)
    print_summary(*counts)
    total, success, already, failed, _unknown = counts
    not_applicable = sum(1 for r in results if r.get("not_applicable"))
    new_sign = success - already - not_applicable

    credits = [r.get("credit") for r in results
               if (r.get("credit") or 0) > 0 and not r.get("already_checked")]
    streaks = [(r.get("streak_days") if r.get("success") else None) for r in results]
    extra = [
        f"🆕 本次新签 {new_sign}｜📅 今日已签 {already}",
        (f"🎁 本次新得：积分 +{sum(credits)}" if credits
         else "🎁 本次新得：无（今日已签或接口未返回积分）"),
        "🔥 连签天数：" + "／".join("-" if s is None else f"{s} 天" for s in streaks),
    ]

    return notify_result(
        PLATFORM, total, success, failed,
        extra=extra,
        details=["\n".join(result_lines(r)) for r in results],
        notes=warnings,
        elapsed=time.time() - run_start,
        footers=[
            f"🔑 Token 剩余有效期见上方；低于 {WARN_DAYS} 天会提醒，过期后需重新抓取",
            "💡 建议在到期前 3 天更新 WORKBUDDY_TOKEN，避免断签",
            "⚠️ 提示「登录态已失效」通常是 Token 过期，不是脚本问题",
        ])


if __name__ == "__main__":
    sys.exit(main())
