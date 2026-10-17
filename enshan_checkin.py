#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
恩山论坛自动签到

cron: 0 0 * * *
new Env('恩山论坛签到');

环境变量
  ENSHAN_COOKIE     论坛 Cookie，多账号用换行分隔
  PRIVACY_MODE      可选，默认 true（脱敏）；置 false 显示完整用户名
  RANDOM_SIGNIN     可选，默认 true，延迟开关；置 false 关闭全部等待（启动 + 账号间）
  MAX_RANDOM_DELAY  可选，默认 600，启动随机延迟上限（秒）
'''

import os
import re
import sys
import time
import random
import logging
from datetime import datetime

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

BASE_URL = 'https://www.right.com.cn/forum'
SIGN_PAGE_URL = f'{BASE_URL}/erling_qd-sign_in.html'
SIGN_API_URL = f'{BASE_URL}/plugin.php?id=erling_qd:action&action=sign'
FORUM_URL = f'{BASE_URL}/forum.php'

# 积分页存在多种参数形式，按序尝试
CREDIT_URLS = [
    f'{BASE_URL}/home.php?mod=spacecp&ac=credit',
    f'{BASE_URL}/home.php?mod=spacecp&ac=credit&showcredit=1',
    'https://www.right.com.cn/FORUM/home.php?mod=spacecp&ac=credit',
    'https://www.right.com.cn/FORUM/home.php?mod=spacecp&ac=credit&showcredit=1',
]

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Cache-Control': 'max-age=0',
}

PRIVACY_MODE = os.getenv('PRIVACY_MODE', 'true').lower() == 'true'

# 随机延迟配置
max_random_delay = int(os.getenv("MAX_RANDOM_DELAY", "600"))
random_signin = os.getenv("RANDOM_SIGNIN", "true").lower() == "true"

MSG_COOKIE_EXPIRED = 'Cookie 已失效，请重新登录恩山论坛后更新 ENSHAN_COOKIE'
MSG_WAF_FAILED = ('WAF 挑战解析失败，非 Cookie 问题；请检查站点是否改版，'
                  '或稍后重试（清关 Cookie 可能已过期）')
# 未登录访客的 uid，Discuz 用它标记游客
GUEST_UID = '0'

# 未登录时接口返回的文案特征
NEED_LOGIN_HINTS = ('请先登录', '请登录', 'not logged in', '您需要登录')


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
LOGGER = logging.getLogger('enshan')
LOGGER.setLevel(logging.INFO)
if not LOGGER.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    ))
    LOGGER.addHandler(_handler)
    LOGGER.propagate = False


def log(msg, level=logging.INFO):
    """带时间戳输出，青龙会捕获 stdout 作为任务日志。"""
    LOGGER.log(level, msg)


# ---------------- 通用工具 ----------------
def mask_username(username):
    """用户名脱敏。"""
    if not username:
        return username
    if not PRIVACY_MODE:
        return username
    if len(username) <= 2:
        return '*' * len(username)
    if len(username) <= 4:
        return username[0] + '*' * (len(username) - 2) + username[-1]
    return username[0] + '*' * 3 + username[-1]


def extract_first(text, patterns, default=None, flags=0):
    """按序尝试多个正则，返回首个命中的 group(1)（去空白）。"""
    for pattern in patterns:
        match = re.search(pattern, text or '', flags)
        if match:
            value = match.group(1)
            return value.strip() if isinstance(value, str) else value
    return default


def extract_number(text):
    """从文本中提取整数，失败返回 0。"""
    if not text:
        return 0
    digits = re.sub(r'[^\d]', '', str(text))
    try:
        return int(digits) if digits else 0
    except (ValueError, TypeError):
        return 0


def format_duration(seconds):
    """把秒数格式化为 x小时x分x秒；不足 1 分钟保留一位小数。"""
    if seconds <= 0:
        return '0秒'
    if seconds < 60:
        return f'{seconds:.1f}秒'
    hours, minutes = divmod(int(seconds), 3600)
    minutes, secs = divmod(minutes, 60)
    if hours:
        return f'{hours}小时{minutes}分{secs}秒'
    if minutes:
        return f'{minutes}分{secs}秒'
    return f'{secs}秒'


def fmt_clock(ts=None):
    """时间戳渲染为 YYYY-MM-DD HH:MM:SS。"""
    return datetime.fromtimestamp(ts if ts else time.time()).strftime('%Y-%m-%d %H:%M:%S')


def parse_cookies(cookie_str):
    """解析多账号 Cookie，多账号用换行分隔（兼容 &&），自动去重。"""
    if not cookie_str:
        return []
    cookies = []
    for line in cookie_str.strip().split('\n'):
        for part in line.split('&&'):
            part = part.strip()
            if part and part not in cookies:
                cookies.append(part)
    return cookies


def wait_with_countdown(seconds, task_name):
    """带倒计时的等待。"""
    if seconds <= 0:
        return
    log(f'{task_name} 需要等待 {format_duration(seconds)}')
    remaining = seconds
    while remaining > 0:
        if remaining <= 10 or remaining % 10 == 0:
            log(f'{task_name} 倒计时：{format_duration(remaining)}')
        step = 1 if remaining <= 10 else min(10, remaining)
        time.sleep(step)
        remaining -= step


# ---------------- WAF / Cookie 工具 ----------------
# 站点用 JSObfuscator 类混淆生成挑战脚本，变量名（如 oo/qo/wi/po）会随版本变化。
# 下面这套解析不写死变量名，而是先从脚本中动态识别角色，再据此拼装正则，
# 从而在站点改版（换变量名/换循环边界）时仍能还原 Cookie。

JS_IDENT = r'[A-Za-z_$][\w$]*'

# 候选变量名的出现顺序即角色线索：主数组 → 索引 → 解码密钥 → 输出
ROLE_KEYS = ('array', 'index', 'key', 'target')


def _js_unescape(text):
    """还原 JS 字符串里的 \\xNN / \\uNNNN 转义，便于正则匹配。"""
    if not text:
        return ''
    text = re.sub(r'\\x([0-9a-fA-F]{2})',
                  lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r'\\u([0-9a-fA-F]{4})',
                  lambda m: chr(int(m.group(1), 16)), text)
    return text


def _detect_roles(html):
    """动态识别挑战脚本中的变量角色。

    返回 {'array','index','key','target'} 的变量名，识别失败返回 None。
    识别依据（按强弱）：
      array  : 被赋值成十六进制/数字数组的那个变量
      index  : 参与 `do{...}while(--X>=N)` 递减循环的变量
      key    : 出现在 setTimeout("fn(N)") 里的那个数字所属函数
    """
    script = _js_unescape(html)

    roles = {}

    # array：形如 X = [0x12,0x34,...] 或 X=[12,34,...]
    # 用括号配对判定，避免嵌套数组被误截
    for am in re.finditer(rf'({JS_IDENT})\s*=\s*\[', script):
        start = script.find('[', am.start())
        depth, i, n, end = 0, start, len(script), -1
        while i < n:
            if script[i] == '[':
                depth += 1
            elif script[i] == ']':
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        if end < 0:
            continue
        raw = script[start + 1:end]
        # 必须是纯数字/十六进制列表（允许逗号空白），才算主数组
        if re.fullmatch(r'[\s,0-9a-fA-Fx]+', raw) and re.search(r'\d', raw):
            roles['array'] = am.group(1)
            roles['array_raw'] = raw
            break

    # index：形如 do{...;X--;}while(X>=N) 或 while(--X>=N) / while(X-->=N)
    m = re.search(rf'({JS_IDENT})\s*--\s*;\s*\}}\s*while\s*\(\s*{JS_IDENT}\s*>=?\s*\d+', script)
    if not m:
        m = re.search(rf'while\s*\(\s*--\s*({JS_IDENT})\s*>=?\s*\d+', script)
    if not m:
        m = re.search(rf'while\s*\(\s*({JS_IDENT})\s*--\s*>=?\s*\d+', script)
    if m:
        roles['index'] = m.group(1)

    # key / target：setTimeout("fn(N)") 里的函数名与数字
    m = re.search(r'setTimeout\s*\(\s*["\']\s*(\w+)\s*\(\s*(\d+)\s*\)', script)
    if m:
        roles['target'] = m.group(1)
        roles['key'] = m.group(2)          # 这里存的是数字，非变量名

    if 'array' not in roles:
        return None
    return roles


def _extract_oo(html, name=None):
    """提取混淆主数组。name 为 None 时自动识别。

    用方括号配对提取，避免嵌套数组（如 [[1,2],[3,4]]）被截断。
    """
    script = _js_unescape(html)
    if name:
        pattern = rf'{re.escape(name)}\s*=\s*\['
    else:
        pattern = rf'({JS_IDENT})\s*=\s*\['
    match = re.search(pattern, script)
    if not match:
        return None

    # 从 '[' 开始做方括号配对
    start = script.find('[', match.start())
    if start < 0:
        return None
    depth, i, n, end = 0, start, len(script), -1
    while i < n:
        c = script[i]
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                end = i
                break
        i += 1
    if end < 0:
        return None

    raw = script[start + 1:end]
    # 只取顶层数字/十六进制 token，忽略嵌套结构里的分隔
    tokens = re.findall(r'0x[0-9a-fA-F]+|\d+', raw)
    if not tokens:
        return None
    return [int(t, 16) if t.lower().startswith('0x') else int(t) for t in tokens]


def _extract_wi(html, roles=None):
    """提取解码密钥（setTimeout 里的数字）。"""
    script = _js_unescape(html)
    if roles and roles.get('key'):
        return int(roles['key'])
    # 限定在 setTimeout 调用内，避免误匹配任意 fn(数字)
    m = re.search(r'setTimeout\s*\(\s*["\']\s*\w+\s*\(\s*(\d+)\s*\)', script)
    if m:
        return int(m.group(1))
    # 退路：捕获 setTimeout("fn(N)" 附近的数字，仍限定在 setTimeout 之后
    m = re.search(r'setTimeout\s*\([^)]*?(\d{1,4})', script)
    return int(m.group(1)) if m else None


def _do_while_block(script, ie, marker):
    """切出含 marker 的那个 do{...}while(...) 块。

    返回 (block, end, head)：
      block = do{...}while(...) 整体
      end   = while 里的终止边界 N
      head  = do 之前一段，用于找 `X = N;` 初始化
    """
    m = re.search(marker, script)
    if not m:
        return None, None, None
    pos = m.start()

    # 向前找最近的独立 `do`（避免匹配到 document / doSomething 里的 do）
    do_pos = -1
    for dm in re.finditer(r'\bdo\b', script[:pos]):
        do_pos = dm.start()
    if do_pos < 0:
        return None, None, None

    while_pos = -1
    for wm in re.finditer(r'\bwhile\b', script[pos:]):
        while_pos = pos + wm.start()
        break
    if while_pos < 0:
        return None, None, None

    # while 括号结束位置，作为块尾
    close = script.find(')', while_pos)
    block = script[do_pos:close + 1 if close > 0 else while_pos + 60]

    m2 = re.search(rf'while\s*\(\s*(?:--\s*)?{ie}\s*(?:--\s*)?>=\s*(\d+)\s*\)',
                   script[while_pos:while_pos + 60])
    end = int(m2.group(1)) if m2 else None

    # head：do 之前一段，用于找 `X = N;` 初始化
    head = script[max(0, do_pos - 40):do_pos]
    return block, end, head


def _extract_loop1_params(html, roles=None):
    """循环1：取反 + 循环移位 + 减法。不再写死变量名与移位常量。"""
    script = _js_unescape(html)
    arr = roles.get('array') if roles else None
    idx = roles.get('index') if roles else None
    if not arr or not idx:
        return None
    ae, ie = re.escape(arr), re.escape(idx)

    # 取反特征定位 loop1 块：arr[X] = (-arr[X]) & 0xff
    marker = rf'{ae}\s*\[\s*{ie}\s*\]\s*=\s*\(\s*-\s*{ae}\s*\[\s*{ie}\s*\]\s*\)\s*&\s*0xff'
    block, end, head = _do_while_block(script, ie, marker)
    if block is None:
        return None

    # 起始值在 do 之前的 head 里
    m = re.search(rf'{ie}\s*=\s*(\d+)\s*;?\s*$', head)
    if not m:
        return None
    start = int(m.group(1))

    # 移位与减法：arr[X]=(((arr[X]>>R)|((arr[X]<<L)&0xff))-S)&0xff
    m = re.search(
        rf'{ae}\s*\[\s*{ie}\s*\]\s*=\s*\(?\s*\(?\s*\(?\s*{ae}\s*\[\s*{ie}\s*\]\s*>>\s*(\d+)\s*\)'
        r'\s*\|\s*\(\s*\(\s*'
        rf'{ae}\s*\[\s*{ie}\s*\]\s*<<\s*(\d+)\s*\)\s*&\s*0xff\s*\)\s*\)'
        r'\s*-\s*(\d+)\s*\)?\s*&\s*0xff',
        block,
    )
    if not m:
        return None

    return {
        'start': start,
        'shift_r': int(m.group(1)),
        'shift_l': int(m.group(2)),
        'sub': int(m.group(3)),
        'end': end if end is not None else 2,
    }


def _extract_loop2_start(html, roles=None):
    """循环2：相邻差分。arr[X] = (arr[X] - arr[X-1]) & 0xff"""
    script = _js_unescape(html)
    arr = roles.get('array') if roles else None
    idx = roles.get('index') if roles else None
    if not arr or not idx:
        return None
    ae, ie = re.escape(arr), re.escape(idx)

    marker = rf'{ae}\s*\[\s*{ie}\s*\]\s*=\s*\(\s*{ae}\s*\[\s*{ie}\s*\]\s*-\s*{ae}\s*\[\s*{ie}\s*-\s*1\s*\]'
    block, end, head = _do_while_block(script, ie, marker)
    if block is None:
        return None

    m = re.search(rf'{ie}\s*=\s*(\d+)\s*;?\s*$', head)
    if not m:
        return None
    return {
        'start': int(m.group(1)),
        'end': end if end is not None else 3,
    }


def _balanced_block(script, start):
    """从 start（指向 '{'）开始，按大括号配对提取整块内容，支持嵌套。"""
    depth, i, n = 0, start, len(script)
    begin = None
    while i < n:
        c = script[i]
        if c == '{':
            depth += 1
            if depth == 1:
                begin = i + 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return script[begin:i]
        i += 1
    return None


def _extract_loop3_params(html, roles=None):
    """循环3：加常量 + 循环左移。不再写死变量名。"""
    script = _js_unescape(html)
    arr = roles.get('array') if roles else None
    if not arr:
        return None
    ae = re.escape(arr)

    # 定位 for 循环的 {，并用大括号配对提取（避免嵌套 if 截断）
    m = re.search(r'for\s*\([^)]*\)\s*\{', script)
    if not m:
        return None
    block = _balanced_block(script, m.end() - 1)
    if not block:
        return None

    # 上界：X > N) break
    upper_m = re.search(rf'{JS_IDENT}\s*>\s*(\d+)\s*\)\s*break', block)
    if not upper_m:
        return None

    # 赋值表达式：arr[X] = (...)  注意行尾可能无分号
    assign_m = re.search(rf'{ae}\s*\[\s*{JS_IDENT}\s*\]\s*=\s*(.+?)(?:;|\}}|$)', block, re.S)
    if not assign_m:
        return None
    expr = assign_m.group(1)

    add_nums = re.findall(r'\+\s*(\d+)', expr)
    shifts = [int(g) for g in re.findall(r'<<\s*(\d+)', expr)]
    if len(add_nums) < 2 or not shifts:
        return None

    return {
        'upper': int(upper_m.group(1)),
        'add1': int(add_nums[0]),
        'add2': int(add_nums[1]),
        'rot_l': shifts[0],
    }


def _extract_mod_skip(html, roles=None):
    """步长：index % N"""
    script = _js_unescape(html)
    idx = roles.get('index') if roles else None
    if idx:
        m = re.search(rf'{re.escape(idx)}\s*%\s*(\d+)', script)
        if m:
            return int(m.group(1))
    m = re.search(rf'{JS_IDENT}\s*%\s*(\d+)', script)
    return int(m.group(1)) if m else 7


def _extract_challenge_params(html):
    """一次性识别全部挑战参数，返回 (params, roles) 或 (None, None)。"""
    roles = _detect_roles(html)
    if not roles:
        return None, None

    oo = _extract_oo(html, roles.get('array'))
    wi = _extract_wi(html, roles)
    loop1 = _extract_loop1_params(html, roles)
    loop2 = _extract_loop2_start(html, roles)
    loop3 = _extract_loop3_params(html, roles)
    if not oo or wi is None or not loop1 or not loop2 or not loop3:
        return None, roles

    params = {
        'loop1_start': loop1['start'],
        'loop1_end': loop1.get('end', 2),
        'loop2_start': loop2['start'],
        'loop2_end': loop2.get('end', 3),
        'loop3_upper': loop3['upper'],
        'shift_r': loop1['shift_r'],
        'shift_l': loop1['shift_l'],
        'sub': loop1['sub'],
        'add1': loop3['add1'],
        'add2': loop3['add2'],
        'rot_l': loop3['rot_l'],
        'mod_skip': _extract_mod_skip(html, roles),
    }
    return (oo, wi, params), roles


def _rotl8(x, r):
    x &= 0xFF
    r &= 7
    return ((x << r) & 0xFF) | (x >> (8 - r))


def _rotr8(x, r):
    x &= 0xFF
    r &= 7
    return (x >> r) | ((x << (8 - r)) & 0xFF)


def _decode_po(oo_hex, wi, params):
    """还原 WAF 挑战脚本，解出 document.cookie 明文。

    循环边界（loop1_end/loop2_end）由脚本动态识别，不再写死 2/3。
    """
    oo = [b & 0xFF for b in oo_hex]
    if len(oo) < 6:
        return ''

    last = len(oo) - 1

    # 循环1：取反 → 循环移位 → 减常量
    end1 = params.get('loop1_end', 2)
    qo = min(params['loop1_start'], last - 1)
    while True:
        oo[qo] = (-oo[qo]) & 0xFF
        if params['shift_r'] + params['shift_l'] == 8:
            # 右移 r + 左移 (8-r) 等价于纯右旋
            oo[qo] = (_rotr8(oo[qo], params['shift_r']) - params['sub']) & 0xFF
        else:
            oo[qo] = (((oo[qo] >> params['shift_r']) | ((oo[qo] << params['shift_l']) & 0xFF))
                      - params['sub']) & 0xFF
        qo -= 1
        if qo < end1:
            break

    # 循环2：相邻差分
    end2 = params.get('loop2_end', 3)
    qo = min(params['loop2_start'], last - 2)
    while True:
        oo[qo] = (oo[qo] - oo[qo - 1]) & 0xFF
        qo -= 1
        if qo < end2:
            break

    # 循环3：加常量 → 循环左移
    for qo in range(1, min(params['loop3_upper'], last - 1) + 1):
        x = (oo[qo] + params['add1']) & 0xFF
        x = (x + params['add2']) & 0xFF
        oo[qo] = _rotl8(x, params['rot_l'])

    return ''.join(
        chr((oo[qo] ^ (wi & 0xFF)) & 0xFF)
        for qo in range(1, last) if qo % params['mod_skip'] != 0
    )


def _extract_formhash(html):
    """提取 formhash，兼容单/双引号、属性顺序不定、name/value 分行。"""
    return extract_first(
        html,
        patterns=[
            # name 在前、value 在后（引号不限，允许跨行与多余属性）
            r'''name\s*=\s*["']formhash["'][^>]*?value\s*=\s*["']([0-9a-fA-F]+)["']''',
            # value 在前、name 在后
            r'''value\s*=\s*["']([0-9a-fA-F]+)["'][^>]*?name\s*=\s*["']formhash["']''',
            # 退而求其次：从登出链接里取
            r'member\.php\?mod=logging(?:&amp;|&)action=logout(?:&amp;|&)formhash=([0-9a-fA-F]+)',
        ],
        default=None,
        flags=re.S,
    )


def _extract_uid(html):
    """从页面提取当前登录 uid；游客为 0，取不到返回 None。"""
    return extract_first(
        html,
        patterns=[
            r"discuz_uid\s*=\s*'(\d+)'",
            r'discuz_uid\s*=\s*"(\d+)"',
        ],
        default=None,
        flags=re.S,
    )


def _upsert_cookie(base_cookies, new_kv):
    """把 "k=v" 合并进 Cookie 串，同键覆盖。"""
    if not new_kv or '=' not in new_kv:
        return base_cookies
    new_key, new_value = (s.strip() for s in new_kv.split('=', 1))

    parts, replaced = [], False
    for raw in base_cookies.split(';'):
        part = raw.strip()
        if not part or '=' not in part:
            continue
        key, value = part.split('=', 1)
        key = key.strip()
        if key == new_key:
            parts.append(f'{new_key}={new_value}')
            replaced = True
        else:
            parts.append(f'{key}={value.strip()}')
    if not replaced:
        parts.append(f'{new_key}={new_value}')
    return '; '.join(parts)


# ---------------- 签到主体 ----------------
class EnShanSigner:
    name = '恩山论坛'

    def __init__(self, cookie, index=1):
        self.cookie = cookie
        self.index = index
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.headers['Cookie'] = cookie

        self.user_name = None
        self.user_group = None
        self.uid = None
        self.formhash = None
        self.credit_before = None
        self.credit_after = None
        self.contribution = None
        self.cookie_valid = None      # None=未验证 / True / False
        self.cookie_note = ''         # 校验结论说明
        self.waf_failed = False       # 失败是否源于 WAF 挑战环节
        self.signed_at = None         # 确认签到成功的时刻（失败/未知时为 None）
        self.sign_state = None        # success / already / unknown / fail
        self.credit_gain = None       # 签到前后恩山币差值，失败时为 None

    # 日志带账号序号，多账号时不混淆
    def _log(self, msg, level=logging.INFO):
        log(f'[{self.index}] {msg}', level)

    def _sync_cookie(self):
        self.session.headers['Cookie'] = self.cookie

    def _merge_response_cookies(self, response):
        for name, value in response.cookies.items():
            self.cookie = _upsert_cookie(self.cookie, f'{name}={value}')
        self._sync_cookie()

    def _page_headers(self, referer=None):
        return {
            'User-Agent': UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-User': '?1',
            'Sec-Fetch-Dest': 'document',
            'Referer': referer or SIGN_PAGE_URL,
            'Cookie': self.cookie,
        }

    def _refresh_clearance_cookie(self):
        """过 WAF 拿 formhash；站点下发新 Cookie 时自动合并。

        返回 (ok, msg, waf_failed)：
          waf_failed=True 表示失败发生在 WAF 挑战环节，
          属站点侧问题，不能据此判定 Cookie 失效。
        """
        try:
            response = self.session.get(
                SIGN_PAGE_URL, headers=self._page_headers(),
                timeout=30, allow_redirects=True,
            )
        except Exception as e:
            return False, f'获取签到页失败：{e}', False

        self._merge_response_cookies(response)

        # 用结构特征判断是否为 WAF 挑战页，而非硬编码变量名 'oo'
        parsed, roles = _extract_challenge_params(response.text)
        if not parsed:
            # 不是挑战页（或挑战参数不全）→ 当作普通页面直接找 formhash
            formhash = _extract_formhash(response.text)
            if formhash:
                self.formhash = formhash
                if roles:
                    # 识别到了角色但参数不全，说明模板又变了，留痕便于排查
                    return True, f'已刷新签到参数（WAF 模板变更，角色={sorted(roles)}）', False
                return True, '已刷新签到参数', False
            if roles:
                return False, 'WAF 挑战参数提取失败，站点可能已改版', True
            return False, '签到页未提取到 formhash', False

        oo, wi, params = parsed
        cookie_kv = extract_first(
            _decode_po(oo, wi, params),
            patterns=[r"document\.cookie=['\"]([^'\"]+)['\"]"],
            default=None,
        )
        if not cookie_kv:
            return False, 'WAF 解码后未找到 Cookie', True
        cookie_kv = cookie_kv.strip().split(';', 1)[0].strip()

        self.cookie = _upsert_cookie(self.cookie, cookie_kv)
        self._sync_cookie()

        try:
            follow = self.session.get(
                SIGN_PAGE_URL, headers=self._page_headers(),
                timeout=30, allow_redirects=True,
            )
        except Exception as e:
            return False, f'WAF 通过后重试签到页失败：{e}', True

        self._merge_response_cookies(follow)
        formhash = _extract_formhash(follow.text)
        if formhash:
            self.formhash = formhash
            return True, '已刷新 WAF Cookie 和 formhash', False
        return False, 'WAF 通过后未提取到 formhash', True

    def login(self):
        """获取 formhash 与 uid，并判定 Cookie 是否有效。

        判定依据按可靠性排序（强证据优先，避免误判）：
          1. 页面明确返回游客 uid=0        → Cookie 失效（硬证据）
          2. 拿不到任何页面（网络/状态码）  → 状态未知，不下失效结论
          3. WAF 环节失败但页面可读         → 站点侧问题，不判定 Cookie 失效
          4. 取到 formhash                  → 登录态正常
          5. 其余（页面可读但无 formhash）  → Cookie 失效
        """
        self._log('正在验证登录态...')
        ok, msg, waf_failed = self._refresh_clearance_cookie()
        self.waf_failed = waf_failed
        html = ''
        fallback_reason = msg if (not ok or not self.formhash) else None

        # 原因本身就是「没取到 formhash」时，日志里再复述一遍是冗余的，
        # 这种情况只保留结论；只有别的原因（WAF 失败等）才值得附带说明。
        redundant = fallback_reason is not None and 'formhash' in fallback_reason

        def _reason_suffix():
            return '' if redundant else f'（{fallback_reason}）'

        if fallback_reason:
            # 先不急着报警：回退 forum 页能拿到 formhash 就属正常流程，
            # 只有回退也失败时才值得 WARNING。日志延后到结果已知再打。
            try:
                response = self.session.get(FORUM_URL, timeout=20)
            except Exception as e:
                self._log(f'签到页失败{_reason_suffix()}，forum 页回退也异常：{e}',
                          logging.WARNING)
                self.cookie_valid = None
                return False, f'网络异常：{e}'

            if response.status_code != 200:
                self._log(f'签到页失败{_reason_suffix()}，forum 页返回状态码 '
                          f'{response.status_code}', logging.WARNING)
                self.cookie_valid = None
                return False, f'论坛返回状态码 {response.status_code}'

            self._merge_response_cookies(response)
            self.formhash = _extract_formhash(response.text)
            html = response.text

            # 回退结果已知，此时才决定日志级别
            if self.formhash:
                self._log(f'签到页未取到 formhash{_reason_suffix()}，'
                          f'已从 forum 页回退：{self.formhash}')
            else:
                self._log(f'签到页未取到 formhash{_reason_suffix()}，'
                          f'forum 页回退后仍未取到', logging.WARNING)
        else:
            # 签到页已通过，再用论坛页确认登录态
            try:
                html = self.session.get(FORUM_URL, timeout=20).text
            except Exception:
                html = ''

        self.uid = _extract_uid(html) if html else self.uid

        # 游客 uid 是唯一硬证据，优先判定
        if self.uid == GUEST_UID:
            self.cookie_valid = False
            self.cookie_note = MSG_COOKIE_EXPIRED
            return False, self.cookie_note

        # 页面可读但取不到 uid/formhash：WAF 侧失败时不下失效结论
        if not self.formhash:
            if waf_failed:
                self.cookie_valid = None
                self.cookie_note = MSG_WAF_FAILED
                return False, self.cookie_note
            self.cookie_valid = False
            self.cookie_note = MSG_COOKIE_EXPIRED
            return False, self.cookie_note

        self.cookie_valid = True
        self.cookie_note = '登录态正常'
        self._log(f'formhash 获取成功：{self.formhash}')
        if self.uid:
            self._log(f'uid 获取成功：{self.uid}')
        else:
            self.cookie_note = '登录态正常（未取到 uid）'
            if waf_failed:
                self.cookie_note += '，WAF 环节异常'
        return True, self.cookie_note

    def fetch_user_info(self, is_after=False):
        """抓取用户信息与积分，多候选 URL + 临时错误重试。"""
        stage = '签到后' if is_after else '签到前'
        self._log(f'正在获取{stage}用户信息...')
        time.sleep(random.uniform(2, 5))

        response, last_status = None, None
        for url in CREDIT_URLS:
            for attempt in range(1, 4):
                try:
                    resp = self.session.get(
                        url, headers={**HEADERS, 'Referer': FORUM_URL},
                        timeout=20, allow_redirects=True,
                    )
                except Exception as e:
                    last_status = f'网络异常({e})'
                    time.sleep(1.5 * attempt)
                    continue

                last_status = resp.status_code
                if resp.status_code == 200 and resp.text:
                    response = resp
                    break
                # 429/521/5xx 属临时性错误，退避重试
                if resp.status_code in (429, 521) or 500 <= resp.status_code < 600:
                    time.sleep(1.5 * attempt + random.uniform(0, 0.8))
                    continue
                break
            if response is not None:
                break

        if response is None:
            return False, f'获取用户信息失败，状态码：{last_status}'

        # 积分解析：失败返回 None（而非 '0'），避免「273 → 0」的假变化
        credit = extract_first(
            response.text,
            patterns=[
                r'恩山币\s*[:：]\s*</em>\s*([^<&\s]+)',
                r'恩山币\s*</em>\s*[:：]\s*(?:<[^>]+>\s*)*([^<&\s]+)',
                r'恩山币\s*[:：]\s*([^<\s]+)\s*币',
                r'恩山币\s*[:：]\s*(?:<[^>]+>\s*)*([^<&\s]+)',
            ],
            default=None, flags=re.S,
        )
        point = extract_first(
            response.text,
            patterns=[
                r'积分\s*[:：]\s*</em>\s*([^<&\s]+)',
                r'积分\s*</em>\s*[:：]\s*(?:<[^>]+>\s*)*([^<&\s]+)',
                r'<em>\s*积分\s*[:：]\s*</em>\s*([^<\s]+)',
                r'积分\s*[:：]\s*(?:<[^>]+>\s*)*([^<&\s]+)',
            ],
            default=None, flags=re.S,
        )

        if credit is None:
            # 页面结构变化或未登录，明确告警而不是伪造 0
            return False, '用户信息页未解析出恩山币（页面结构可能已变化）'

        if is_after:
            self.credit_after = credit
            self._log(f'签到后 - 恩山币：{credit}，积分：{point if point is not None else "未知"}')
        else:
            self.credit_before = credit
            self._log(f'签到前 - 恩山币：{credit}，积分：{point}')

            self.user_name = extract_first(
                response.text,
                patterns=[
                    r'访问我的空间">([^<]+)</a>',
                    r'class="vwmy"[^>]*>([^<]+)</a>',
                    r'欢迎您回来\s*,\s*([^<\n]+)',
                    r'用户名[：:]\s*([^<\n]+)',
                ],
                default=None, flags=re.S,
            )
            self.user_group = extract_first(
                response.text,
                patterns=[
                    r'用户组\s*[:：]\s*([^<\n]+)</',
                    r'用户组\s*[:：]\s*([^<\n]+)',
                ],
                default='未知等级', flags=re.S,
            )
            self.contribution = extract_first(
                response.text,
                patterns=[
                    r'贡献\s*[:：]\s*</em>\s*([^<\s]+)\s*分',
                    r'贡献\s*[:：]\s*(\d+)',
                ],
                default='0', flags=re.S,
            )
            if self.user_name:
                self._log(f'用户：{mask_username(self.user_name)}  等级：{self.user_group}')

        return True, '用户信息获取成功'

    def sign(self):
        """提交签到请求。"""
        self._log('正在执行签到...')
        if not self.formhash:
            ok, msg = self.login()
            if not ok:
                return False, msg, 'fail'

        # 签到前再刷新一次，降低 WAF Cookie 过期导致的失败。
        # 这只是「顺手刷新」，formhash 已在 login 阶段拿到，失败不影响主流程。
        _ok, _refresh_msg, _waf = self._refresh_clearance_cookie()
        if _ok:
            self._log('签到前已刷新签到参数')
        elif _waf:
            # 刷新阶段就被 WAF 拦下：记下来，供失败归因使用
            self.waf_failed = True

        headers = {
            'User-Agent': UA,
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': 'https://www.right.com.cn',
            'Referer': SIGN_PAGE_URL,
            'Cookie': self.cookie,
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
        }

        try:
            response = self.session.post(
                SIGN_API_URL, headers=headers,
                data={'formhash': self.formhash}, timeout=30,
            )
        except Exception as e:
            return False, f'签到请求异常：{e}'
        self._log(f'签到响应状态码：{response.status_code}')

        try:
            result = response.json()
            if not isinstance(result, dict):
                return False, '签到请求已提交（接口未返回可识别结果，结果未知）', 'unknown'
        except ValueError:
            # 非 JSON（常见于 WAF 拦截页或站点维护页），截断后再判断
            text = (response.text or '').strip()
            if response.status_code != 200:
                return False, f'签到接口返回状态码 {response.status_code}', 'fail'
            if any(hint in text for hint in NEED_LOGIN_HINTS):
                self.cookie_valid = False
                self.cookie_note = MSG_COOKIE_EXPIRED
                return False, MSG_COOKIE_EXPIRED, 'fail'
            # 既没拿到 JSON 也没有明确失败信号：标为「结果未知」，
            # 不能当成签到成功，留给积分复核判定。
            self._log(f'签到接口返回非 JSON（前 120 字）：{text[:120]}', logging.WARNING)
            return False, '签到请求已提交（接口未返回 JSON，结果未知）', 'unknown'

        message = str(result.get('message', '')).strip()
        status = result.get('status')

        # 接口明确要求登录 → Cookie 一定失效，回写状态供上层提醒
        if any(hint in message for hint in NEED_LOGIN_HINTS):
            self.cookie_valid = False
            self.cookie_note = MSG_COOKIE_EXPIRED
            return False, MSG_COOKIE_EXPIRED, 'fail'

        if '已签到' in message or '已经签到' in message:
            return True, message, 'already'
        if result.get('success') is True or status in (1, '1', 'success', True):
            return True, message or '签到成功', 'success'
        if '成功' in message:
            return True, message, 'success'
        if message:
            return False, f'签到失败：{message}', 'fail'
        return False, '签到请求已提交（接口未返回明确结果，结果未知）', 'unknown'

    def run(self):
        """单账号完整流程，返回 (message, success, already)。"""
        if not self.cookie.strip():
            self.sign_state = 'fail'
            return 'Cookie 为空', False, False

        ok, msg = self.login()
        if not ok:
            self.sign_state = 'fail'
            return msg, False, False

        # 登录成功说明 WAF 已通过（含回退路径），不再把它算作失败归因
        self.waf_failed = False

        self.fetch_user_info(is_after=False)
        time.sleep(random.uniform(2, 5))

        success, sign_msg, state = self.sign()
        # 只有确认成功/已签到时才记录「签到时间」；
        # 失败或结果未知时留空，展示端回落为「完成时间」。
        if state in ('success', 'already'):
            self.signed_at = time.time()
        already = (state == 'already')
        self.sign_state = state

        time.sleep(random.uniform(2, 4))
        after_ok, _ = self.fetch_user_info(is_after=True)

        # 以积分变化复核签到结果（仅在拿到前后积分时生效）
        if after_ok and self.credit_before and self.credit_after:
            before_n = extract_number(self.credit_before)
            after_n = extract_number(self.credit_after)
            self.credit_gain = after_n - before_n
            self._log(f'恩山币变化：{before_n} → {after_n}（{self.credit_gain:+d}）')
            if self.credit_gain > 0:
                # 有正向增量是硬证据，可把「未知」升级为「成功」
                success, state = True, 'success'
                prefix = '签到成功（此前已签到）' if already else '签到成功'
                sign_msg = f'{prefix}，获得 {self.credit_gain} 恩山币'
            elif self.credit_gain == 0 and state == 'success' and (
                    '已签到' in sign_msg or '已经签到' in sign_msg):
                # 只有接口明确说过「已签到」才降级；否则保持成功，
                # 避免奖励延迟到账被误判成「今日已签到」。
                already, state = True, 'already'
                sign_msg = '今日已签到（积分无变化）'
            # state == 'unknown' 时积分无变化不足以证明已签到，保持未知
            self.sign_state = state

        return sign_msg, success, already


# ---------------- 结果渲染 ----------------
def count_results(results):
    """统计口径，返回 (total, success, already, failed, unknown)。

    完全按 sign_state 分档，避免「unknown 同时被算进 success」
    导致「本次新签」虚增、失败数变负。
    success 含 already，故「本次新签 = success - already」。
    """
    total = len(results)
    success = sum(1 for r in results if r.get('state') in ('success', 'already'))
    already = sum(1 for r in results if r.get('state') == 'already')
    unknown = sum(1 for r in results if r.get('state') == 'unknown')
    failed = total - success - unknown
    return total, success, already, failed, unknown


def cookie_line(r):
    """Cookie 校验行：有效 / 失效 / 站点异常 / 未完成校验。"""
    valid = r.get('cookie_valid')
    if valid is True:
        return '登录态正常'
    if valid is False:
        return '已失效'
    if r.get('waf_failed'):
        return '站点 WAF 异常（Cookie 未必失效）'
    return '未完成校验'


def result_lines(r, indent=''):
    """单个账号的展示行，日志与通知共用。"""
    # WAF 异常属站点侧问题，用警示符号区别于真正的签到失败；
    # 结果未知同样用警示符号，不能显示成 ✅。
    state = r.get('state')
    if state == 'unknown':
        head = f"{indent}⚠️ [{r['tag']}] {r['message']}"
    elif r['already']:
        head = f"{indent}📅 [{r['tag']}] {r['message']}"
    elif r['success']:
        head = f"{indent}✅ [{r['tag']}] {r['message']}"
    elif r.get('waf_failed'):
        head = f"{indent}⚠️ [{r['tag']}] {r['message']}"
    else:
        head = f"{indent}❌ [{r['tag']}] {r['message']}"
    lines = [head]
    gain = r.get('credit_gain')
    before, after = r.get('credit_before'), r.get('credit_after')
    bal = f"（余额 {after or before}）" if (before or after) else ""
    if gain is None:
        lines.append(f"{indent}    🎁 今日获得：未知{bal or '（未取到积分）'}")
    elif gain > 0:
        lines.append(f"{indent}    🎁 今日获得：+{gain} 恩山币{bal}")
    elif r.get('already'):
        lines.append(f"{indent}    🎁 今日获得：无新增（今日已签到）{bal}")
    elif r.get('state') == 'unknown':
        lines.append(f"{indent}    🎁 今日获得：未知（签到结果未确认）{bal}")
    elif r['success']:
        lines.append(f"{indent}    🎁 今日获得：无新增（奖励可能延迟到账）{bal}")
    else:
        lines.append(f"{indent}    🎁 今日获得：无（签到未成功）{bal}")

    when = r.get('signed_at') or r.get('finished_at')
    if when:
        label = '🕐 签到时间' if r.get('signed_at') else '🕐 完成时间'
        elapsed = r.get('elapsed')
        suffix = f"（耗时 {format_duration(elapsed)}）" if elapsed is not None else ""
        lines.append(f"{indent}    {label}：{fmt_clock(when)}{suffix}")

    lines.append(f"{indent}    🔑 Cookie：{cookie_line(r)}")
    return lines


def build_notification(results, counts, warnings=None, started_at=None):
    """构造通知标题与正文。"""
    total, success, already, failed, unknown = counts
    waf_only = [r for r in results if not r['success'] and r.get('waf_failed')]

    if failed == 0 and unknown == 0:
        title = '恩山论坛签到成功'
    elif failed == 0 and unknown > 0:
        # 没有失败账号，只是部分账号结果不确定
        title = '恩山论坛签到完成（部分结果未知）'
    elif success == 0 and unknown == 0 and len(waf_only) == failed:
        # 全部失败都源于 WAF，属站点问题，不误导为账号/Cookie 故障
        title = '恩山论坛签到受阻（站点 WAF 异常）'
    elif success == 0 and unknown == 0:
        title = '恩山论坛签到失败'
    elif success == 0:
        title = '恩山论坛签到结果未知'
    else:
        title = '恩山论坛签到部分失败'

    lines = [
        f'📊 {total} 个账号｜本次新签 {success - already}｜'
        f'今日已签 {already}｜失败 {failed}'
        + (f'｜未知 {unknown}' if unknown else '')
    ]
    gains = [r.get('credit_gain') for r in results if (r.get('credit_gain') or 0) > 0]
    if gains:
        lines.append(f'🎁 合计获得：+{sum(gains)} 恩山币')
    if started_at:
        lines.append(f'⏱️ 耗时：{format_duration(time.time() - started_at)}')
    lines += ['', '详细结果：']
    for r in results:
        lines.extend(result_lines(r))

    lines += ['', '🔑 Cookie 无到期字段，失效后需重新抓取']

    if warnings:
        lines += ['', '提醒：'] + [f'  · {w}' for w in warnings]

    content = '\n'.join(lines)
    if len(content) > 1800:
        content = content[:1800] + '\n…（内容过长已截断）'
    return title, content


def print_summary(results):
    """打印执行结果统计。"""
    total, success, already, failed, unknown = count_results(results)
    log('=' * 50)
    log('执行结果统计')
    log('=' * 50)
    log(f'总账号数：{total}')
    log(f'本次新签：{success - already}')
    log(f'今日已签：{already}')
    log(f'签到失败：{failed}')
    if unknown:
        log(f'结果未知：{unknown}')
    log('详细结果：')
    for r in results:
        for line in result_lines(r, indent='  '):
            log(line)
    log('=' * 50)
    return total, success, already, failed, unknown


def main():
    if not HAVE_REQUESTS:
        log('缺少 requests 库，请在青龙依赖管理安装', logging.ERROR)
        sys.exit(1)

    start = datetime.now()
    log('=' * 50)
    log('恩山论坛自动签到开始')
    log('=' * 50)

    raw_cookie = os.environ.get('ENSHAN_COOKIE', '').strip()
    if not raw_cookie:
        msg = '未找到 ENSHAN_COOKIE 环境变量，请在青龙面板添加后重试'
        log(f'❌ {msg}', logging.ERROR)
        notify('恩山论坛签到异常', msg)
        sys.exit(2)

    cookies = parse_cookies(raw_cookie)
    if not cookies:
        msg = 'Cookie 解析失败，请检查 ENSHAN_COOKIE 格式（多账号用换行分隔）'
        log(f'❌ {msg}', logging.ERROR)
        notify('恩山论坛签到异常', msg)
        sys.exit(2)

    log(f'共发现 {len(cookies)} 个账号')

    if random_signin and max_random_delay > 0:
        wait_with_countdown(random.randint(0, max_random_delay), '恩山论坛签到')

    results, warnings = [], []
    for idx, cookie in enumerate(cookies, 1):
        tag = f'账号{idx}'
        if idx > 1 and random_signin:
            delay = random.uniform(10, 20)
            log(f'等待 {delay:.1f} 秒后处理下一个账号')
            time.sleep(delay)

        signer = EnShanSigner(cookie, idx)
        t0 = time.time()
        try:
            message, success, already = signer.run()
        except Exception as e:
            message, success, already = f'执行异常：{type(e).__name__}: {e}', False, False
            # 异常时状态可能已被 run() 写成 success，必须覆盖，
            # 否则统计会按 state 把这个账号算成成功。
            signer.sign_state = 'fail'
            signer.signed_at = None
        elapsed = time.time() - t0

        if signer.user_name:
            tag = f'账号{idx}({mask_username(signer.user_name)})'
        if signer.cookie_valid is False:
            warnings.append(f'{tag} {MSG_COOKIE_EXPIRED}')
        elif signer.waf_failed:
            warnings.append(f'{tag} {MSG_WAF_FAILED}')

        results.append({
            'tag': tag,
            'success': success,
            'already': already,
            'state': signer.sign_state,
            'message': message,
            'user_name': signer.user_name,
            'user_group': signer.user_group,
            'credit_before': signer.credit_before,
            'credit_after': signer.credit_after,
            'credit_gain': signer.credit_gain,
            'cookie_valid': signer.cookie_valid,
            'waf_failed': signer.waf_failed,
            'signed_at': signer.signed_at,
            'finished_at': time.time(),
            'elapsed': elapsed,
        })

    counts = print_summary(results)
    notify(*build_notification(results, counts, warnings, started_at=start.timestamp()))

    log(f'任务完成，耗时 {format_duration((datetime.now() - start).total_seconds())}')
    # 结果未知也要非零退出：多半是站点返回结构变了，需要人看一眼
    if counts[3] > 0 or counts[4] > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
