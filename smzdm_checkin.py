"""cron: 0 0 * * *
new Env('什么值得买签到')

每日签到 + 签到奖励 + 本月经验，多账号。依赖 requests、ql_common.py（需同目录）。

环境变量
  SMZDM_COOKIE      必填，多账号换行分隔
  RANDOM_SIGNIN     默认 true，false 关闭全部延迟
  MAX_RANDOM_DELAY  默认 300，启动延迟上限（秒）
"""

import time
import random
import hashlib
import re
import sys
from datetime import datetime

try:
    from ql_common import (  # noqa: E402
        EXIT_CONFIG, RANDOM_SIGNIN, account_delay, account_header, banner,
        env_str, fmt_clock, fmt_elapsed, get_logger, notify_config_error, notify_result,
        report_duplicates, require_dep, split_accounts, start_delay,
    )
except ImportError:  # 公共层没上传/没被订阅同步：给结论，不抛 Traceback
    print("❌ 缺少公共层 ql_common.py：它必须与本脚本放在同一目录")
    print("🔧 青龙「订阅」会同步整个仓库；手动上传时请把它一起传上去")
    sys.exit(2)

PLATFORM = "什么值得买签到"
logger = get_logger("smzdm_checkin")

try:
    import requests
except ImportError:  # 缺依赖不在这里崩，交由 main 的 require_dep 出结论
    requests = None


def to_int(value):
    """把「1,234」「1.2万」这类展示值转成 int，取不到返回 None。

    旧实现直接剔除非数字字符，「1.2万」会被压成 12（「万」被丢掉），
    金币过万后算出来的差值就会离谱。
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    scale = 1
    if text.endswith("万"):
        scale, text = 10000, text[:-1]
    elif text.endswith("亿"):
        scale, text = 100000000, text[:-1]
    m = re.search(r'-?\d+(?:\.\d+)?', text)
    if not m:
        return None
    try:
        return int(float(m.group()) * scale)
    except ValueError:
        return None


SECRET_KEY = "zok5JtAq3$QixaA%mncn*jGWlEpSL3E1"
USER_API_URL = "https://user-api.smzdm.com"
APP_VERSION = '11.1.35'
APP_UA = 'smzdm 11.1.35 rv:167 (iPhone 6s; iOS 15.8.3; zh_CN)/iphone_smzdmapp/11.1.35'
H5_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 15_6 like Mac OS X) '
         'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148/smzdm 10.4.6 '
         'rv:130.1 (iPhone 13; iOS 15.6; zh_CN)/iphone_smzdmapp/10.4.6/wkwebview/jsbv_1.0.0')


def calculate_sign(data):
    """按 key 排序拼串 + 固定 key 后缀，大写 MD5"""
    sorted_keys = sorted(data.keys())
    parts = []
    for key in sorted_keys:
        value = data[key]
        if value is not None and value != "" and value != []:
            value_str = re.sub(r'[^\S\r\n]+', '', str(value))
            if value_str:
                parts.append(f"{key}={value_str}")
    query_string = "&".join(parts) + f"&key={SECRET_KEY}"
    return hashlib.md5(query_string.encode('utf-8')).hexdigest().upper()


class SmzdmAPI:
    """什么值得买签到接口封装"""

    def __init__(self, cookie: str):
        self.cookie = cookie
        self.last_error = None  # 最近一次 API 错误原因，供上层展示
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': APP_UA,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh-Hans;q=0.9',
            'Cookie': cookie,
        })

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def _request(self, method, url, **kwargs):
        try:
            resp = self.session.request(method, url, timeout=30, **kwargs)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            logger.error(f"    ❌ 请求超时: {url}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"    ❌ 请求失败: {e}")
            return None
        except ValueError as e:
            logger.error(f"    ❌ JSON 解析失败: {e}")
            return None

        code = data.get('error_code')
        if code not in [0, '0', None]:
            msg = data.get('error_msg') or '未知错误'
            logger.error(f"    ❌ API 错误: {msg} (错误码: {code})")
            # 记下来，让上层能把真实原因写进日志与通知
            self.last_error = f"{msg}（错误码 {code}）"
            return None
        return data

    def _app_headers(self):
        h = dict(self.session.headers)
        h.update({
            'User-Agent': APP_UA,
            'Content-Type': 'application/x-www-form-urlencoded',
            'request_key': str(int(time.time() * 1000000000))[:18],
            'Content-Encoding': 'gzip',
            'Accept-Language': 'zh-Hans-CN;q=1',
        })
        return h

    def _signed_post(self, url, params):
        params = dict(params)
        params['sign'] = calculate_sign(params)
        return self._request('POST', url, data=params, headers=self._app_headers())

    def daily_checkin(self):
        """
        每日签到（只需签名，无需 token）
        Returns: (ok, msg, data, already)
            already=True 表示今天此前已签过，此时 ok 同样为 True
        """
        ts = int(time.time() * 1000)
        params = {
            'basic_v': '0', 'f': 'iphone', 'time': str(ts),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        data = self._signed_post(f'{USER_API_URL}/checkin', params)
        if not data:
            return False, (self.last_error or '签到请求失败'), None, False

        body = data.get('data') or {}
        code = data.get('error_code')

        if code not in (0, '0') and not body:
            return False, (data.get('error_msg')
                           or '响应结构异常，未取得签到结果（可能被风控或接口变更）'), data, False

        msg = (data.get('error_msg') or body.get('sub_title') or '').strip()
        already = '已签到' in msg
        return True, (msg or '签到成功'), body, already


def get_user_info(cookie, quiet=False):
    """抓个人中心页取昵称/等级/金币/碎银，失败一律返回未知值。

    用户名与资产由结果块统一渲染，这里只留进度提示，避免同一个数字打两遍。
    """
    def _p(msg):
        if not quiet:
            logger.info(msg)

    try:
        _p("👤 正在获取用户信息...")
        headers = {
            'Host': 'zhiyou.smzdm.com',
            'Accept': '*/*',
            'Connection': 'keep-alive',
            'Cookie': cookie,
            'User-Agent': H5_UA,
            'Accept-Language': 'zh-CN,zh-Hans;q=0.9',
            'Referer': 'https://m.smzdm.com/',
        }
        resp = requests.get('https://zhiyou.smzdm.com/user/', headers=headers, timeout=15)
        # 风控时返回的是 412/403 的提示页，正则匹配不到会静默降级成「未知用户」，
        # 看起来像正常，所以先看状态码再解析。
        if resp.status_code != 200:
            logger.warning(f"    ⚠️ 用户页返回 HTTP {resp.status_code}，可能被风控")
            return "未知用户", "0", "", ""
        html = resp.text

        name_match = re.search(r'<a href="https://zhiyou.smzdm.com/user">\s*(.*?)\s*</a>', html)
        level_match = re.search(r'/level/(\d+)\.png', html)
        gold_match = re.search(
            r'<div class="assets-part assets-gold">.*?assets-num">(.*?)</span>', html, re.S)
        silver_match = re.search(
            r'<div class="assets-part assets-prestige">.*?assets-num">(.*?)</span>', html, re.S)

        name = name_match.group(1).strip() if name_match else "未知用户"
        level = level_match.group(1) if level_match else "0"
        # 解析不到就留空：之前回落 "0" 会让「签到后资产」变成 0，
        # 与签到前相减得出大额负数，通知里出现「今日获得 -1234 金币」
        gold = gold_match.group(1).strip() if gold_match else ""
        silver = silver_match.group(1).strip() if silver_match else ""
        return name, level, gold, silver
    except Exception as e:
        _p(f"    ⚠️ 获取用户信息失败: {e}")
        return "未知用户", "0", "", ""


def get_monthly_exp(cookie):
    """本月经验合计；一页页翻到出现上个月或空页为止，取不到返回 None。

    每页 20 条，某月记录超过 60 条时旧实现（写死 3 页）会少算，
    这里改成翻到跨月为止，另给 12 页兜底防止接口异常导致死循环。
    """
    current_month = datetime.now().strftime('%Y-%m')
    headers = {
        'Host': 'zhiyou.m.smzdm.com',
        'Accept': 'application/json, text/plain, */*',
        'Connection': 'keep-alive',
        'Cookie': cookie,
        'User-Agent': H5_UA,
        'Accept-Language': 'zh-CN,zh-Hans;q=0.9',
        'Referer': 'https://zhiyou.m.smzdm.com/user/exp/',
    }
    logger.info("📊 正在获取本月经验...")
    total, got_any = 0, False
    try:
        for page in range(1, 13):
            resp = requests.get(f'https://zhiyou.m.smzdm.com/user/exp/ajax_log?page={page}',
                                headers=headers, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"    ⚠️ 经验日志第 {page} 页返回 HTTP {resp.status_code}")
                break
            rows = (resp.json().get('data') or {}).get('rows') or []
            if not rows:
                break

            reached_prev_month = False
            for row in rows:
                exp_date = str(row.get('creation_date') or '')[:7]
                if not exp_date:
                    continue
                if exp_date == current_month:
                    got_any = True
                    try:
                        total += int(row.get('add_exp') or 0)
                    except (TypeError, ValueError):
                        # 单条脏数据不该让整月归零，跳过并说出来
                        logger.warning(f"    ⚠️ 跳过一条无法解析的经验记录: {row.get('add_exp')!r}")
                elif exp_date < current_month:
                    reached_prev_month = True
            if reached_prev_month:
                break
            if RANDOM_SIGNIN:
                time.sleep(random.uniform(0.5, 1.5))

        if not got_any:
            logger.warning("    ⚠️ 未取到本月经验记录")
            return None
        return total
    except Exception as e:
        logger.warning(f"    ⚠️ 获取本月经验失败: {e}")
        return None


def smzdm_signin(cookie, index):
    """
    单账号签到：用户信息 → 签到 → 本月经验
    Returns: (final_msg, is_success)
    """
    started = time.time()
    try:
        name, level, gold, silver = get_user_info(cookie)

        api = SmzdmAPI(cookie)
        try:
            logger.info("🎯 正在执行签到...")
            ok, signin_msg, checkin_data, already = api.daily_checkin()
            sign_at = time.time()

            # 连签天数在 data.daily_num 里（旧代码读的 continue_checkin_days 不存在，恒为 0）
            continuous_days = to_int((checkin_data or {}).get('daily_num')) or 0

            monthly_exp = get_monthly_exp(cookie)

            # 签到响应里的 cgold 就是签到后的金币，比再抓一次 H5 页面可靠
            gold_now = (checkin_data or {}).get('cgold') or gold

            if not ok:
                state_line = f"❌ 签到失败: {signin_msg}"
            elif already:
                state_line = "✅ 今日已签到"
            else:
                state_line = "✅ 签到成功"
            if continuous_days:
                state_line += f"｜连签 {continuous_days} 天"

            final_msg = (
                f"👤 第{index}个账号 {name}｜VIP{level}\n"
                f"💰 金币: {gold_now or '未知'}｜🪙 碎银: {silver or '未知'}\n"
                f"📊 本月经验: {monthly_exp if monthly_exp is not None else '未知'}\n"
                f"{state_line}\n"
                f"🕐 {'签到' if ok else '完成'}时间: {fmt_clock(sign_at)}"
                f"｜耗时 {fmt_elapsed(time.time() - started)}"
            )

            # 逐行打日志，青龙里每行都带时间戳，与其他脚本的结果块对齐
            for line in final_msg.splitlines():
                logger.info(line)
            return final_msg, ok

        finally:
            api.close()

    except requests.exceptions.Timeout:
        error_msg = (f"❌ 账号{index}: 请求超时，网络连接可能有问题\n"
                     f"⏱️ 本次耗时: {fmt_elapsed(time.time() - started)}")
        logger.error(error_msg)
        return error_msg, False
    except requests.exceptions.ConnectionError:
        error_msg = (f"❌ 账号{index}: 网络连接错误，无法连接到服务器\n"
                     f"⏱️ 本次耗时: {fmt_elapsed(time.time() - started)}")
        logger.error(error_msg)
        return error_msg, False
    except Exception as e:
        error_msg = (f"❌ 账号{index}: 签到异常 - {str(e)}\n"
                     f"⏱️ 本次耗时: {fmt_elapsed(time.time() - started)}")
        logger.error(error_msg)
        return error_msg, False


def main():
    """入口，固定五段：依赖 → 配置 → 延迟 → 逐账号执行 → 汇总通知。"""
    if not require_dep("requests", PLATFORM):
        return EXIT_CONFIG

    raw = env_str("SMZDM_COOKIE")
    if not raw:
        return notify_config_error(
            PLATFORM, "缺少 Cookie",
            "未找到 SMZDM_COOKIE 环境变量",
            hints=[
                "🔑 登录 zhiyou.smzdm.com → F12 → 复制完整 Cookie",
                "💡 多账号每行一个",
            ])

    cookies, dropped = split_accounts(raw)
    report_duplicates("SMZDM_COOKIE", dropped)
    if not cookies:
        return notify_config_error(
            PLATFORM, "Cookie 无效",
            "SMZDM_COOKIE 为空或格式无效",
            hints=["📝 重新复制整条 Cookie，不要把值清空后只留换行"])

    banner(PLATFORM, len(cookies))
    start_delay(PLATFORM)

    run_start = time.time()
    total = len(cookies)
    success_count = 0
    details = []

    for i, cookie in enumerate(cookies, 1):
        account_header(i, f"账号{i}", total)
        try:
            result_msg, is_success = smzdm_signin(cookie, i)
        except Exception as e:
            result_msg = f"处理异常：{type(e).__name__}: {e}"
            is_success = False
            logger.info(f"❌ 账号{i} {result_msg}")
        if is_success:
            success_count += 1
        details.append(f"{'✅' if is_success else '❌'} 账号{i}\n{str(result_msg).strip()}")
        account_delay(i, total, PLATFORM)

    return notify_result(
        PLATFORM, total, success_count, total - success_count,
        details=details, elapsed=time.time() - run_start,
        footers=["🔑 失效时重新登录 zhiyou.smzdm.com，复制完整 Cookie 到 SMZDM_COOKIE"])


if __name__ == "__main__":
    sys.exit(main())
