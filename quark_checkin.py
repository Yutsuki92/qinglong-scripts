#!/usr/bin/python3
# -*- coding: utf-8 -*-
'''
new Env('夸克自动签到')
cron: 0 0 * * *

使用移动端接口完成每日自动签到。

环境变量：QUARK_COOKIE，多账户用换行分隔
格式一（推荐）: user=张三; url=https://drive-m.quark.cn/1/clouddrive/act/growth/reward?xxxx=xxxx&kps=abcdefg&sign=hijklmn&vcode=111111111;
格式二（旧版兼容）: user=张三; kps=abcdefg; sign=hijklmn; vcode=111111111;
user 字段是用户名，可随意填写，多账户方便区分。

抓包流程：
    【手机端】
    ①打开抓包，手机端访问抽奖页
    ②找到 url 为 https://drive-m.quark.cn/1/clouddrive/act/growth/reward 的请求信息
    ③复制整段 url（必须含 kps / sign / vcode），粘贴到环境变量 QUARK_COOKIE

可选环境变量：
    RANDOM_SIGNIN       延迟开关，默认 true；置 false 关闭全部等待（启动 + 账号间）
    MAX_RANDOM_DELAY    启动随机延迟上限秒数，默认 600
    QUARK_TIMEOUT       单请求超时秒数，默认 15

Author: BNDou
Source: https://github.com/Cp0204/quark-auto-save
'''
import os
import re
import sys
import time
import random
import logging

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


# ---------------- 日志 ----------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('Quark')


# 随机延迟配置
max_random_delay = int(os.getenv("MAX_RANDOM_DELAY", "600"))
random_signin = os.getenv("RANDOM_SIGNIN", "true").lower() == "true"

# ---------------- 配置 ----------------
TIMEOUT = int(os.getenv('QUARK_TIMEOUT', '15'))
BASE_PARAMS = {'pr': 'ucpro', 'fr': 'android'}
HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
}


def convert_bytes(b):
    """字节数转可读字符串"""
    try:
        b = float(b)
    except (TypeError, ValueError):
        return '0 B'
    units = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = 0
    while b >= 1024 and i < len(units) - 1:
        b /= 1024
        i += 1
    return f"{b:.2f} {units[i]}"


class Quark:
    """封装夸克的签到 / 领取签到奖励"""

    def __init__(self, user_data, index=0):
        self.param = user_data
        self.index = index
        self.name = user_data.get('user') or f'账号{index + 1}'
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ---------- 底层请求 ----------
    def _creds(self):
        """取出鉴权三件套，顺带做空值校验"""
        kps = (self.param.get('kps') or '').strip()
        sign = (self.param.get('sign') or '').strip()
        vcode = (self.param.get('vcode') or '').strip()
        return kps, sign, vcode

    def _params(self):
        kps, sign, vcode = self._creds()
        p = dict(BASE_PARAMS)
        p.update({'kps': kps, 'sign': sign, 'vcode': vcode})
        return p

    def _request(self, method, url, **kw):
        """统一请求：带超时、带重试、异常不外抛"""
        kw.setdefault('timeout', TIMEOUT)
        for attempt in range(3):
            try:
                resp = self.session.request(method, url, **kw)
                return resp.json()
            except requests.exceptions.Timeout:
                if attempt == 2:
                    logger.warning(f"    ⏱  请求超时: {url}")
                    return None
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    logger.warning(f"    🌐 网络异常: {e}")
                    return None
            except ValueError:
                logger.warning(f"    ⚠️  响应不是合法 JSON: {url}")
                return None
            time.sleep(1 + attempt)
        return None

    def _check_auth(self, data):
        """识别鉴权失败（参数过期），给出明确提示"""
        if not data:
            return
        code = data.get('code')
        if code in (31001, 31002, 31003, 40001) or \
                'kps' in str(data.get('message', '')).lower() or \
                '登录' in str(data.get('message', '')):
            logger.error(f"    🔑 鉴权失败，kps/sign/vcode 可能已过期，请重新抓包更新环境变量")

    # ---------- 业务接口 ----------
    def get_growth_info(self):
        """获取用户当前的签到信息，失败返回 False"""
        url = "https://drive-m.quark.cn/1/clouddrive/capacity/growth/info"
        data = self._request('GET', url, params=self._params())
        self._check_auth(data)
        if data and data.get("data"):
            return data["data"]
        return False

    def get_growth_sign(self):
        """执行签到，返回 (是否成功, 奖励字节数 或 错误消息)"""
        url = "https://drive-m.quark.cn/1/clouddrive/capacity/growth/sign"
        data = self._request('POST', url, params=self._params(),
                             json={"sign_cyclic": True})
        self._check_auth(data)
        if data and data.get("data"):
            return True, data["data"].get("sign_daily_reward", 0)
        msg = (data or {}).get("message", "未知错误")
        return False, msg

    def query_balance(self):
        """查询抽奖余额（保留能力，返回值可直接展示）"""
        url = "https://coral2.quark.cn/currency/v1/queryBalance"
        kps, _, _ = self._creds()
        data = self._request('GET', url, params={
            'moduleCode': '1f3563d38896438db994f118d4ff53cb',
            'kps': kps,
        })
        if data and data.get("data"):
            return data["data"].get("balance")
        return None

    # ---------- 主流程 ----------
    def do_sign(self):
        """执行签到，返回结果文本"""
        kps, sign, vcode = self._creds()
        if not (kps and sign and vcode):
            return (f"❌ {self.name}: kps/sign/vcode 缺失或不完整\n"
                    f"   请确认环境变量里含完整的 url 或 kps;sign;vcode\n")

        growth_info = self.get_growth_info()
        if not growth_info:
            return (f"❌ {self.name}: 获取成长信息失败\n"
                    f"   可能原因：kps/sign/vcode 已过期，或网络异常（详见上方日志）\n")

        cap_sign = growth_info.get('cap_sign') or {}
        cap_comp = growth_info.get('cap_composition') or {}

        log = (f" {'88VIP' if growth_info.get('88VIP') else '普通用户'} {self.name}\n"
               f"💾 网盘总容量：{convert_bytes(growth_info.get('total_capacity', 0))}，"
               f"签到累计容量：")

        if "sign_reward" in cap_comp:
            log += f"{convert_bytes(cap_comp['sign_reward'])}\n"
        else:
            log += "0 MB\n"

        progress = cap_sign.get('sign_progress', 0) or 0
        target = cap_sign.get('sign_target', 0) or 0

        if cap_sign.get("sign_daily"):
            log += (f"✅ 签到日志: 今日已签到+"
                    f"{convert_bytes(cap_sign.get('sign_daily_reward', 0))}，"
                    f"连签进度({progress}/{target})\n")
        else:
            ok, reward = self.get_growth_sign()
            if ok:
                log += (f"✅ 执行签到: 今日签到+{convert_bytes(reward)}，"
                        f"连签进度({progress + 1}/{target})\n")
            else:
                log += f"❌ 签到异常: {reward}\n"

        return log


def split_accounts(raw):
    """按真实语义切分多账户。

    难点：新版环境变量把整段 url 塞进 user 段，而 url 的 query 里天然含 '&'，
    所以 '&&' 既可能是「账号分隔符」，也可能是「url 里恰好出现的空参数」。

    可靠的判别依据：**url 段落里只有 url= 这一个 key，kps/sign/vcode 都藏在
    它的 query 里**。所以一行文本中，第一个 `user=` 到该账号 `url=...` 结束
    为止，属于同一个账号。基于此，采用如下策略：

      1. 先按换行切 —— 换行一定是账号分隔符。
      2. 对每一行：以「`user=` 出现的位置」为切点。每个 `user=` 开启一个新账号；
         若该行只出现一次 user=，则整行属于同一账号 —— 这样 url 里的 && 自然
         不会被误切。
      3. 若一行里没有任何 user=（兼容旧格式直接写 kps=;sign=;vcode=），
         退化为：按 `&&` 切，且把「不含 user= 且值里带 & 」的碎片并回上一段。
    """
    if not raw:
        return []

    accounts = []
    for line in raw.split('\n'):
        line = line.strip()
        if not line:
            continue

        if 'user=' in line:
            # 以每个 user= 为切点（保留切点本身）
            parts = re.split(r'(?=\buser\s*=)', line)
            accounts.extend(p.strip() for p in parts if p.strip())
        else:
            # 旧格式：可能用 && 分隔多个 kps=;sign=;vcode= 组合
            cur = ''
            for part in line.split('&&'):
                if re.match(r'^\s*kps\s*=', part) and cur.strip():
                    accounts.append(cur.strip())
                    cur = part
                else:
                    cur = f'{cur}&&{part}' if cur else part
            if cur.strip():
                accounts.append(cur.strip())

    return [a for a in accounts if a]


def parse_account(seg):
    """把一段 'k=v; k=v' 解析成 dict，兼容 url 内嵌参数"""
    user_data = {}
    for kv in re.split(r';\s*', seg):
        kv = kv.strip()
        if not kv or '=' not in kv:
            continue
        key, value = kv.split('=', 1)
        # 丢掉被账号分隔符带进来的尾部 '&&'
        value = value.strip().rstrip('&').strip()
        user_data[key.strip()] = value

    # url 中提取 kps / sign / vcode
    url = user_data.get('url', '')
    if url:
        qs = url.split('?', 1)[1] if '?' in url else ''
        for pair in qs.split('&'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                if k in ('kps', 'sign', 'vcode') and not user_data.get(k):
                    user_data[k] = v
    return user_data


def get_env():
    """读取并切分环境变量"""
    raw = os.getenv('QUARK_COOKIE', '').strip()
    if not raw:
        msg = '❌未添加QUARK_COOKIE变量'
        print(msg)
        notify('夸克自动签到', msg)
        sys.exit(0)
    return split_accounts(raw)


def main():
    accounts = get_env()
    logger.info(f"✅ 检测到共 {len(accounts)} 个夸克账号")
    if not accounts:
        return ''

    # 启动随机延迟
    if random_signin and max_random_delay > 0:
        delay_seconds = random.randint(0, max_random_delay)
        logger.info(f"🎲 随机延迟: {delay_seconds} 秒")
        time.sleep(delay_seconds)

    msg = ''
    for i, seg in enumerate(accounts):
        user_data = parse_account(seg)
        name = user_data.get('user') or f'账号{i + 1}'
        logger.info(f"🙍🏻‍♂️ 第{i + 1}个账号 {name}")
        try:
            log = Quark(user_data, i).do_sign()
        except Exception as e:
            log = f"❌ {name}: 执行异常 {e}\n"
            logger.error(f"第{i + 1}个账号执行异常: {e}", exc_info=True)
        msg += f"🙍🏻‍♂️ 第{i + 1}个账号\n{log}"
        print(log)

        # 多账户间随机延迟，降低风控概率
        if random_signin and i < len(accounts) - 1:
            delay = random.uniform(10, 20)
            logger.info(f"⏳ 随机等待 {delay:.1f} 秒...")
            time.sleep(delay)

    notify('夸克自动签到', msg)
    return msg.strip()


if __name__ == "__main__":
    logger.info("----------夸克网盘开始签到----------")
    main()
    logger.info("----------夸克网盘签到完毕----------")
