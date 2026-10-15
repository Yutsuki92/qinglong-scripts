"""
cron: 0 0 * * *
new Env('什么值得买签到')
"""

"""
什么值得买 每日签到（纯签到版 · 青龙单文件）
功能：每日签到 + 签到奖励 + 本月经验，支持多账号
版本：3.1-checkin
说明：由 1-SMZDM_checkin.py 拆分而来，不含众测/互动任务模块
依赖：requests（青龙自带）；通知走面板内置通知（QLAPI.systemNotify）
环境变量：
  SMZDM_COOKIE       必填，多账号用换行分隔
  RANDOM_SIGNIN      可选，默认 true，延迟开关；置 false 关闭全部等待（启动 + 账号间）
  MAX_RANDOM_DELAY   可选，默认 600，启动随机延迟上限（秒）
"""

import json
import time
import random
import hashlib
import os
import re
import sys
import logging
from datetime import datetime, timedelta

import requests

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

# ---------------- 环境变量 ----------------
# 随机延迟配置
max_random_delay = int(os.getenv("MAX_RANDOM_DELAY", "600"))
random_signin = os.getenv("RANDOM_SIGNIN", "true").lower() == "true"

# ---------------- 常量 ----------------
SECRET_KEY = "zok5JtAq3$QixaA%mncn*jGWlEpSL3E1"
USER_API_URL = "https://user-api.smzdm.com"
APP_VERSION = '11.1.35'
APP_UA = 'smzdm 11.1.35 rv:167 (iPhone 6s; iOS 15.8.3; zh_CN)/iphone_smzdmapp/11.1.35'
H5_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 15_6 like Mac OS X) '
         'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148/smzdm 10.4.6 '
         'rv:130.1 (iPhone 13; iOS 15.6; zh_CN)/iphone_smzdmapp/10.4.6/wkwebview/jsbv_1.0.0')


# ==================== 日志 ====================
def setup_logger():
    logger = logging.getLogger("smzdm_checkin")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-7s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(handler)
    return logger


logger = setup_logger()


# ==================== 签名 ====================
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


# ==================== 通用工具 ====================
def format_time_remaining(seconds):
    if seconds <= 0:
        return "立即执行"
    hours, minutes = divmod(seconds, 3600)
    minutes, secs = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分{secs}秒"
    elif minutes > 0:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def wait_with_countdown(delay_seconds, task_name):
    if delay_seconds <= 0:
        return
    print(f"{task_name} 需要等待 {format_time_remaining(delay_seconds)}")
    remaining = delay_seconds
    while remaining > 0:
        if remaining <= 10 or remaining % 10 == 0:
            print(f"{task_name} 倒计时: {format_time_remaining(remaining)}")
        sleep_time = 1 if remaining <= 10 else min(10, remaining)
        time.sleep(sleep_time)
        remaining -= sleep_time


# ==================== API 层（仅签到相关） ====================
class SmzdmAPI:
    """什么值得买签到接口封装"""

    def __init__(self, cookie: str):
        self.cookie = cookie
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
            logger.error(f"    ❌ API 错误: {data.get('error_msg', '未知错误')} "
                         f"(错误码: {code})")
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

    # ---------- 每日签到 ----------
    def daily_checkin(self):
        """
        每日签到（只需签名，无需 token）
        Returns: (ok, msg, data)
        """
        ts = int(time.time() * 1000)
        params = {
            'basic_v': '0', 'f': 'iphone', 'time': str(ts),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        data = self._signed_post(f'{USER_API_URL}/checkin', params)
        if not data:
            return False, '签到请求失败', None

        body = data.get('data') or {}
        msg = body.get('sub_title') or body.get('error_msg') or '签到成功'
        return True, msg, body


# ==================== 用户信息（H5） ====================
def get_user_info(cookie):
    try:
        print("👤 正在获取用户信息...")
        headers = {
            'Host': 'zhiyou.smzdm.com',
            'Accept': '*/*',
            'Connection': 'keep-alive',
            'Cookie': cookie,
            'User-Agent': H5_UA,
            'Accept-Language': 'zh-CN,zh-Hans;q=0.9',
            'Referer': 'https://m.smzdm.com/',
        }
        resp = requests.get('https://zhiyou.smzdm.com/user/', headers=headers,
                            timeout=15).text

        name_match = re.search(r'<a href="https://zhiyou.smzdm.com/user">\s*(.*?)\s*</a>', resp)
        level_match = re.search(r'/level/(\d+)\.png', resp)
        gold_match = re.search(
            r'<div class="assets-part assets-gold">.*?assets-num">(.*?)</span>', resp, re.S)
        silver_match = re.search(
            r'<div class="assets-part assets-prestige">.*?assets-num">(.*?)</span>', resp, re.S)

        name = name_match.group(1).strip() if name_match else "未知用户"
        level = level_match.group(1) if level_match else "0"
        gold = gold_match.group(1).strip() if gold_match else "0"
        silver = silver_match.group(1).strip() if silver_match else "0"

        print(f"👤 用户: {name} (VIP{level})")
        print(f"💰 金币: {gold}, 🪙 碎银: {silver}")
        return name, level, gold, silver
    except Exception as e:
        print(f"❌ 获取用户信息失败: {e}")
        return "未知用户", "0", "0", "0"


def get_monthly_exp(cookie):
    try:
        print("📊 正在获取本月经验...")
        current_month = datetime.now().strftime('%Y-%m')
        total_exp = 0
        headers = {
            'Host': 'zhiyou.m.smzdm.com',
            'Accept': 'application/json, text/plain, */*',
            'Connection': 'keep-alive',
            'Cookie': cookie,
            'User-Agent': H5_UA,
            'Accept-Language': 'zh-CN,zh-Hans;q=0.9',
            'Referer': 'https://zhiyou.m.smzdm.com/user/exp/',
        }
        for page in range(1, 4):
            url = f'https://zhiyou.m.smzdm.com/user/exp/ajax_log?page={page}'
            resp = requests.get(url=url, headers=headers, timeout=10)
            if resp.status_code != 200:
                break
            rows = (resp.json().get('data') or {}).get('rows') or []
            if not rows:
                break
            for row in rows:
                exp_date = str(row.get('creation_date', ''))[:7]
                if exp_date == current_month:
                    total_exp += int(row.get('add_exp', 0) or 0)
                elif exp_date < current_month:
                    print(f"📊 本月经验: {total_exp}")
                    return total_exp
            time.sleep(random.uniform(0.5, 1.5))
        print(f"📊 本月经验: {total_exp}")
        return total_exp
    except Exception as e:
        print(f"❌ 获取月度经验失败: {e}")
        return 0


# ==================== 单账号签到 ====================
def smzdm_signin(cookie, index):
    """
    单账号签到：用户信息 → 签到 → 本月经验
    Returns: (final_msg, is_success)
    """
    print(f"\n==== 开始第{index}个帐号签到 ====")

    try:
        name, level, gold, silver = get_user_info(cookie)

        api = SmzdmAPI(cookie)
        try:
            print("🎯 正在执行签到...")
            ok, signin_msg, checkin_data = api.daily_checkin()
            print(f"🎯 签到状态: {signin_msg}")

            continuous_days = 0
            if checkin_data:
                continuous_days = checkin_data.get('continue_checkin_days', 0) or 0

            monthly_exp = get_monthly_exp(cookie)

            final_msg = (
                f"什么值得买签到结果\n\n"
                f"👤 账号: 第{index}个账号 ({name})\n"
                f"⭐ 等级: VIP{level}\n"
                f"💰 金币: {gold}\n"
                f"🪙 碎银: {silver}\n"
                f"📊 本月经验: {monthly_exp}\n\n"
                f"🎯 签到状态: {signin_msg}"
                + (f"\n📅 连续签到: {continuous_days} 天" if continuous_days else "")
                + f"\n\n🕐 签到时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

            print(f"{'✅ 签到成功' if ok else '❌ 签到失败'}")
            return final_msg, ok

        finally:
            api.close()

    except requests.exceptions.Timeout:
        error_msg = f"❌ 账号{index}: 请求超时，网络连接可能有问题"
        print(error_msg)
        return error_msg, False
    except requests.exceptions.ConnectionError:
        error_msg = f"❌ 账号{index}: 网络连接错误，无法连接到服务器"
        print(error_msg)
        return error_msg, False
    except Exception as e:
        error_msg = f"❌ 账号{index}: 签到异常 - {str(e)}"
        print(error_msg)
        return error_msg, False


def main():
    print(f"==== 什么值得买签到开始 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")

    if random_signin:
        delay_seconds = random.randint(0, max_random_delay)
        if delay_seconds > 0:
            signin_time = datetime.now() + timedelta(seconds=delay_seconds)
            print(f"🎲 随机模式: 延迟 {format_time_remaining(delay_seconds)} 后开始")
            print(f"⏰ 预计开始时间: {signin_time.strftime('%H:%M:%S')}")
            wait_with_countdown(delay_seconds, "什么值得买签到")

    SMZDM_COOKIE_env = os.getenv("SMZDM_COOKIE")
    if not SMZDM_COOKIE_env:
        error_msg = "❌ 未找到SMZDM_COOKIE环境变量，请设置什么值得买Cookie"
        print(error_msg)
        notify("什么值得买签到失败", error_msg)
        return 1

    cookies = [c.strip() for c in SMZDM_COOKIE_env.replace('\r\n', '\n').split('\n')
               if c.strip()]
    print(f"📝 共发现 {len(cookies)} 个账号")

    success_count = 0
    total_count = len(cookies)
    detail_lines = []

    for i, cookie in enumerate(cookies):
        try:
            if i > 0 and random_signin:
                delay = random.uniform(10, 20)
                print(f"⏱️  随机等待 {delay:.1f} 秒后处理下一个账号...")
                time.sleep(delay)

            result_msg, is_success = smzdm_signin(cookie, i + 1)
            if is_success:
                success_count += 1

            detail_lines.append(('✅' if is_success else '❌') + f" 账号{i + 1}")
            notify(
                f"什么值得买账号{i + 1}签到{'成功' if is_success else '失败'}",
                result_msg
            )
        except Exception as e:
            error_msg = f"❌ 账号{i + 1}: 处理异常 - {str(e)}"
            print(error_msg)
            detail_lines.append(f"❌ 账号{i + 1}")
            notify(f"什么值得买账号{i + 1}签到失败", error_msg)

    if total_count > 1:
        summary_msg = (
            f"什么值得买签到汇总\n\n"
            f"📊 总计处理: {total_count}个账号\n"
            f"✅ 签到成功: {success_count}个账号\n"
            f"❌ 签到失败: {total_count - success_count}个账号\n"
            f"📈 成功率: {success_count / total_count * 100:.1f}%\n"
            + "\n".join(detail_lines)
            + f"\n🕐 完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        notify("什么值得买签到汇总", summary_msg)

    print(f"\n==== 什么值得买签到完成 - 成功{success_count}/{total_count} - "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")

    return 0 if success_count == total_count else 1


if __name__ == "__main__":
    sys.exit(main())
