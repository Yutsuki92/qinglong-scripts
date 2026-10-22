#!/usr/bin/python3
"""cron: 0 0 * * *
new Env('天翼云盘签到')
改编自：https://www.52pojie.cn/thread-1231190-1-1.html

环境变量 TY_ACCOUNT：账号:密码，多账号换行分隔，也可一行写 user1:pass1&&user2:pass2。
按第一个冒号切分（密码含冒号无妨），中文冒号「：」同样兼容。

可选：RANDOM_SIGNIN（默认 true）、MAX_RANDOM_DELAY（默认 300，启动延迟上限秒）

依赖：requests、rsa、ql_common.py（需同目录）
"""

import time
import re
import json
import base64
import urllib.parse
import sys
try:
    import rsa
except ImportError:  # 缺依赖不在这里崩，交由 main 的 require_dep 出结论
    rsa = None
try:
    import requests
except ImportError:  # 缺依赖不在这里崩，交由 main 的 require_dep 出结论
    requests = None
from datetime import datetime

try:
    from ql_common import (  # noqa: E402
        EXIT_CONFIG, account_delay, account_header, banner,
        env_str, fmt_clock, fmt_elapsed, get_logger, notify_config_error, notify_result,
        require_dep, start_delay,
    )
except ImportError:  # 公共层没上传/没被订阅同步：给结论，不抛 Traceback
    print("❌ 缺少公共层 ql_common.py：它必须与本脚本放在同一目录")
    print("🔧 青龙「订阅」会同步整个仓库；手动上传时请把它一起传上去")
    sys.exit(2)

PLATFORM = "天翼云盘签到"
log = get_logger("ty_netdisk_checkin")


BI_RM = list("0123456789abcdefghijklmnopqrstuvwxyz")
B64MAP = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


class TianYiYunPan:
    def __init__(self, username, password, index):
        self.username = username
        self.password = password
        self.index = index
        self.session = requests.Session()

        # 明细展示用的字段
        self.logged_in = False   # 登录是否成功
        self.sign_at = None      # 签到接口返回的时刻
        self.bonus_mb = 0        # 本次签到获得的空间（MB）
        self.new_bonus_mb = 0    # 其中本次真正新增的部分（已签过的记 0）
        self.already = False     # 今日此前是否已签到

    def int2char(self, a):
        return BI_RM[a]

    def b64tohex(self, a):
        d = ""
        e = 0
        c = 0
        for i in range(len(a)):
            if list(a)[i] != "=":
                v = B64MAP.index(list(a)[i])
                if 0 == e:
                    e = 1
                    d += self.int2char(v >> 2)
                    c = 3 & v
                elif 1 == e:
                    e = 2
                    d += self.int2char(c << 2 | v >> 4)
                    c = 15 & v
                elif 2 == e:
                    e = 3
                    d += self.int2char(c)
                    d += self.int2char(v >> 2)
                    c = 3 & v
                else:
                    e = 0
                    d += self.int2char(c << 2 | v >> 4)
                    d += self.int2char(15 & v)
        if e == 1:
            d += self.int2char(c << 2)
        return d

    def rsa_encode(self, j_rsakey, string):
        rsa_key = f"-----BEGIN PUBLIC KEY-----\n{j_rsakey}\n-----END PUBLIC KEY-----"
        pubkey = rsa.PublicKey.load_pkcs1_openssl_pem(rsa_key.encode())
        result = self.b64tohex((base64.b64encode(rsa.encrypt(f'{string}'.encode(), pubkey))).decode())
        return result

    def login(self):
        """登录天翼云盘：兼容 2026 新版 wap 登录页"""
        try:
            print(f"👤 账号{self.index}: 开始登录 {self.username}")

            headers = {
                "User-Agent": "Mozilla/5.0 (Linux; Android 12; Mobile) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            self.session.headers.update(headers)

            def add_param(url, key, value):
                if key + "=" in url:
                    return url
                sep = "&" if "?" in url else "?"
                return url + sep + key + "=" + urllib.parse.quote(str(value))

            def parse_json_or_jsonp(resp_text):
                txt = resp_text.strip()
                if txt.startswith("callbackMsg("):
                    txt = txt[len("callbackMsg("):]
                    if txt.endswith(");"):
                        txt = txt[:-2]
                    elif txt.endswith(")"):
                        txt = txt[:-1]
                return json.loads(txt)

            urlToken = "https://m.cloud.189.cn/udb/udb_login.jsp?pageId=1&pageKey=default&clientType=wap&redirectURL=https://m.cloud.189.cn/zhuanti/2021/shakeLottery/index.html"
            r = self.session.get(urlToken, headers=headers, timeout=15)

            match = re.search(r"https?://[^\s'\"]+", r.text)
            if not match:
                raise Exception("获取登录URL失败")

            auto_url = match.group()
            r = self.session.get(auto_url, headers={**headers, "Referer": urlToken}, timeout=15)

            if "/index.html" in r.url:
                login_page_url = r.url.replace("/index.html", "/login.html")
            else:
                login_page_url = r.url

            login_page_url = add_param(login_page_url, "protocol", "https")
            login_page_url = add_param(login_page_url, "showback", "true")

            r = self.session.get(
                login_page_url,
                headers={**headers, "Referer": auto_url},
                timeout=15
            )

            if "j_rsaKey" not in r.text:
                with open("/tmp/189_login_page_error.html", "w", encoding="utf-8") as f:
                    f.write(r.text)
                raise Exception("获取账号密码登录页失败，页面已保存到 /tmp/189_login_page_error.html")

            key_match = re.search(r'id=["\']j_rsaKey["\'][^>]*value=["\']([^"\']+)["\']', r.text)
            if not key_match:
                raise Exception("提取 j_rsaKey 失败")

            j_rsakey = key_match.group(1)

            query = urllib.parse.urlsplit(r.url).query
            conf_url = "https://open.e.189.cn/api/logbox/oauth2/wap/appConf.do?" + query

            rc = self.session.post(
                conf_url,
                headers={**headers, "Referer": r.url},
                timeout=15
            )

            conf = rc.json()
            if str(conf.get("result")) != "0":
                raise Exception(f"appConf 获取失败: {conf.get('msg', conf)}")

            data_conf = conf.get("data") or {}

            appKey = data_conf.get("appKey", "cloud")
            accountType = data_conf.get("accountType", "02")
            paramId = data_conf.get("paramId", "")
            lt = data_conf.get("lt", "")
            reqId = data_conf.get("reqId", "")
            state = data_conf.get("state", "")
            isOauth2 = str(data_conf.get("isOauth2", True)).lower()
            returnUrl = urllib.parse.quote(data_conf.get("returnUrl", ""), safe="")

            if not paramId or not lt or not reqId or not returnUrl:
                raise Exception("appConf 关键参数不完整")

            self.session.headers.update({"lt": lt})

            username_plain = self.username
            if str(data_conf.get("hasAt", "false")).lower() == "true" and "@" not in username_plain:
                username_plain = username_plain + "@189.cn"

            username_encrypted = self.rsa_encode(j_rsakey, username_plain)
            password_encrypted = self.rsa_encode(j_rsakey, self.password)

            login_url = "https://open.e.189.cn/api/logbox/oauth2/loginSubmit.do"

            params = {
                "apptype": "wap",
                "appKey": appKey,
                "accountType": accountType,
                "dynamicCheck": "false",
                "userName": "{RSA}" + username_encrypted,
                "epd": "{RSA}" + password_encrypted,
                "version": "v2.0",
                "returnUrl": returnUrl,
                "isConfigurable": "true",
                "isOauth2": isOauth2,
                "state": state,
                "paramId": paramId,
                "lt": lt,
                "REQID": reqId,
                "callbackMsg": "callbackMsg",
            }

            r = self.session.get(
                login_url,
                params=params,
                headers={
                    **headers,
                    "Referer": r.url,
                    "Accept": "*/*",
                },
                timeout=15
            )

            try:
                result = r.json()
            except Exception:
                result = parse_json_or_jsonp(r.text)

            if str(result.get("result")) == "0":
                print(f"✅ 账号{self.index}: 登录成功")
                self.logged_in = True
                redirect_url = result.get("toUrl")
                if redirect_url:
                    self.session.get(redirect_url, headers=headers, timeout=15)
                return True

            msg = result.get("msg") or result.get("message") or result
            print(f"❌ 账号{self.index}: 登录失败 - {msg}")
            return False

        except Exception as e:
            print(f"❌ 账号{self.index}: 登录异常 - {str(e)}")
            return False

    def sign_in(self):
        """执行签到"""
        try:
            print(f"🎯 账号{self.index}: 开始签到")

            rand = str(round(time.time() * 1000))
            sign_url = f'https://api.cloud.189.cn/mkt/userSign.action?rand={rand}&clientType=TELEANDROID&version=8.6.3&model=SM-G930K'

            headers = {
                'User-Agent': 'Mozilla/5.0 (Linux; Android 5.1.1; SM-G930K Build/NRD90M; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/74.0.3729.136 Mobile Safari/537.36 Ecloud/8.6.3 Android/22 clientId/355325117317828 clientModel/SM-G930K imsi/460071114317824 clientChannelId/qq proVersion/1.0.6',
                "Referer": "https://m.cloud.189.cn/zhuanti/2016/sign/index.jsp?albumBackupOpened=1",
                "Host": "m.cloud.189.cn",
                "Accept-Encoding": "gzip, deflate",
            }

            response = self.session.get(sign_url, headers=headers, timeout=15)
            result = response.json()

            if not isinstance(result, dict) or 'isSign' not in result:
                raise ValueError(
                    f"签到接口返回异常（缺少 isSign 字段）: {str(result)[:120]}"
                )

            netdiskBonus = result.get('netdiskBonus', 0)
            # isSign: false = 本次签到成功；true = 今日此前已签
            raw_is_sign = str(result.get('isSign')).strip().lower()
            is_sign_false = raw_is_sign in ("false", "0", "0.0")

            self.sign_at = time.time()
            try:
                self.bonus_mb = int(netdiskBonus or 0)
            except (TypeError, ValueError):
                self.bonus_mb = 0
            self.already = not is_sign_false
            self.new_bonus_mb = 0 if self.already else self.bonus_mb

            if is_sign_false:
                status_msg = f"✅ 签到成功，获得 {netdiskBonus}M 空间"
                print(f"✅ 账号{self.index}: {status_msg}")
            else:
                status_msg = f"📅 今日已签到，获得 {netdiskBonus}M 空间"
                print(f"📅 账号{self.index}: {status_msg}")

            return status_msg, True

        except Exception as e:
            error_msg = f"签到异常: {str(e)}"
            print(f"❌ 账号{self.index}: {error_msg}")
            return error_msg, False

    def main(self):
        """主执行函数"""
        started = time.time()
        try:
            print(f"\n==== 账号{self.index} 开始执行 ====")
            print(f"🕐 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            # 登录
            if not self.login():
                error_msg = (f"❌ 账号{self.index}: {self.username}\n"
                             f"登录失败，无法完成签到\n"
                             f"⏱️ 本次耗时: {fmt_elapsed(time.time() - started)}\n"
                             f"🔑 凭证状态: 账号密码登录失败，请检查账号或密码")
                print(error_msg)
                return error_msg, False

            # 签到
            sign_result, sign_ok = self.sign_in()
            elapsed = time.time() - started

            # 获得内容：签到接口直接返回本次空间奖励
            if not sign_ok:
                gain_text = "无（签到未成功）"
            elif self.bonus_mb:
                gain_text = (f"+{self.bonus_mb} MB"
                             + ("（本次无新增）" if self.already else ""))
            else:
                gain_text = "无（接口未返回空间）"

            sign_time = (f"🕐 签到时间: {fmt_clock(self.sign_at)}"
                         if self.sign_at else
                         f"🕐 完成时间: {fmt_clock()}")
            status_text = "今日已签到" if self.already else ""

            # 格式化结果（标题已体现成功/失败，这里不重复）
            head = f"👤 账号: {self.username}"
            if status_text:
                head += f"（{status_text}）"
            result_msg = f"""{head}
🎁 今日获得: {gain_text}
{sign_time}（耗时 {fmt_elapsed(elapsed)}）"""
            if not sign_ok:
                result_msg += f"\n❌ {sign_result}"

            print("\n🎉 === 最终签到结果 ===")
            print(result_msg)
            print(f"==== 账号{self.index} 签到完成 ====\n")

            return result_msg, sign_ok

        except Exception as e:
            error_msg = (f"❌ 账号{self.index}: 执行异常 - {str(e)}\n"
                         f"⏱️ 本次耗时: {fmt_elapsed(time.time() - started)}")
            print(error_msg)
            return error_msg, False


def _mask_account_item(item):
    """日志里输出「账号:密码」时脱敏，避免把明文密码写进青龙日志。"""
    if '：' in item and ':' not in item:
        item = item.replace('：', ':', 1)
    if ':' in item:
        user, _, pwd = item.partition(':')
        user = user.strip()
        user = (user[:3] + '****' + user[-4:]) if len(user) > 7 else (user or '(空)')
        return f"{user}:{'*' * min(len(pwd.strip()), 6) or '(空)'}"
    return f"***（长度 {len(item)}，缺少冒号）"


def parse_accounts(raw):
    """解析 TY_ACCOUNT，返回 [(username, password), ...]。

    按第一个冒号切分（密码含冒号无妨），兼容中文冒号与 && 连接；面板会把同名多行用 & 拼成一条，
    故单个 & 在「后面紧跟 账号:密码 形状」时也当分隔符，user:pa&ss 不会被拆散。
    """
    if not raw:
        return []

    # 换行与 && 视为账号分隔符（不再按逗号切：密码里可能含逗号）
    normalized = raw.replace('\r\n', '\n').replace('\r', '\n')
    parts = []
    for chunk in re.split(r'\n|&&', normalized):
        seg = ''
        for piece in chunk.split('&'):
            if seg and re.match(r'^[^:：&\s]+[:：]', piece):
                parts.append(seg)
                seg = piece
            else:
                seg = f'{seg}&{piece}' if seg else piece
        if seg.strip():
            parts.append(seg)

    accounts = []
    for part in parts:
        item = part.strip()
        if not item:
            continue
        # 用中文冒号或英文冒号切分，按第一个冒号切
        if '：' in item and ':' not in item:
            item = item.replace('：', ':', 1)
        if ':' not in item:
            print(f"⚠️  跳过格式错误的一项（缺少冒号）: {_mask_account_item(item)}")
            continue
        username, password = item.split(':', 1)
        username, password = username.strip(), password.strip()
        if not username or not password:
            print(f"⚠️  跳过格式错误的一项（账号或密码为空）: {_mask_account_item(item)}")
            continue
        accounts.append((username, password))

    by_user = {}
    for username, password in accounts:
        if username in by_user:
            masked = username[:3] + "****" + username[-4:] if len(username) > 7 else username
            print(f"⚠️  {masked} 重复出现，以最后一条为准（只签一次）")
        by_user[username] = password

    unique = list(by_user.items())

    if len(unique) != len(accounts):
        print(f"⚠️  TY_ACCOUNT 中共忽略 {len(accounts) - len(unique)} 个重复账号")

    return unique


def main():
    """入口，固定五段：依赖 → 配置 → 延迟 → 逐账号执行 → 汇总通知。

    两个依赖分别检查：requests 缺了全部接口不可用，rsa 缺了只是登录不了，
    但两者都属于环境问题，结论一致 —— 退出码 2、提示装什么。
    """
    if not require_dep("requests", PLATFORM):
        return EXIT_CONFIG
    if not require_dep("rsa", PLATFORM):
        return EXIT_CONFIG

    raw = env_str("TY_ACCOUNT")
    if not raw:
        return notify_config_error(
            PLATFORM, "缺少账号",
            "未找到 TY_ACCOUNT 环境变量",
            hints=[
                "🔑 格式：手机号:密码，多账号用换行分隔",
                "💡 密码里可以有冒号，脚本按第一个冒号切分",
            ])

    accounts = parse_accounts(raw)
    if not accounts:
        return notify_config_error(
            PLATFORM, "账号格式错误",
            "TY_ACCOUNT 中没有可用账号（应为 账号:密码）",
            hints=[
                "📝 正确写法：13800000000:密码",
                "💡 多账号每行一个；密码里可以有 & 和冒号",
            ])

    banner(PLATFORM, len(accounts))
    start_delay(PLATFORM)

    run_start = time.time()
    total = len(accounts)
    success_count = 0
    already_count = 0
    total_bonus_mb = 0
    blocks = []

    for index, (username, password) in enumerate(accounts, 1):
        account_header(index, _mask_account_item(f"{username}:{password}"), total)
        try:
            account = TianYiYunPan(username, password, index)
            message, is_success = account.main()
            total_bonus_mb += account.new_bonus_mb or 0
            if account.already:
                already_count += 1
        except Exception as e:
            message = f"处理异常：{type(e).__name__}: {e}"
            is_success = False
            log(f"❌ 账号{index} {message}")
        if is_success:
            success_count += 1
        blocks.append(f"{'✅' if is_success else '❌'} 账号{index}\n{str(message).strip()}")
        account_delay(index, total, PLATFORM)

    extra = [f"🎁 本次新增 {total_bonus_mb} MB"
             + (f"（另有 {already_count} 个账号今日早前已签，未计入）"
                if already_count else "")]
    return notify_result(
        PLATFORM, total, success_count, total - success_count,
        extra=extra, details=blocks, elapsed=time.time() - run_start,
        footers=["🔑 改密后需同步 TY_ACCOUNT；提示需要短信验证时建议改用 Cookie"])


if __name__ == "__main__":
    sys.exit(main())
