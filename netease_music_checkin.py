#!/usr/bin/env python3
"""cron: 0 0 * * *
new Env('网易云音乐签到')

每日签到领云贝，走 weapi（AES-128-CBC + RSA）。依赖 requests、pycryptodome、ql_common.py。

环境变量
  NETEASE_COOKIE    Cookie，单账号直接填，多账号每行一个，# 开头为注释
  RANDOM_SIGNIN     默认 true，false 关闭全部延迟
  MAX_RANDOM_DELAY  默认 300，启动延迟上限（秒）

Cookie 至少含 MUSIC_U 与 __csrf，缺一个都签不上；整条粘贴即可，换行分隔的字段会自动拼回一行。
MUSIC_U 通常数月过期，失效后接口返回 301，重新登录重取。
"""
import os
import sys
import json
import time
import base64
import re
import traceback
import urllib.parse

try:
    from ql_common import (  # noqa: E402
        account_delay, account_header, banner, count_results, env_str,
        fmt_elapsed, get_logger, http_text, notify_config_error,
        notify_result, print_summary, result_time_line, split_lines, start_delay,
    )
except ImportError:  # 公共层没上传/没被订阅同步：给结论，不抛 Traceback
    print("❌ 缺少公共层 ql_common.py：它必须与本脚本放在同一目录")
    print("🔧 青龙「订阅」会同步整个仓库；手动上传时请把它一起传上去")
    sys.exit(2)

PLATFORM = "网易云音乐签到"
log = get_logger("netease_music_checkin")

try:
    from Crypto.Cipher import AES
    HAVE_AES = True
except ImportError:
    HAVE_AES = False

WEAPI_SIGN_URL = "https://music.163.com/weapi/point/dailyTask"

TIMEOUT = 20  # 单次请求超时（秒）
RETRIES = 2   # 5xx 与网络异常的额外重试次数

BASE_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://music.163.com",
    "Referer": "https://music.163.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}

MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
    "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
    "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932"
    "575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b"
    "3ece0462db0a22b8e7"
)
PUBKEY = "010001"
_ENC_KEY = b"0CoJUm6Qyw8W8jud"
_ENC_IV = b"0102030405060708"

MSG_NO_COOKIE = "未检测到 Cookie，请设置 NETEASE_COOKIE"
MSG_NO_VALID = "NETEASE_COOKIE 中没有有效行（空行与 # 开头的注释会被跳过）"
MSG_COOKIE_EXPIRED = "登录态已失效（code=301），请重新登录后更新 Cookie"
MSG_MISSING_MUSIC_U = "Cookie 缺少 MUSIC_U，请重新登录获取"
MSG_MISSING_CSRF = "Cookie 缺少 __csrf，请把整条 Cookie 一起复制（仅 MUSIC_U 会返回 -460）"
MSG_NO_AES = "缺少依赖 pycryptodome，无法完成 weapi 加密"


def _aes_encrypt(plaintext, key, iv):
    """AES-128-CBC 加密，带 PKCS7 填充，返回 bytes。"""
    padlen = 16 - (len(plaintext) % 16)
    plaintext = plaintext + bytes([padlen]) * padlen
    return AES.new(key, AES.MODE_CBC, iv).encrypt(plaintext)


def _rsa_encrypt(text):
    """原始 RSA 公钥加密（weapi 专用：反转字节 → int → pow → 256 位 hex）。"""
    c = pow(int.from_bytes(text[::-1], "big"), int(PUBKEY, 16), int(MODULUS, 16))
    return format(c, "x").zfill(256)


def weapi_encrypt(plain_dict):
    """weapi 加密，返回 {'params', 'encSecKey'}。

    两层 AES：固定密钥加密原始 JSON，再用 16 字节随机密钥加密第一层结果；RSA 只加密随机密钥。
    """
    data = json.dumps(plain_dict, separators=(",", ":")).encode("utf-8")
    secret = base64.b64encode(os.urandom(16)).decode()[:16].encode()
    inner_b64 = base64.b64encode(_aes_encrypt(data, _ENC_KEY, _ENC_IV)).decode()
    params = base64.b64encode(
        _aes_encrypt(inner_b64.encode("utf-8"), secret, _ENC_IV)
    ).decode()
    return {
        "params": params,
        "encSecKey": _rsa_encrypt(secret),
    }


def parse_cookie(cookie_str):
    """Cookie 字符串 → dict，用于提取 MUSIC_U / __csrf。"""
    d = {}
    for part in cookie_str.split(";"):
        k, _, v = part.partition("=")
        if v.strip():
            d[k.strip()] = v.strip()
    return d


def normalize_cookie(raw):
    """清洗 Cookie：去引号、换行转分号、丢弃空字段。

    从 F12 的 Cookie 表格里逐行复制时字段常被换行拆开，这里统一成一行。
    """
    parts = re.sub(r"[\r\n]+", ";", (raw or "").strip().strip("\"'")).split(";")
    return "; ".join(p.strip() for p in parts if p.strip())


def mask_cookie(cookie_str):
    """脱敏展示：只显示 MUSIC_U / __csrf，值保留首尾各 6 / 4 位。"""
    ck = parse_cookie(cookie_str)
    return "; ".join(
        f"{k}={ck[k][:6]}***{ck[k][-4:]}" if len(ck[k]) > 10 else f"{k}={ck[k]}"
        for k in ("MUSIC_U", "__csrf") if ck.get(k)
    )


def get_accounts():
    """读取 Cookie 列表，返回 (cookies, 错误提示)。

    NETEASE_COOKIE 每行一个账号，# 开头为注释；__csrf 单独成行时并回上一个账号。
    面板会把同名多行用 & 拼成一条，故 & 也当分隔符（MUSIC_U / __csrf 本身不含 &）。
    """
    raw = env_str("NETEASE_COOKIE")

    cookies = []
    for line in split_lines(raw):
        line = normalize_cookie(line)
        if not line or line.startswith("#"):
            continue
        if cookies and "__csrf" in line and "MUSIC_U" not in line:
            cookies[-1] += "; " + line
        else:
            cookies.append(line)

    cookies = list(dict.fromkeys(cookies))
    if not cookies:
        err = MSG_NO_VALID if raw else MSG_NO_COOKIE
        log(f"❌ {err}")
        return [], err
    return cookies, ""


def new_result():
    """构造空结果结构，保证字段口径唯一。"""
    return {
        "success": False,
        "already_checked": False,
        "message": "",
        "point": None,        # 本次获得的云贝
        "checked_at": None,   # 真正执行签到的时刻
        "finished_at": None,  # 该账号处理结束的时刻
        "elapsed": None,      # 该账号耗时（秒）
        "tag": "",
    }


def checkin_one(cookie):
    """对单个账号执行签到，返回结构化结果。"""
    result = new_result()

    ck = parse_cookie(cookie)
    if "MUSIC_U" not in ck:
        result["message"] = MSG_MISSING_MUSIC_U
        return result
    csrf = ck.get("__csrf", "")
    if not csrf:
        result["message"] = MSG_MISSING_CSRF
        return result

    # 明文参数 → weapi 加密 → 表单提交
    body = urllib.parse.urlencode(weapi_encrypt({"type": 1, "csrf_token": csrf}))
    code, text = http_text(
        "POST", WEAPI_SIGN_URL, headers=dict(BASE_HEADERS, Cookie=cookie),
        body=body, timeout=TIMEOUT, retries=RETRIES)
    if code != 200:
        result["message"] = (f"HTTP {code}：{text[:120]}" if code
                             else f"网络异常：{text}")
        return result

    try:
        payload = json.loads(text)
    except ValueError:
        result["message"] = f"接口返回非 JSON：{text[:120]}"
        return result
    if not isinstance(payload, dict):
        # 拿到数组/字符串时 .get 会抛 AttributeError，被当成代码缺陷
        result["message"] = f"接口返回结构异常：{text[:120]}"
        return result

    api_code = payload.get("code")
    msg = payload.get("message") or payload.get("msg") or ""
    if api_code == 200:
        result["success"] = True
        result["point"] = payload.get("point")
        result["checked_at"] = time.time()
        result["message"] = "签到成功"
    elif api_code == -2 or "重复" in msg:
        # 今日已签到，幂等处理，不算失败
        result["success"] = True
        result["already_checked"] = True
        result["point"] = payload.get("point")
        result["message"] = "今日已签到"
    elif api_code == 301:
        result["message"] = MSG_COOKIE_EXPIRED
    else:
        result["message"] = f"签到失败 code={api_code} msg={msg}"
    return result


def format_detail(r):
    """单账号明细：本次拿到的云贝；今日早前已签的标注「本次无新增」。"""
    if r.get("already_checked"):
        if r.get("point"):
            return f"本次无新增（今日早前已签 +{r['point']} 云贝）"
        return "本次无新增（今日早前已签）"
    if r.get("point"):
        return f"+{r['point']} 云贝"
    return "无（接口未返回云贝数量）"


def result_lines(r, indent=""):
    """单个账号的完整展示行（输出到日志）。"""
    lines = [f"{indent}{'✅' if r['success'] else '❌'} [{r['tag']}] {r['message']}"]

    if r.get("success"):
        lines.append(f"{indent}    🎁 获得：{format_detail(r)}")

    when = result_time_line(r, indent=indent + "    ")
    if when:
        lines.append(when)
    return lines


def main():
    """入口，固定五段：依赖 → 配置 → 延迟 → 逐账号执行 → 汇总通知。

    依赖检查放在配置检查之前：缺 pycryptodome 时无论 Cookie 对不对都签不了，
    先给环境结论，别让人误以为是 Cookie 过期。
    """
    if not HAVE_AES:
        return notify_config_error(
            PLATFORM, "缺少依赖", MSG_NO_AES,
            hints=["🔧 青龙「依赖管理」→「安装依赖」→ 类型 Python → 粘贴 pycryptodome"])

    accounts, err = get_accounts()
    if not accounts:
        return notify_config_error(
            PLATFORM, "缺少 Cookie", err or MSG_NO_COOKIE,
            hints=[
                "🔑 需要 MUSIC_U 与 __csrf 两个字段",
                "💡 登录 music.163.com → F12 → Application → Cookies 复制",
                "📝 变量名 NETEASE_COOKIE，多账号每行一个",
            ])

    banner(PLATFORM, len(accounts))
    start_delay(PLATFORM)

    run_start = time.time()
    total = len(accounts)
    results = []

    for idx, cookie in enumerate(accounts, 1):
        account_header(idx, f"账号{idx} {mask_cookie(cookie)}", total)
        t0 = time.time()
        try:
            r = checkin_one(cookie)
        except Exception as e:
            traceback.print_exc()
            r = new_result()
            r["message"] = f"异常：{type(e).__name__}: {e}"
        r["tag"] = f"账号{idx}"
        r["finished_at"] = time.time()
        r["elapsed"] = r["finished_at"] - t0
        results.append(r)

        log(("✅ " if r["success"] else "❌ ") + f"[{r['tag']}] {r['message']}")
        if r["success"]:
            log(f"🎁 [{r['tag']}] {format_detail(r)}")
        log(f"⏱️  [{r['tag']}] 耗时 {fmt_elapsed(r['elapsed'])}")
        account_delay(idx, total, PLATFORM)

    counts = count_results(results)
    print_summary(*counts)
    total, success, already, failed, _unknown = counts
    new_sign = success - already

    points = [r.get("point") for r in results
              if (r.get("point") or 0) > 0 and not r.get("already_checked")]
    extra = [
        f"🆕 本次新签 {new_sign}｜📅 今日已签 {already}",
        (f"🎁 本次新得：+{sum(points)} 云贝" if points
         else "🎁 本次新得：无（或接口未返回云贝数）"),
    ]

    return notify_result(
        PLATFORM, total, success, failed,
        extra=extra,
        details=["\n".join(result_lines(r)) for r in results],
        elapsed=time.time() - run_start,
        footers=["🔑 失效时重新登录 music.163.com，复制 MUSIC_U 与 __csrf"])


if __name__ == "__main__":
    sys.exit(main())
