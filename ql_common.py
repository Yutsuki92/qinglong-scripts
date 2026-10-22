#!/usr/bin/env python3
"""青龙脚本公共层：与本目录脚本配套，订阅会一并同步。

退出码：0 成功 / 1 有账号失败或结果未知 / 2 配置或依赖问题
通知标题：<平台>成功｜3/3、<平台>部分失败｜成功 2/3、<平台>失败｜原因
环境变量：RANDOM_SIGNIN（延迟总开关）、MAX_RANDOM_DELAY（启动延迟上限，默认 300 秒）、
TZ_OFFSET_HOURS（时间展示偏移，留空跟随系统时区）
账号切分：换行与 & 都当分隔符（面板会把同名多行拼成一条）
"""
import base64
import gzip
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.request
import zlib

try:  # requests 是可选依赖：没装就回落 urllib
    import requests as _requests
except Exception:  # 坏包也要能兜住，不能因为 import 失败就退出
    _requests = None

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2

# 容器 LANG 未设或 PYTHONIOENCODING=ascii/gbk 时，logging 写 emoji 会抛 UnicodeEncodeError，
# 整份日志退化成「--- Logging error ---」；只把 errors 改成替换，编码不变。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


def env_str(name, default=""):
    """读取字符串环境变量，空值按默认值处理。"""
    raw = (os.getenv(name) or "").strip()
    return raw or default


def env_int(name, default, minimum=0, maximum=None):
    """读取整型环境变量，脏值回落默认值，并按上下限夹紧。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        try:
            # "12.7" / "1e3" 能识别；inf/nan 会抛异常，不接住就等于变量填错即导入崩溃。
            value = int(float(raw))
        except (ValueError, OverflowError):
            log(f"⚠️  环境变量 {name}={raw!r} 不是整数，已按默认值 {default} 处理",
                logging.WARNING)
            return default
    if minimum is not None and value < minimum:
        log(f"⚠️  环境变量 {name}={value} 小于下限 {minimum}，已按下限处理",
            logging.WARNING)
        return minimum
    if maximum is not None and value > maximum:
        log(f"⚠️  环境变量 {name}={value} 大于上限 {maximum}，已按上限处理",
            logging.WARNING)
        return maximum
    return value


def env_bool(name, default):
    """读取布尔环境变量，支持 true/false、1/0、yes/no、on/off。"""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    log(f"⚠️  环境变量 {name}={raw!r} 无法识别，已按默认值 {default} 处理",
        logging.WARNING)
    return default


# 账号间延迟区间（秒）；env_* 脏值要打警告，故延迟常量统一放在 log 定义之后读取。
ACCOUNT_DELAY_MIN, ACCOUNT_DELAY_MAX = 10, 20


class Logger:
    """统一日志器，写 stdout；实例可当函数用（log("x")、log("x", logging.ERROR)）。"""

    def __init__(self, name):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.INFO)
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            self._logger.addHandler(handler)
            self._logger.propagate = False

    def __call__(self, msg, level=logging.INFO):
        self._logger.log(level, msg)

    def debug(self, msg, **kw):
        self._logger.debug(msg, **kw)

    def info(self, msg, **kw):
        self._logger.info(msg, **kw)

    def warning(self, msg, **kw):
        self._logger.warning(msg, **kw)

    def error(self, msg, **kw):
        self._logger.error(msg, **kw)

    def exception(self, msg, **kw):
        self._logger.exception(msg, **kw)


def get_logger(name):
    """取本脚本专属日志器。"""
    return Logger(name)


log = get_logger("ql_common")

# 必须在 log 定义之后读：env_* 脏值要打警告，提前读会 NameError，所有脚本导入即崩。
RANDOM_SIGNIN = env_bool("RANDOM_SIGNIN", True)
MAX_RANDOM_DELAY = env_int("MAX_RANDOM_DELAY", 300, 0, 3600)

# 时间展示偏移（小时）：留空跟随系统时区，整数则统一按该偏移展示（UTC 容器想看北京时间填 8）。
# 只接受 -14~14：8.5 / UTC+8 会告警并回退，99 会显示成次日凌晨。
_tz_raw = (os.getenv("TZ_OFFSET_HOURS") or "").strip()
TZ_OFFSET_HOURS = None
if _tz_raw:
    if _tz_raw.lstrip("-").isdigit() and abs(int(_tz_raw)) <= 14:
        TZ_OFFSET_HOURS = int(_tz_raw)
    else:
        log(f"⚠️  环境变量 TZ_OFFSET_HOURS={_tz_raw!r} 不是 -14~14 的整数，"
            f"已按系统时区处理", logging.WARNING)
if TZ_OFFSET_HOURS is not None:
    # 让日志行首时间戳也跟随同一偏移，否则消息里的时间会与行首差几个小时
    logging.Formatter.converter = staticmethod(
        lambda secs: time.gmtime(secs + TZ_OFFSET_HOURS * 3600))


def fmt_elapsed(seconds):
    """耗时：1.2 秒 / 3 分 05 秒 / 1 小时 02 分 09 秒。

    先四舍五入再决定单位，否则 59.96 秒会显示成「60.0 秒」这种自相矛盾的文案。
    """
    total = max(0.0, float(seconds or 0))
    secs_int = int(round(total))
    if secs_int < 60:
        return f"{total:.1f} 秒"
    minutes, secs = divmod(secs_int, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes:02d} 分 {secs:02d} 秒"
    return f"{minutes} 分 {secs:02d} 秒"


def fmt_clock(ts=None):
    """绝对时间：YYYY-MM-DD HH:MM:SS。

    默认跟随系统时区，与日志行首时间戳一致；设了 TZ_OFFSET_HOURS 则统一按该偏移。
    """
    t = float(ts if ts else time.time())
    if TZ_OFFSET_HOURS is None:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t + TZ_OFFSET_HOURS * 3600))


def fmt_remaining(seconds):
    """紧凑时长（倒计时用）：1小时2分3秒 / 2分3秒 / 3秒。"""
    total = int(max(0, seconds or 0))
    if total <= 0:
        return "立即执行"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def fmt_left(seconds):
    """有效期：剩余 2.3 天 / 已过期 1.0 小时 / 未知。"""
    if seconds is None:
        return "未知"
    n = abs(int(seconds))
    prefix = "已过期 " if seconds <= 0 else "剩余 "
    for unit, secs in (("年", 365 * 86400), ("天", 86400), ("小时", 3600)):
        if n >= secs:
            return f"{prefix}{n / secs:.1f} {unit}"
    return f"{prefix}{n // 60} 分钟"


def fmt_span(seconds):
    """宽泛区间（无前缀）：2.0 小时 / 30.0 天；非正数返回空串。"""
    try:
        n = float(seconds)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    for unit, secs in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if n >= secs:
            return f"{n / secs:.1f} {unit}"
    return f"{int(n)} 秒"


def wait(seconds, task_name="任务"):
    """带倒计时的等待。"""
    total = int(seconds or 0)
    if total <= 0:
        return
    log(f"⏳ {task_name} 需要等待 {fmt_remaining(total)}")
    remaining = total
    while remaining > 0:
        if remaining <= 10 or remaining % 10 == 0:
            log(f"⏳ {task_name} 倒计时：{fmt_remaining(remaining)}")
        step = 1 if remaining <= 10 else min(10, remaining)
        time.sleep(step)
        remaining -= step


def start_delay(platform):
    """启动前随机延迟，返回实际秒数。"""
    if not RANDOM_SIGNIN or MAX_RANDOM_DELAY <= 0:
        return 0
    delay = random.randint(0, MAX_RANDOM_DELAY)
    if delay > 0:
        log(f"🎲 随机延迟 {fmt_remaining(delay)} 后开始")
        wait(delay, platform)
    return delay


def account_delay(index, total, platform="任务"):
    """账号之间的随机等待（index 从 1 开始，最后一个不再等）。"""
    if not RANDOM_SIGNIN or index >= total:
        return 0
    delay = random.uniform(ACCOUNT_DELAY_MIN, ACCOUNT_DELAY_MAX)
    log(f"⏳ {platform} 等待 {delay:.1f} 秒后处理下一个账号")
    time.sleep(delay)
    return delay


def split_lines(raw, extra=("&",)):
    """按换行与 extra 分隔符切分，去空白，不去重。"""
    if not raw:
        return []
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    for sep in extra:
        if sep:
            text = text.replace(sep, "\n")
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def split_accounts(raw, extra=("&",)):
    """切分多账号并去重，返回 (列表, 被忽略的重复条数)。"""
    items = split_lines(raw, extra)
    unique = list(dict.fromkeys(items))
    return unique, len(items) - len(unique)


def report_duplicates(name, dropped):
    """重复账号的统一提示。"""
    if dropped > 0:
        log(f"⚠️  {name} 中有 {dropped} 个重复账号，已自动忽略")


def normalize_bearer(raw):
    """清洗 Token：去掉 Bearer 前缀、引号与空白。"""
    tok = (raw or "").strip().strip('"').strip("'").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    return tok


def jwt_payload(token):
    """无需密钥解出 JWT 的 payload，失败返回空字典。"""
    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
    except Exception:
        return {}


def jwt_sub(token):
    """JWT 里的用户标识（sub），取不到返回空串。"""
    return str(jwt_payload(token).get("sub") or "")


def jwt_exp(token):
    """JWT 的到期时间戳，取不到返回 None。"""
    try:
        exp = jwt_payload(token).get("exp")
        return int(exp) if exp else None
    except (TypeError, ValueError):
        return None


def jwt_left(token):
    """距 JWT 到期的剩余秒数，取不到返回 None。"""
    exp = jwt_exp(token)
    return exp - int(time.time()) if exp else None


def account_label(uid, index=None):
    """账号显示名：有 uid 取前 8 位，否则用序号。"""
    if uid:
        return str(uid)[:8] + "…"
    return f"账号{index}" if index is not None else "账号"


def bearer_accounts(multi_env, single_env, uid_env=""):
    """读取 Bearer Token 类账号，返回 [(uid, token), ...]（按 token 去重）。

    multi_env 每项可为纯 Token 或 uid:Token，分隔符兼容换行、& 与逗号；先剥引号再拆分，
    否则 uid 会解析成「"10086」，X-User-Id 带引号必然 401。uid 缺省回落 JWT 的 sub，再回落 uid_env。
    """
    raw = (os.getenv(multi_env) or "").strip()
    single = (os.getenv(single_env) or "").strip()
    fallback = (os.getenv(uid_env) or "").strip() if uid_env else ""

    items = []
    for item in raw.replace("&", "\n").replace("\n", ",").split(","):
        item = item.strip().strip('"').strip("'").strip()
        if not item:
            continue
        if item.endswith(":") and item.count(":") == 1:
            # 「uid:」这种漏写 Token 的行，若原样当 Token 用会 401，直接丢弃更清楚
            log(f"⚠️  {multi_env} 有一行缺少 Token：{item[:16]}…，已忽略")
            continue
        uid, tok = "", item
        if item.count(":") == 1:
            left, right = (x.strip() for x in item.split(":", 1))
            if left and right and "." not in left and (
                    "." in right or len(right) > 40):
                uid, tok = left, right
        items.append((uid, normalize_bearer(tok)))
    if single:
        items.append(("", normalize_bearer(single)))

    # 同一账号粘了新旧两个 Token 时只保留第一个：重复会重复派出/等待，旧 Token 必然 401。
    seen_tok, seen_uid, accounts = set(), set(), []
    for uid, tok in items:
        if not tok or tok in seen_tok:
            continue
        if uid and uid in seen_uid:
            log(f"⚠️  uid {str(uid)[:8]}… 重复出现（Token 不同），已只保留第一个")
            continue
        seen_tok.add(tok)
        if uid:
            seen_uid.add(uid)
        accounts.append((uid or jwt_sub(tok) or fallback, tok))
    return accounts


def _decode_body(raw, encoding):
    """按 Content-Encoding 解压响应体；解不开就原样返回（不能因解压失败丢掉响应）。"""
    enc = (encoding or "").strip().lower()
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:  # 裸 deflate（无 zlib 头）也常见
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        return raw
    return raw


def http_text(method, url, headers=None, body=None, timeout=20, retries=2,
              backoff=1.5, opener=None, proxies=None):
    """发请求并返回 (状态码, 响应文本)。

    装了 requests 就用它，否则回落 urllib（回落路径需手动解 gzip，否则 JSON 变乱码）。
    网络异常、5xx、429 会重试（退避递增），其余 4xx 直接返回；彻底失败时状态码 0、文本为原因。
    """
    payload = body.encode("utf-8") if isinstance(body, str) else body
    code, text, last_err = 0, "", ""
    for attempt in range(retries + 1):
        try:
            if _requests is not None:
                resp = _requests.request(method, url, headers=headers, data=payload,
                                         timeout=timeout, proxies=proxies)
                code, text = resp.status_code, resp.text
            else:
                req = urllib.request.Request(url, data=payload,
                                             headers=headers or {}, method=method)
                open_url = opener.open if opener else urllib.request.urlopen
                try:
                    with open_url(req, timeout=timeout) as r:
                        raw = _decode_body(r.read(), r.headers.get("Content-Encoding"))
                        code, text = r.status, raw.decode("utf-8", "ignore")
                except urllib.error.HTTPError as e:
                    raw = _decode_body(e.read(), e.headers.get("Content-Encoding"))
                    code, text = e.code, raw.decode("utf-8", "ignore")
            # 拿到响应才算数：网络异常时 code 为 0，不能当成功返回
            if 0 < code < 500 and code != 429:
                return code, text
            last_err = f"HTTP {code}"
        except Exception as e:
            last_err, code, text = f"{type(e).__name__}: {e}", 0, ""
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    # 重试用尽：拿到过状态码就原样返回，连不上才归 0
    return (code, text) if code else (0, last_err)


def have(module):
    """模块能否真正导入（用真导入而非 find_spec，坏包也能识别）。"""
    try:
        __import__(module)
        return True
    except Exception:
        return False


def require_dep(module, platform, pip_name=None):
    """依赖缺失：打印安装指引 + 通知，返回 False（调用方 return EXIT_CONFIG）。"""
    if module is None or have(module):
        return True
    package = pip_name or module
    msg = f"❌ 缺少 {package} 依赖，相关接口无法请求"
    log(msg, logging.ERROR)
    log("🔧 青龙「依赖管理」→「安装依赖」→ 类型 Python → 粘贴：" + package,
        logging.ERROR)
    notify_config_error(
        platform, "缺少依赖", msg,
        hints=[f"🔧 青龙「依赖管理」→「安装依赖」→ 类型 Python → 粘贴 {package}"],
    )
    return False


def _load_notify():
    """加载青龙生成的 notify.py，返回 send(title, content)；找不到返回 None。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here and here not in sys.path:
        sys.path.insert(0, here)
    try:
        import notify as _notify_mod
        return (getattr(_notify_mod, "send", None)
                or getattr(_notify_mod, "notify", None))
    except Exception:
        return None


def _panel_notify(title, content):
    """走面板「通知设置」里的渠道（QLAPI.systemNotify）。

    QLAPI 仅在面板运行任务时注入，命令行直跑拿不到：True=已发、False=明确失败、None=通道不可用。
    """
    try:
        import builtins
        api = getattr(builtins, "QLAPI", None)
        if api is None or not hasattr(api, "systemNotify"):
            return None
        result = api.systemNotify({"title": title, "content": content})
        if isinstance(result, dict) and result.get("code") not in (None, 200):
            log(f"⚠️  面板通知返回异常：{result.get('message') or result}",
                logging.WARNING)
            return False
        return True
    except Exception as e:
        log(f"⚠️  面板通知调用失败：{type(e).__name__}: {e}", logging.WARNING)
        return None


def notify(title, content):
    """发送通知：优先面板「通知设置」的渠道，其次 notify.py / sendNotify.js。

    失败只告警不中断；只有真正发出才记「已发送」。
    """
    panel = _panel_notify(title, content)
    if panel:
        log(f"📨 通知已发送（面板通知设置）：{title}")
        return True

    send = _load_notify()
    if callable(send):
        try:
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                send(title, content)
            out = buf.getvalue()
            if "无推送渠道" in out or "not found" in out.lower():
                log("⚠️  notify.py 未配置推送渠道，通知未真正发出", logging.WARNING)
                return False
            log(f"📨 通知已发送（notify.py）：{title}")
            return True
        except Exception as e:
            log(f"⚠️  通知发送失败：{type(e).__name__}: {e}", logging.WARNING)
            return False

    for name in ("sendNotify.js", "notify.js"):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        if not os.path.exists(path):
            continue
        try:
            import subprocess
            code = ("const m=require(process.argv[1]);const f=(m&&m.sendNotify)||m;"
                    "Promise.resolve(f(process.argv[2],process.argv[3]))"
                    ".catch(e=>{console.error(e);process.exit(1)})")
            r = subprocess.run(["node", "-e", code, path, title, content],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                log(f"📨 通知已发送（{name}）：{title}")
                return True
            log(f"⚠️  {name} 发送失败：{(r.stderr or '').strip()[:200]}",
                logging.WARNING)
        except Exception as e:
            log(f"⚠️  {name} 调用失败：{type(e).__name__}: {e}", logging.WARNING)

    log("⚠️  通知未发送：面板通知不可用，且无 notify.py / sendNotify.js 可回落",
        logging.WARNING)
    return False


def notify_config_error(platform, reason, detail, hints=()):
    """配置/依赖类失败的统一通知，返回 EXIT_CONFIG 便于直接 return。"""
    log(f"❌ {detail}", logging.ERROR)
    lines = [f"❌ {detail}"]
    if hints:
        lines += [""] + list(hints)
    notify(f"{platform}失败｜{reason}", "\n".join(lines))
    return EXIT_CONFIG


def section(title):
    """小节标题：全角方括号包裹，汉字字体必有这两个字形，任何客户端渲染一致。"""
    return f"【{title}】"


def notify_result(platform, total, ok, fail=None, unknown=0, *,
                  title=None, extra=(), details=(), notes=(), footers=(),
                  elapsed=None, ok_label="成功", skip=0):
    """结果通知的统一模板，返回退出码（0 无失败 / 1 有失败或未知）。

    total/ok 账号数与成功数；fail 省略则按 total-ok-unknown-skip 推算；
    unknown 结果未知数；skip 正常跳过数（不计失败）；title 省略则自动生成。
    """
    fail = (total - ok - unknown - skip) if fail is None else fail
    if title is None:
        if fail == 0 and unknown == 0 and skip == 0:
            title = f"{platform}成功｜{ok}/{total}"
        elif ok == 0 and fail == 0:
            title = f"{platform}无新增｜跳过 {skip}/{total}"
        elif ok == 0:
            title = f"{platform}失败｜{fail} 个账号失败"
            if skip:
                title += f"（跳过 {skip}）"
        else:
            title = f"{platform}部分失败｜成功 {ok}/{total}"
            if skip:
                title += f"（跳过 {skip}）"

    summary = f"📊 共 {total} 个账号｜✅ {ok_label} {ok}｜❌ 失败 {fail}"
    if skip:
        summary += f"｜⏭️ 跳过 {skip}"
    if unknown:
        summary += f"｜⚠️ 未知 {unknown}"
    lines = [summary] + [str(x) for x in extra]
    if elapsed is not None:
        lines.append(f"⏱️ 耗时 {fmt_elapsed(elapsed)}")
    lines.append(f"🕐 完成于 {fmt_clock()}")

    if details:
        lines += ["", section("账号明细")]
        for block in details:
            lines += [str(block).strip(), ""]
    if notes:
        lines.append(section("提醒"))
        lines += [f"⚠️ {n}" for n in notes]
        lines.append("")
    if footers:
        lines.append(section("凭证与提示"))
        lines += [str(x) for x in footers]

    notify(title, "\n".join(lines))
    log(f"==== {platform} 完成 - {ok_label}{ok}/{total}"
        f"{f'（失败{fail}）' if fail else ''} - {fmt_clock()} ====")
    return EXIT_OK if (fail == 0 and unknown == 0) else EXIT_FAIL


def count_results(results, success_key="success", already_key="already_checked",
                  unknown_key=None):
    """统计账号结果，返回 (total, success, already, failed, unknown)。

    success 含「今日已签」，故本次新签 = success - already；failed = total - success - unknown。
    """
    total = len(results)
    success = sum(1 for r in results if r.get(success_key))
    already = sum(1 for r in results if r.get(already_key))
    unknown = sum(1 for r in results if unknown_key and r.get(unknown_key))
    return total, success, already, total - success - unknown, unknown


def result_time_line(r, when_keys=("checked_at", "finished_at"), indent="    "):
    """明细里的时间行：签到时间 / 完成时间 + 耗时；两个字段都没有时返回 None。

    when_keys 首位是「签到时间」的字段名，其后是「完成时间」的字段名。
    """
    for pos, key in enumerate(when_keys):
        when = r.get(key)
        if not when:
            continue
        elapsed = r.get("elapsed")
        suffix = f"（耗时 {fmt_elapsed(elapsed)}）" if elapsed is not None else ""
        label = "🕐 签到时间" if pos == 0 else "🕐 完成时间"
        return f"{indent}{label}：{fmt_clock(when)}{suffix}"
    return None


def print_summary(total, success, already, failed, unknown=0, details=None):
    """控制台汇总：账号数 / 本次新签 / 今日已签 / 失败 / 未知。

    details 传入每账号的行列表时会逐条打印（默认不传，明细已在循环里打过）。
    """
    log("=" * 50)
    log("执行结果统计")
    log("=" * 50)
    log(f"总账号数：{total}")
    log(f"本次新签：{success - already}")
    log(f"今日已签：{already}")
    log(f"签到失败：{failed}")
    if unknown:
        log(f"结果未知：{unknown}")
    if details:
        log("详细结果：")
        for lines in details:
            for line in lines:
                log(line)
    log("=" * 50)


def banner(platform, count=None):
    """统一抬头：横线 + 平台名 + 账号数 + 开始时间。"""
    log("=" * 50)
    log(f"▶️  {platform} 开始" + (f"，共发现 {count} 个账号" if count else ""))
    log("=" * 50)


def account_header(index, tag, total=None):
    """账号分隔行。"""
    prefix = f"[{index}/{total}] " if total else ""
    log(f"\n{prefix}👤 {tag}")
