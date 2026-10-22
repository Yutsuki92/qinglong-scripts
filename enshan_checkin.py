#!/usr/bin/env python3
"""cron: 0 0 * * *
new Env('恩山论坛签到')

恩山论坛每日签到，多账号。依赖 requests、ql_common.py（需同目录）。

环境变量
  ENSHAN_COOKIE     论坛 Cookie，多账号换行分隔
  PRIVACY_MODE      默认 true 脱敏，false 显示完整用户名
  RANDOM_SIGNIN     默认 true，false 关闭全部延迟
  MAX_RANDOM_DELAY  默认 300，启动延迟上限（秒）
"""

import re
import sys
import time
import random
import logging

try:
    from ql_common import (  # noqa: E402
        EXIT_CONFIG, account_delay, account_header, banner, count_results,
        env_bool, env_str, fmt_elapsed, get_logger, notify_config_error,
        notify_result,
        print_summary, report_duplicates, require_dep, result_time_line,
        split_accounts, start_delay,
    )
except ImportError:  # 公共层没上传/没被订阅同步：给结论，不抛 Traceback
    print("❌ 缺少公共层 ql_common.py：它必须与本脚本放在同一目录")
    print("🔧 青龙「订阅」会同步整个仓库；手动上传时请把它一起传上去")
    sys.exit(2)

PLATFORM = "恩山论坛签到"
log = get_logger("enshan_checkin")

try:
    import requests
except ImportError:  # 缺依赖不在这里崩，交由 main 的 require_dep 出结论
    requests = None

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

PRIVACY_MODE = env_bool('PRIVACY_MODE', True)


MSG_COOKIE_EXPIRED = 'Cookie 已失效，请重新登录恩山论坛后更新 ENSHAN_COOKIE'
MSG_WAF_FAILED = ('WAF 挑战解析失败，非 Cookie 问题；请检查站点是否改版，'
                  '或稍后重试（清关 Cookie 可能已过期）')
# 未登录访客的 uid，Discuz 用它标记游客
GUEST_UID = '0'

# 未登录时接口返回的文案特征
NEED_LOGIN_HINTS = ('请先登录', '请登录', 'not logged in', '您需要登录')


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


JS_IDENT = r'[A-Za-z_$][\w$]*'

# 候选变量名的出现顺序即角色线索：主数组 → 索引 → 解码密钥 → 输出


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
    """动态识别挑战脚本里的变量角色，返回 {'array','index','key','target'}，失败返回 None。

    array 是被赋成十六进制数组的变量，index 参与 do{...}while(--X>=N)，key 来自 setTimeout("fn(N)")。
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
    """切出含 marker 的 do{...}while(...) 块，返回 (block, end, head)。

    end 是 while 的终止边界，head 是 do 之前一段（用于找 `X = N;` 初始化）。
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
    try:
        step = int(m.group(1)) if m else 7
    except (TypeError, ValueError):
        step = 7
    # 步长为 0 会让下游 `% step` 直接 ZeroDivisionError，
    # 那样异常会逃出 WAF 归类（waf_failed 不会被置位）
    return step if step > 0 else 7


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
        self.contribution = None  # 贡献值，接入 results 供明细展示
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

        返回 (ok, msg, waf_failed)；waf_failed=True 表示失败在 WAF 环节，属站点侧问题。
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
        """获取 formhash 与 uid，判定 Cookie 是否有效。

        优先级：游客 uid=0 → 失效；拿不到页面 → 未知；WAF 失败但页面可读 → 站点问题；
        取到 formhash → 正常；页面可读但无 formhash → 失效。
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
            # 请求已发出但结果不可知，按 unknown 处理（原先漏了 state，会导致
            # run() 解包失败，真实原因被 ValueError 覆盖）
            return False, f'签到请求异常：{e}', 'unknown'
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
            if self.credit_gain > 0 and state != 'already':
                # 有正向增量是硬证据：可把「失败/未知」升级为「成功」。
                # 但接口若是明确说「已签到」，不升级（否则会被算成今日新签）
                success, state = True, 'success'
                prefix = '签到成功（此前已签到）' if already else '签到成功'
                sign_msg = f'{prefix}，获得 {self.credit_gain} 恩山币'
            # state == 'unknown' 时积分无变化不足以证明已签到，保持未知
            self.sign_state = state

        return sign_msg, success, already


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
    """单个账号的展示行（输出到日志）。"""
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

    when = result_time_line(r, ("signed_at", "finished_at"), indent=indent + "    ")
    if when:
        lines.append(when)

    contribution = r.get('contribution')
    if contribution and str(contribution) not in ('0', '未知等级'):
        lines.append(f"{indent}    🏅 贡献：{contribution}")

    lines.append(f"{indent}    🔑 Cookie：{cookie_line(r)}")
    return lines


def main():
    """入口，固定五段：依赖 → 配置 → 延迟 → 逐账号执行 → 汇总通知。"""
    if not require_dep("requests", PLATFORM):
        return EXIT_CONFIG

    raw = env_str("ENSHAN_COOKIE")
    if not raw:
        return notify_config_error(
            PLATFORM, "缺少 Cookie",
            "未找到 ENSHAN_COOKIE 环境变量",
            hints=[
                "🔑 需要登录态字段：auth / saltkey 等（前缀不限）",
                "💡 浏览器登录后 F12 → Application → Cookies 复制整段",
                "📝 多账号每行一个，行内不要换行",
            ])

    cookies, dropped = split_accounts(raw)
    report_duplicates("ENSHAN_COOKIE", dropped)
    if not cookies:
        return notify_config_error(
            PLATFORM, "Cookie 格式错误",
            "ENSHAN_COOKIE 解析后没有任何有效账号",
            hints=[
                "📝 正确格式：浏览器登录后 F12 复制的整段 Cookie",
                "💡 多账号每行一个，不要用 && 拼在一行",
            ])

    banner(PLATFORM, len(cookies))
    start_delay(PLATFORM)

    started = time.time()
    total = len(cookies)
    results = []
    warnings = []

    for idx, cookie in enumerate(cookies, 1):
        account_header(idx, f"账号{idx}", total)
        signer = EnShanSigner(cookie, idx)
        t0 = time.time()
        try:
            message, success, already = signer.run()
        except Exception as e:
            message, success, already = f"执行异常：{type(e).__name__}: {e}", False, False
            # 异常时状态可能已被 run() 写成 success，必须覆盖，
            # 否则统计会按 state 把这个账号算成成功
            signer.sign_state = "fail"
            signer.signed_at = None
        elapsed = time.time() - t0

        tag = f"账号{idx}"
        if signer.user_name:
            tag += f"({mask_username(signer.user_name)})"
        if signer.cookie_valid is False:
            warnings.append(f"[{tag}] {MSG_COOKIE_EXPIRED}")
        elif signer.waf_failed and signer.sign_state in ("fail", "unknown"):
            # 只在真的没签成时提醒站点异常，避免「✅ 成功」配一条「WAF 异常」
            warnings.append(f"[{tag}] {MSG_WAF_FAILED}")

        results.append({
            "tag": tag,
            "success": success,
            "already": already,
            "unknown": signer.sign_state == "unknown",
            "state": signer.sign_state,
            "message": message,
            "user_name": signer.user_name,
            "user_group": signer.user_group,
            "credit_before": signer.credit_before,
            "credit_after": signer.credit_after,
            "credit_gain": signer.credit_gain,
            "contribution": signer.contribution,
            "cookie_valid": signer.cookie_valid,
            "waf_failed": signer.waf_failed,
            "signed_at": signer.signed_at,
            "finished_at": time.time(),
            "elapsed": elapsed,
        })
        log(("✅ " if success else "❌ ") + f"[{tag}] {message}")
        log(f"⏱️  [{tag}] 耗时 {fmt_elapsed(elapsed)}")
        account_delay(idx, total, PLATFORM)

    counts = count_results(results, already_key="already", unknown_key="unknown")
    print_summary(*counts, details=[result_lines(r, indent="  ") for r in results])
    total, success, already, failed, unknown = counts
    new_sign = success - already

    if failed == 0 and unknown == 0:
        title = f"{PLATFORM}成功｜新签 {new_sign}／已签 {already}"
    elif failed == 0 and unknown == total:
        title = f"{PLATFORM}结果未知｜{unknown} 个账号均未确认"
    elif failed == 0:
        title = f"{PLATFORM}完成｜部分结果未知（{unknown} 个）"
    else:
        title = (f"{PLATFORM}失败｜新签 {new_sign}／已签 {already}／失败 {failed}"
                 + (f"／未知 {unknown}" if unknown else ""))

    gains = [r.get("credit_gain") for r in results if (r.get("credit_gain") or 0) > 0]
    extra = [
        f"🆕 本次新签 {new_sign}｜📅 今日已签 {already}",
        (f"🎁 合计获得：+{sum(gains)} 恩山币" if gains
         else "🎁 合计获得：无（或未取到积分）"),
    ]

    return notify_result(
        PLATFORM, total, success, failed, unknown,
        title=title, extra=extra,
        details=["\n".join(result_lines(r)) for r in results],
        notes=warnings,
        elapsed=time.time() - started,
        footers=["🔑 Cookie 失效后重新抓取 ENSHAN_COOKIE；WAF 报错稍后重跑即可"])


if __name__ == "__main__":
    sys.exit(main())
