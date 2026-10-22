#!/usr/bin/python3
"""new Env('夸克网盘签到')
cron: 0 0 * * *

移动端接口每日签到，多账号。依赖 requests、ql_common.py（需同目录）。

环境变量 QUARK_COOKIE（每行一个账号），两种格式：
  user=张三; url=https://drive-m.quark.cn/.../reward?...&kps=..&sign=..&vcode=..
  user=张三; kps=..; sign=..; vcode=..              （旧版）
user 可随意填写，仅用于区分账号；抓包取抽奖页请求的整段 url（含 kps/sign/vcode）粘贴即可。

可选：RANDOM_SIGNIN（默认 true）、MAX_RANDOM_DELAY（默认 300）、QUARK_TIMEOUT（默认 15）

Author: BNDou   Source: https://github.com/Cp0204/quark-auto-save
"""
import re
import sys
import time

try:
    from ql_common import (  # noqa: E402
        EXIT_CONFIG, account_delay, account_header, banner,
        env_int, env_str, fmt_clock, fmt_elapsed, get_logger, notify_config_error,
        notify_result, require_dep, start_delay,
    )
except ImportError:  # 公共层没上传/没被订阅同步：给结论，不抛 Traceback
    print("❌ 缺少公共层 ql_common.py：它必须与本脚本放在同一目录")
    print("🔧 青龙「订阅」会同步整个仓库；手动上传时请把它一起传上去")
    sys.exit(2)

PLATFORM = "夸克网盘签到"
logger = get_logger("quark_checkin")

try:
    import requests
except ImportError:  # 缺依赖不在这里崩，交由 main 的 require_dep 出结论
    requests = None

# requests 的异常文本会带上完整 URL，而夸克的 kps/sign/vcode 写在 query 里，
# 等价于账号凭证：直接 f"{e}" 会把凭证原样写进青龙日志（面板多用户可见）。
_SECRET_QUERY_RE = re.compile(
    r"((?:kps|sign|vcode|pwd|password|token)=)([^&\s'\"]+)", re.I
)


def mask_secrets(obj):
    """把 URL/异常文本里的凭证参数替换成 ***。"""
    return _SECRET_QUERY_RE.sub(lambda m: m.group(1) + "***", str(obj))


TIMEOUT = env_int('QUARK_TIMEOUT', 15, 5, 120)
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

        # 明细展示用的字段（由 do_sign 填充）
        self.sign_at = None          # 真正执行签到的时刻；已签到则为查询时刻
        self.reward_bytes = 0        # 本次计入的容量（字节）
        self.already_signed = False  # 今日此前是否已签
        self.sign_ok = False         # 本账号是否签到成功/确认已签（供退出码统计）
        self.streak = ''             # 连签进度文本，如 5/30

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
                    logger.warning(f"    🌐 网络异常: {mask_secrets(e)}")
                    return None
            except ValueError:
                logger.warning(f"    ⚠️  响应不是合法 JSON: {mask_secrets(url)}")
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
            logger.error("    🔑 鉴权失败，kps/sign/vcode 可能已过期，请重新抓包更新环境变量")

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

        sign_reward = cap_comp.get('sign_reward', 0) or 0
        summary = (f"{'88VIP' if growth_info.get('88VIP') else '普通用户'}｜"
               f"总容量 {convert_bytes(growth_info.get('total_capacity', 0))}｜"
               f"签到累计 {convert_bytes(sign_reward)}\n")

        progress = cap_sign.get('sign_progress', 0) or 0
        target = cap_sign.get('sign_target', 0) or 0

        if cap_sign.get("sign_daily"):
            self.already_signed = True
            self.sign_ok = True
            self.reward_bytes = cap_sign.get('sign_daily_reward', 0) or 0
            self.streak = f"{progress}/{target}"
            self.sign_at = time.time()
            summary += "✅ 今日已签到（此前完成）\n"
        else:
            ok, reward = self.get_growth_sign()
            if ok:
                self.sign_ok = True
                self.sign_at = time.time()
                self.reward_bytes = reward or 0
                self.streak = f"{progress + 1}/{target}"
                summary += "✅ 签到完成\n"
            else:
                self.reward_bytes = 0
                summary += f"❌ 签到异常: {reward}\n"

        return summary

    def detail_lines(self, elapsed):
        """明细：获得了什么 / 签到时间 / 耗时（输出到日志）。"""
        lines = []

        if self.reward_bytes:
            if self.already_signed:
                what = f"本次无新增（今日早前已签 +{convert_bytes(self.reward_bytes)}）"
            else:
                what = f"+{convert_bytes(self.reward_bytes)}"
                if self.streak:
                    what += f"（连签 {self.streak}）"
            lines.append(f"🎁 今日获得：{what}")
        elif self.already_signed:
            lines.append("🎁 今日获得：本次无新增（今日已签到）")
        elif self.sign_ok:
            # 签到成功但接口没给奖励数值，别写成「未完成」
            lines.append("🎁 今日获得：无新增（接口未返回奖励）")
        else:
            lines.append("🎁 今日获得：无（签到未完成）")

        if self.sign_at:
            lines.append(f"🕐 签到时间：{fmt_clock(self.sign_at)}"
                         f"（耗时 {fmt_elapsed(elapsed)}）")
        else:
            lines.append(f"🕐 完成时间：{fmt_clock()}（耗时 {fmt_elapsed(elapsed)}）")

        return lines


def split_quark_accounts(raw):
    """按真实语义切分多账号，避免把 url 里的 & 当成账号分隔。

    换行一定是分隔符；行内以 user= 出现的位置切分（url 里的 && 因此不会被误切）；
    行内没有 user= 时按 && 切，并把「无 user= 且带 &」的碎片并回上一段。
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


def load_accounts():
    """读取并切分 QUARK_COOKIE，返回 (账号列表, 失败标题, 失败说明)。

    读配置不再直接 sys.exit —— 退出码由 main() 统一决定，通知也在 main() 里发，
    这样「配置失败」在各脚本里走同一条路径。
    """
    raw = env_str("QUARK_COOKIE")
    if not raw:
        return [], "缺少 Cookie", "未添加 QUARK_COOKIE 变量"

    accounts = split_quark_accounts(raw.replace("\r\n", "\n").replace("\r", "\n"))
    unique = list(dict.fromkeys(accounts))
    if len(unique) != len(accounts):
        logger.warning(f"⚠️ QUARK_COOKIE 中有 {len(accounts) - len(unique)} 个重复账号，已自动忽略")
    if not unique:
        return [], "账号解析为空", "QUARK_COOKIE 未解析出任何账号"
    return unique, "", ""


def main():
    """入口，固定五段：依赖 → 配置 → 延迟 → 逐账号执行 → 汇总通知。"""
    if not require_dep("requests", PLATFORM):
        return EXIT_CONFIG

    accounts, reason, detail = load_accounts()
    if not accounts:
        return notify_config_error(
            PLATFORM, reason, detail,
            hints=[
                "🔑 需要 user / kps / sign / vcode（或整段 url）",
                "💡 登录 pan.quark.cn → F12 → Network → 任一转存请求的 query 里复制",
                "📝 多账号每行一个，行内不要用 && 拼接",
            ])

    banner(PLATFORM, len(accounts))
    start_delay(PLATFORM)

    run_start = time.time()
    total = len(accounts)
    total_reward = 0
    failed_count = 0
    details = []

    for i, seg in enumerate(accounts, 1):
        user_data = parse_account(seg)
        name = user_data.get("user") or f"账号{i}"
        account_header(i, f"第{i}个账号 {name}", total)
        acc = Quark(user_data, i - 1)
        t0 = time.time()
        try:
            block = acc.do_sign()
        except Exception as e:
            block = f"❌ {name}: 执行异常 {mask_secrets(e)}"
            # 不带 exc_info：traceback 末尾会回显带 kps/sign/vcode 的 URL
            logger.error(f"第{i}个账号执行异常: {type(e).__name__}: {mask_secrets(e)}")
        elapsed = time.time() - t0

        if not acc.already_signed:
            total_reward += acc.reward_bytes or 0
        if not getattr(acc, "sign_ok", False):
            failed_count += 1

        # 详情直接打到日志，同时收集给通知
        print(block)
        detail_block = [f"🙍 第{i}个账号 {name}", str(block).strip()]
        for line in acc.detail_lines(elapsed):
            print(line)
            detail_block.append(line)
        details.append("\n".join(detail_block))
        account_delay(i, total, PLATFORM)

    ok_count = total - failed_count
    extra = [
        (f"🎁 本次新签合计：+{convert_bytes(total_reward)}" if total_reward
         else "🎁 本次新签合计：无（今日已签或无奖励）"),
    ]
    return notify_result(
        PLATFORM, total, ok_count, failed_count,
        extra=extra, details=details, elapsed=time.time() - run_start,
        footers=["🔑 凭证由登录 URL 派生，失效后整段重抓"])


if __name__ == "__main__":
    sys.exit(main())
