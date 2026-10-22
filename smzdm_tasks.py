"""cron: 10 0 * * *
new Env('什么值得买活动')

众测 + 互动任务（浏览/收藏/点赞/关注），多账号。依赖 requests、ql_common.py（需同目录）。

环境变量
  SMZDM_COOKIE        必填，多账号换行分隔
  SMZDM_TASK_MODULES  默认 both；可选 both / zhongce / interactive（all 视同 both）
  RANDOM_SIGNIN       默认 true，false 关闭全部延迟
  MAX_RANDOM_DELAY    默认 300，启动延迟上限（秒）
"""

import time
import random
import hashlib
import re
import sys
from urllib.parse import unquote

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

PLATFORM = "什么值得买活动"
logger = get_logger("smzdm_tasks")

try:
    import requests
except ImportError:  # 缺依赖不在这里崩，交由 main 的 require_dep 出结论
    requests = None


def to_int(value, default=0):
    """把接口里可能是 str/int/None 的数值统一成 int。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def new_stats():
    """模块统计的统一结构。

    fatal 非空表示该模块整体失败（拿不到活动/任务列表），不能只看统计是否为 0；
    claimed_ids / claimed_rewards 记录执行阶段已领的奖励，避免收尾重复领取或汇总漏记。
    """
    return {
        'success': 0, 'fail': 0, 'skip': 0,
        'reward': None,
        'fatal': '',          # 模块整体失败（请求异常/异常中断）→ 账号判失败
        'unavailable': '',    # 今日没有该模块的活动 → 正常情况，不算失败
        'claimed_ids': set(),
        'claimed_rewards': [],
        'stage_reward': None,   # 活动阶段性奖励：None=无 / True=已领 / False=领取失败
    }


def task_reward_text(task):
    """从任务的 task_reward_data 里拼出奖励名称，如「5金币」。"""
    data = task.get('task_reward_data') or []
    parts = [f"{r.get('name', '')}{r.get('num', '')}" for r in data if isinstance(r, dict)]
    return ', '.join(p for p in parts if p.strip())


_TASK_MODULES_RAW = env_str("SMZDM_TASK_MODULES", "both").lower()
TASK_MODULES = "both" if _TASK_MODULES_RAW == "all" else _TASK_MODULES_RAW
TASK_MODULE_CHOICES = ("both", "zhongce", "interactive")
if TASK_MODULES not in TASK_MODULE_CHOICES:
    TASK_MODULES = "__invalid__"

SECRET_KEY = "zok5JtAq3$QixaA%mncn*jGWlEpSL3E1"

BASE_URL = "https://zhiyou.m.smzdm.com"
TEST_URL = "https://test.m.smzdm.com"
USER_API_URL = "https://user-api.smzdm.com"
TEST_API_URL = "https://test-api.smzdm.com"
ARTICLE_CDN_URL = "https://article-cdn.smzdm.com"
DINGYUE_API_URL = "https://dingyue-api.smzdm.com"

APP_VERSION = '11.1.35'
APP_UA = 'smzdm 11.1.35 rv:167 (iPhone 6s; iOS 15.8.3; zh_CN)/iphone_smzdmapp/11.1.35'


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
    """什么值得买活动接口封装"""

    def __init__(self, cookie: str):
        self.cookie = cookie
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': APP_UA,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh-Hans;q=0.9',
            'Origin': TEST_URL,
            'Referer': f'{TEST_URL}/',
            'Cookie': cookie,
        })

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def _get_sess_token(self) -> str:
        """从 Cookie 提取 sess 字段作为 token"""
        for item in self.cookie.split(';'):
            if 'sess=' in item:
                return unquote(item.split('sess=')[1].strip())
        return ""

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

    def _app_headers(self, extra=None):
        h = dict(self.session.headers)
        h.update({
            'User-Agent': APP_UA,
            'Content-Type': 'application/x-www-form-urlencoded',
            'request_key': str(int(time.time() * 1000000000))[:18],
            'Content-Encoding': 'gzip',
            'Accept-Language': 'zh-Hans-CN;q=1',
        })
        if extra:
            h.update(extra)
        return h

    def _signed_post(self, url, params):
        params = dict(params)
        params['sign'] = calculate_sign(params)
        return self._request('POST', url, data=params, headers=self._app_headers())

    def get_activity_id(self, from_source='zhongce'):
        """返回活动 ID：None=请求失败，''=请求成功但当前没有活动，其余为 ID。"""
        url = f'{BASE_URL}/task/task/ajax_get_activity_id'
        data = self._request('GET', url, params={'from': from_source})
        if data is None:
            return None
        if isinstance(data.get('data'), dict):
            aid = data['data'].get('activity_id')
            if aid:
                return str(aid)
        return ''

    def get_activity_info(self, activity_id):
        url = f'{BASE_URL}/task/task/ajax_get_activity_info'
        data = self._request('GET', url, params={'activity_id': activity_id})
        if data and isinstance(data.get('data'), dict):
            return data['data']
        return None

    def get_task_list(self, activity_id):
        """合并 default_list / accumulate_list / clock_list"""
        info = self.get_activity_info(activity_id)
        if not info:
            return None
        tasks = []
        activity_task = info.get('activity_task') or {}
        for task_type in ['default_list', 'accumulate_list', 'clock_list']:
            tasks.extend(activity_task.get(task_type) or [])
        return tasks

    def get_user_energy_info(self):
        url = f'{TEST_URL}/win_coupon/user_data'
        data = self._request('GET', url)
        if data:
            return data.get('data', {})
        return None

    def receive_reward(self, task_id):
        """领取任务奖励"""
        url = f'{BASE_URL}/task/task/ajax_activity_task_receive'
        resp = self._request('POST', url, data={'task_id': task_id},
                             headers=self._app_headers())
        return resp is not None

    def get_robot_token(self):
        """获取 robot token（领取阶段性奖励用）"""
        params = {
            'basic_v': '0', 'f': 'iphone', 'time': str(int(time.time() * 1000)),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        data = self._signed_post(f'{USER_API_URL}/robot/token', params)
        if data and isinstance(data.get('data'), dict):
            return data['data'].get('token')
        return None

    def activity_task_receive(self, activity_id, robot_token):
        """领取活动阶段性奖励（带 robot token）"""
        params = {
            'basic_v': '0', 'f': 'iphone', 'robot_token': robot_token,
            'task_id': activity_id, 'time': str(int(time.time() * 1000)),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        return self._signed_post(f'{USER_API_URL}/task/activity_task_receive', params)

    def get_interactive_task_list(self, point_type=0, limit=100, offset=0,
                                  source_from='任务活动'):
        params = {
            'basic_v': '0', 'f': 'iphone', 'get_total': '1',
            'limit': str(limit), 'offset': str(offset),
            'point_type': str(point_type), 'source_from': source_from,
            'time': str(int(time.time() * 1000)),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        data = self._signed_post(f'{USER_API_URL}/task/list_v2', params)
        if data is None:
            return None
        # 请求成功但可能没有任务：返回 {}，调用方据此区分「失败」与「无任务」
        return data.get('data') or {}

    def get_article_channel_id(self, article_id):
        """从 CDN 预加载 JSON 取 channel_id"""
        url = (f'{ARTICLE_CDN_URL}/preload/{article_id}/fiphone/v11_1_35/wx1/im0/'
               f'hcae67e467x7q/h5cc7e8ebddb8f0f73.json')
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"    ❌ 获取 channel_id 失败: {e}")
            return None
        if str(data.get('error_code')) == '0':
            cid = (data.get('data') or {}).get('channel_id')
            try:
                return int(cid)
            except (TypeError, ValueError):
                return None
        return None

    def view_article_task(self, task_id, article_id, channel_id):
        """完成浏览文章任务"""
        params = {
            'article_id': str(article_id), 'basic_v': '0',
            'channel_id': str(channel_id), 'f': 'iphone',
            'task_event_type': 'interactive.view.article',
            'task_id': task_id, 'time': str(int(time.time() * 1000)),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        return self._signed_post(f'{USER_API_URL}/task/event_view_article_sync', params)

    def favorite_article_task(self, task_id, article_id, channel_id=None):
        """收藏文章任务"""
        cid = channel_id or self.get_article_channel_id(article_id)
        if not cid:
            logger.error("    ❌ 无法获取 channel_id，收藏任务失败")
            return False
        params = {
            'basic_v': '0', 'channel_id': str(cid), 'f': 'iphone',
            'id': article_id, 'time': str(int(time.time() * 1000)),
            'token': self._get_sess_token(),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        return self._signed_post(f'{USER_API_URL}/favorites/create', params) is not None

    def rating_article_task(self, task_id, article_id, channel_id=None):
        """点赞文章任务"""
        cid = channel_id or self.get_article_channel_id(article_id)
        if not cid:
            logger.error("    ❌ 无法获取 channel_id，点赞任务失败")
            return False
        params = {
            'basic_v': '0', 'channel_id': str(cid), 'f': 'iphone',
            'id': article_id, 'time': str(int(time.time() * 1000)),
            'token': self._get_sess_token(),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        return self._signed_post(f'{USER_API_URL}/rating/like_create', params) is not None

    def share_probation_task(self, article_id, channel_id):
        """分享众测招募任务"""
        params = {
            'article_id': article_id, 'basic_v': '0',
            'channel_id': str(channel_id), 'f': 'iphone',
            'time': str(int(time.time() * 1000)),
            'token': self._get_sess_token(),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        return self._signed_post(f'{USER_API_URL}/share/callback', params) is not None

    def get_probation_list(self, status='progress', offset=0):
        params = {
            'basic_v': '0', 'f': 'iphone', 'offset': str(offset),
            'status': status, 'time': str(int(time.time() * 1000)),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        data = self._signed_post(f'{TEST_API_URL}/probation/list', params)
        if data:
            return (data.get('data') or {}).get('rows', [])
        return None

    def submit_probation_apply(self, probation_id):
        params = {
            'attention_merchant': '0', 'basic_v': '0', 'f': 'iphone',
            'probation_id': probation_id, 'remark_list': '["",""]',
            'time': str(int(time.time() * 1000)),
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        resp = self._signed_post(f'{TEST_API_URL}/probation/submit', params)
        return resp is not None

    def get_follow_user_list(self, page=1):
        params = {
            'basic_v': '0', 'f': 'iphone', 'nav_id': '83', 'page': str(page),
            'time': str(int(time.time() * 1000)), 'type': 'user',
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        data = self._signed_post(f'{DINGYUE_API_URL}/tuijian/search_result', params)
        if data:
            return data.get('data', {})
        return None

    def follow_user(self, keyword, keyword_id):
        params = {
            'basic_v': '0', 'f': 'iphone', 'is_follow_activity_page': '1',
            'is_from_task': '1', 'keyword': keyword, 'keyword_id': keyword_id,
            'refer': 'iPhone/关注/达人/推荐/',
            'time': str(int(time.time() * 1000)),
            'token': self._get_sess_token(), 'type': 'user',
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        return self._signed_post(f'{DINGYUE_API_URL}/dingyue/create', params) is not None

    def unfollow_user(self, keyword, keyword_id):
        params = {
            'basic_v': '0', 'f': 'iphone', 'keyword': keyword,
            'keyword_id': keyword_id,
            'refer': 'iPhone/公共/我的兴趣管理/感兴趣/全部',
            'time': str(int(time.time() * 1000)),
            'token': self._get_sess_token(), 'type': 'user',
            'v': APP_VERSION, 'weixin': '1', 'zhuanzai_ab': 'b',
        }
        return self._signed_post(f'{DINGYUE_API_URL}/dingyue/destroy', params) is not None


class SmzdmService:
    """业务处理层"""

    def __init__(self, api: SmzdmAPI):
        self.api = api

    def print_energy_info(self, user_data):
        if not user_data:
            return
        energy = user_data.get('energy') or user_data.get('energy_num')
        if energy is not None:
            logger.info(f"⚡ 用户能量值: {energy}")

    def execute_task(self, task):
        """
        按 task_event_type 分派众测任务
        Returns: True=已处理, False=失败, None=类型不支持（跳过，不计成功）
        """
        event_type = task.get('task_event_type', '')
        task_id = task.get('task_id', '')

        # 已完成待领取：领奖由调用方统一执行并记账（见 process_zhongce_tasks），
        # 这里只标记为跳过，避免同一 task_id 被领两次。

        # 真机里 article_id/channel_id 常为 0，需从 task_redirect_url 兜底解析
        article_id, channel_id = self._resolve_article_ref(task)

        if event_type == 'interactive.view.article':
            cid = channel_id or self.api.get_article_channel_id(article_id)
            if not cid or not article_id:
                logger.error("    ❌ 缺少 article_id/channel_id，浏览任务失败")
                return False
            return self.api.view_article_task(task_id, article_id, cid) is not None

        if event_type in ('interactive.favorite', 'interactive.favorite.article'):
            return self.api.favorite_article_task(task_id, article_id, channel_id)

        if event_type in ('interactive.rating', 'interactive.rating.article'):
            return self.api.rating_article_task(task_id, article_id, channel_id)

        if event_type in ('interactive.share', 'interactive.share.probation'):
            return self.api.share_probation_task(article_id, channel_id)

        # 真机类型是 guide.apply_zhongce
        if event_type in ('guide.apply_zhongce', 'zhongce.apply', 'probation.apply'):
            return self.apply_zhongce_task(task)

        logger.info(f"    ℹ️  未支持的众测任务类型 [{event_type}]，跳过")
        return None

    def apply_zhongce_task(self, task):
        """申请众测（按剩余次数批量申请）"""
        task_name = task.get('task_name', '未知任务')
        finished = int(task.get('task_finished_num', 0) or 0)
        total = int(task.get('task_even_num', 0) or 0)
        remaining = total - finished
        if remaining <= 0:
            logger.info(f"    ✅ 任务 [{task_name}] 已完成所有申请 ({finished}/{total})")
            return True

        logger.info(f"    📋 任务 [{task_name}] 需申请 {remaining} 次 "
                    f"(已完成 {finished}/{total})")
        probation_list = self.api.get_probation_list()
        if not probation_list:
            logger.error("    ❌ 获取众测列表失败")
            return False

        available = [item for item in probation_list
                     if (item.get('article_probation') or {}).get('product_status') == '1']
        if not available:
            logger.warning("    ⚠️  当前没有可申请的众测商品")
            return False

        logger.info(f"    📋 找到 {len(available)} 个可申请商品")
        success = 0
        for item in available:
            if success >= remaining:
                break
            aid = item.get('article_id', '')
            title = item.get('article_title', '未知商品')
            if not aid:
                continue
            logger.info(f"      → 申请: {title}")
            if self.api.submit_probation_apply(aid):
                success += 1
                logger.info(f"        ✅ 申请成功 ({success}/{remaining})")
            else:
                logger.info("        ⏭️  跳过该商品")
            if success < remaining:
                time.sleep(1)
        return success > 0

    def parse_interactive_tasks(self, task_data):
        """
        解析互动任务列表
        真机结构（已实测 list_v2）：
          data.rows[].cell_data.activity_task.{default_list_v2,accumulate_list,clock_list}
          default_list_v2[] 元素形如 {"task_list": [ {任务...}, ... ]}
        """
        rows = (task_data or {}).get('rows') or []
        tasks = []
        for row in rows:
            cell = row.get('cell_data', row)
            if not isinstance(cell, dict):
                continue
            activity_task = cell.get('activity_task')
            if isinstance(activity_task, dict):
                for grp in ('default_list_v2', 'default_list',
                            'accumulate_list', 'clock_list'):
                    for blk in (activity_task.get(grp) or []):
                        if not isinstance(blk, dict):
                            continue
                        sub = blk.get('task_list')
                        if isinstance(sub, list) and sub:
                            tasks.extend([t for t in sub if isinstance(t, dict)])
                        elif blk.get('task_id'):
                            tasks.append(blk)
                continue
            # 兜底：旧结构
            sub = cell.get('task_list')
            if isinstance(sub, list) and sub:
                tasks.extend([t for t in sub if isinstance(t, dict)])
            elif cell.get('task_id'):
                tasks.append(cell)
        return tasks

    def _resolve_article_ref(self, task):
        """解析众测任务的真实文章 ID，返回 (article_id, channel_id)。

        真机上两者常为 0，真实值在 task_redirect_url.link_val；channel_id 先取任务自带非零值，
        再找同活动内同文章的值，最后交给 CDN 兜底。
        """
        def _nonzero(v):
            try:
                return int(v or 0) != 0
            except (TypeError, ValueError):
                return False

        article_id = task.get('article_id')
        channel_id = task.get('article_channel_id') or task.get('channel_id')

        if not _nonzero(article_id):
            ru = task.get('task_redirect_url') or {}
            lv = ru.get('link_val')
            if lv and str(lv) not in ('0', ''):
                article_id = lv
            else:
                m = re.search(r'/p/([A-Za-z0-9]+)', ru.get('link', '') or '')
                if m:
                    article_id = m.group(1)

        if not _nonzero(channel_id):
            channel_id = self._lookup_channel_id(article_id)

        return article_id, channel_id

    def _lookup_channel_id(self, article_id):
        """从同活动的任务列表里找同文章的非零 channel_id"""
        if not article_id:
            return None
        try:
            aid = self.api.get_activity_id()
            if not aid:
                return None
            for t in (self.api.get_task_list(aid) or []):
                t_art = t.get('article_id')
                t_ch = t.get('channel_id')
                try:
                    same = str(t_art) == str(article_id) and int(t_ch or 0) != 0
                except (TypeError, ValueError):
                    same = False
                if same:
                    return t_ch
        except Exception:
            pass
        return None

    def execute_interactive_task(self, task):
        """执行浏览文章任务"""
        event_type = task.get('task_event_type', '')
        task_id = task.get('task_id', '')

        if event_type in ('interactive.view.article', 'view.article'):
            article_id, channel_id = self._resolve_article_ref(task)
            cid = channel_id or self.api.get_article_channel_id(article_id)
            if not cid or not article_id:
                logger.error("    ❌ 缺少 article_id/channel_id，浏览任务失败")
                return False
            return self.api.view_article_task(task_id, article_id, cid) is not None

        return False

    def execute_follow_task(self, count: int):
        """
        执行关注任务：关注后立即取消关注
        Returns: {'success': n, 'fail': n}
        """
        success = fail = 0
        user_data = self.api.get_follow_user_list()
        if not user_data:
            logger.warning("    ⚠️  获取关注用户列表失败")
            return {'success': 0, 'fail': 1}

        rows = user_data.get('rows') or []
        if not rows:
            logger.warning("    ⚠️  关注用户列表为空")
            return {'success': 0, 'fail': 1}

        processed = 0
        for row in rows:
            if processed >= count:
                break
            name = row.get('article_title', '')
            uid = row.get('keyword_id', '')
            if not name or not uid:
                continue
            if self.api.follow_user(name, uid):
                time.sleep(2)
                if self.api.unfollow_user(name, uid):
                    success += 1
                else:
                    fail += 1
            else:
                fail += 1
            processed += 1
            if processed < count:
                time.sleep(3)
        return {'success': success, 'fail': fail}


class SmzdmTaskManager:
    """封装众测 + 互动两个模块"""

    def __init__(self, api: SmzdmAPI, account_name: str):
        self.api = api
        self.account_name = account_name
        self.service = SmzdmService(api)

    def process_zhongce_tasks(self):
        logger.info(f"\n{'='*60}")
        logger.info("🎯 开始处理众测任务模块")
        logger.info(f"{'='*60}")
        stats = new_stats()
        try:
            energy = self.api.get_user_energy_info()
            if energy:
                self.service.print_energy_info(energy)

            activity_id = self.api.get_activity_id()
            if activity_id is None:
                logger.error("❌ 获取众测活动ID失败（请求异常）")
                stats['fatal'] = '获取众测活动ID失败'
                return stats
            if not activity_id:
                # 请求成功但没有活动 ID：当期活动未上线或已结束，属正常情况
                logger.info("ℹ️  今日没有可参与的众测活动")
                stats['unavailable'] = '今日无众测活动'
                return stats
            logger.info(f"✅ 活动ID: {activity_id}")

            tasks = self.api.get_task_list(activity_id)
            if tasks is None:
                logger.error("❌ 获取众测任务列表失败")
                stats['fatal'] = '获取众测任务列表失败'
                return stats
            if not tasks:
                logger.info("ℹ️  今日没有众测任务")
                return stats

            logger.info(f"📊 众测任务列表 (共 {len(tasks)} 个):")
            for task in tasks:
                self.print_task_info(task)

            logger.info("🔧 开始执行众测任务...")
            for task in tasks:
                task_name = task.get('task_name', '未知任务')
                task_id = task.get('task_id', '')
                task_status = to_int(task.get('task_status'))
                if task_status == 4:
                    stats['skip'] += 1
                    continue
                # 已完成待领取：当场领掉并记录奖励名，交给收尾领取会因状态
                # 变成 4 而查不到，导致「今日获得」被误报为「没有奖励」。
                if task_status == 3:
                    reward_text = task_reward_text(task)
                    logger.info(f"  💰 任务 [{task_name}] 已完成，尝试领取奖励...")
                    try:
                        if self.api.receive_reward(task_id):
                            stats['success'] += 1
                            if task_id:
                                stats['claimed_ids'].add(str(task_id))
                            stats['claimed_rewards'].append(reward_text or task_name)
                            logger.info(f"    ✅ 奖励领取成功: {reward_text}")
                        else:
                            stats['fail'] += 1
                            logger.warning(f"    ❌ [{task_name}] 奖励领取失败")
                    except Exception as e:
                        stats['fail'] += 1
                        logger.error(f"    ❌ 领取 [{task_name}] 奖励异常: {e}")
                    time.sleep(1)
                    continue
                try:
                    result = self.service.execute_task(task)
                    if result is None:
                        stats['skip'] += 1
                    elif result:
                        stats['success'] += 1
                    else:
                        stats['fail'] += 1
                    if RANDOM_SIGNIN:
                        delay_time = random.uniform(10, 15)
                        logger.info(f"[{self.account_name}] 任务 [{task_name}] 执行结束，"
                                    f"等待 {delay_time:.2f} 秒...")
                        time.sleep(delay_time)
                except Exception as e:
                    logger.error(f"    ❌ 执行任务 [{task_name}] 时发生异常: {e}")
                    stats['fail'] += 1

            logger.info("📊 众测任务执行统计:")
            logger.info(f"    ✅ 成功: {stats['success']} 个")
            logger.info(f"    ⚠️  失败: {stats['fail']} 个")
            logger.info(f"    ⏭️  跳过: {stats['skip']} 个")

            logger.info("💰 检查并领取众测任务奖励...")
            stats['reward'] = self.claim_task_rewards(activity_id, stats['claimed_ids'])
            return stats
        except Exception as e:
            logger.error(f"❌ 处理众测任务时发生错误: {e}", exc_info=True)
            stats['fatal'] = f'众测模块异常: {e}'
            return stats

    def print_task_info(self, task):
        task_name = task.get('task_name', '未知任务')
        task_status = to_int(task.get('task_status'))
        task_finished = task.get('task_finished_num', 0)
        task_total = task.get('task_even_num', 0)

        status_map = {
            0: "⚪ 未开始", 1: "🔵 进行中", 2: "🟡 未完成",
            3: "🟢 已完成", 4: "✅ 已领取",
        }
        status_text = status_map.get(task_status, "❓ 未知")

        # 复用 task_reward_text：它对非 dict 元素做了过滤，
        # 这里再单独拼一次会在脏数据上抛 AttributeError，把整个众测模块打成 fatal
        reward_text = task_reward_text(task)
        logger.info(f"    📋 {task_name}: {status_text} ({task_finished}/{task_total})"
                    + (f" 🎁 {reward_text}" if reward_text else ""))

    def claim_task_rewards(self, activity_id, exclude_ids=None):
        """领取众测任务奖励，返回 {'claimed','failed','rewards','ok'}。

        ok=False 表示没取到任务列表、无法判断，不能据此说「没有可领奖励」；exclude_ids 防重复领取。
        """
        claim = {'claimed': 0, 'failed': 0, 'rewards': [], 'ok': True}
        exclude_ids = exclude_ids or set()
        try:
            tasks = self.api.get_task_list(activity_id)
            if tasks is None:
                logger.warning("    ⚠️  重新获取任务列表失败，无法领取奖励")
                claim['ok'] = False
                return claim
            if not tasks:
                logger.info("    ℹ️  今日没有众测任务")
                return claim
            claimable = [t for t in tasks
                         if to_int(t.get('task_status')) == 3
                         and str(t.get('task_id')) not in exclude_ids]
            if not claimable:
                logger.info("    ℹ️  没有可领取的任务奖励")
                return claim
            logger.info(f"    🎁 发现 {len(claimable)} 个可领取奖励的任务")
            claimed = failed = 0
            for task in claimable:
                task_id = task.get('task_id', '')
                task_name = task.get('task_name', '未知任务')
                reward_text = task_reward_text(task)
                if self.api.receive_reward(task_id):
                    claimed += 1
                    logger.info(f"      ✅ [{task_name}] 奖励领取成功: {reward_text}")
                    claim['rewards'].append(reward_text or task_name)
                else:
                    failed += 1
                    logger.warning(f"      ❌ [{task_name}] 奖励领取失败")
                time.sleep(1)
            if claimed or failed:
                logger.info(f"  📊 奖励领取结果: 成功 {claimed} 个, 失败 {failed} 个")
            claim.update({'claimed': claimed, 'failed': failed})
            return claim
        except Exception as e:
            logger.error(f"    ❌ 领取奖励过程中发生错误: {e}")
            claim['ok'] = False
            return claim

    def process_interactive_tasks(self):
        logger.info(f"\n{'='*60}")
        logger.info("🎯 开始处理互动任务模块")
        logger.info(f"{'='*60}")
        stats = new_stats()
        try:
            task_data = self.api.get_interactive_task_list()
            if task_data is None:
                logger.error("❌ 获取互动任务列表失败")
                stats['fatal'] = '获取互动任务列表失败'
                return stats

            rows = task_data.get('rows', [])
            if rows:
                cell_data = rows[0].get('cell_data', {})
                reward_status = cell_data.get('activity_reward_status', 0)
                activity_id = cell_data.get('activity_id', '')
                if str(reward_status) == '1' and activity_id:
                    logger.info("🎁 检测到活动阶段性奖励可领取...")
                    robot_token = self.api.get_robot_token()
                    if robot_token and self.api.activity_task_receive(activity_id, robot_token):
                        stats['stage_reward'] = True
                        logger.info("    ✅ 活动阶段性奖励领取成功")
                    else:
                        stats['stage_reward'] = False
                        logger.warning("    ❌ 活动阶段性奖励领取失败")

            tasks = self.service.parse_interactive_tasks(task_data)
            if not tasks:
                logger.info("ℹ️  今日没有可执行的互动任务（可能已全部完成）")
                return stats

            logger.info(f"📊 互动任务列表 (共 {len(tasks)} 个):")
            logger.info("🔧 开始执行互动任务...")

            for task in tasks:
                task_id = task.get('task_id', '')
                task_name = task.get('task_name', '未知任务')
                task_status = to_int(task.get('task_status'))
                event_type = task.get('task_event_type', '')

                if task_status == 4:
                    logger.info(f"  ⏭️  任务 [{task_name}] 已领取奖励,跳过")
                    stats['skip'] += 1
                    continue

                if task_status == 3:
                    reward_text = task_reward_text(task)
                    logger.info(f"  💰 任务 [{task_name}] 已完成,尝试领取奖励...")
                    try:
                        if self.api.receive_reward(task_id):
                            logger.info(f"    ✅ 任务 [{task_name}] 奖励领取成功: {reward_text}")
                            stats['success'] += 1
                            if task_id:
                                stats['claimed_ids'].add(str(task_id))
                            stats['claimed_rewards'].append(reward_text or task_name)
                        else:
                            logger.warning(f"    ❌ 任务 [{task_name}] 奖励领取失败")
                            stats['fail'] += 1
                    except Exception as e:
                        logger.error(f"    ❌ 领取任务 [{task_name}] 奖励异常: {e}")
                        stats['fail'] += 1
                    time.sleep(1)
                    continue

                try:
                    logger.info(f"  🔨 开始执行任务: [{task_name}] (类型: {event_type})")
                    if event_type in ("interactive.view.article", "view.article"):
                        ok = self.service.execute_interactive_task(task)
                        logger.info(f"    {'✅ 任务执行成功' if ok else '❌ 任务执行失败'} [{task_name}]")
                        stats['success' if ok else 'fail'] += 1

                    elif event_type in ("interactive.follow.user", "follow.user"):
                        logger.info(f"    📌 任务 [{task_name}] 类型为关注用户，开始执行关注任务")
                        finished = int(task.get('task_finished_num', 0) or 0)
                        total = int(task.get('task_even_num', 0) or 0)
                        remaining = total - finished
                        if remaining <= 0:
                            logger.info(f"    ✅ 任务 [{task_name}] 已完成所有关注 "
                                        f"({finished}/{total})")
                            stats['success'] += 1
                            continue
                        result = self.service.execute_follow_task(min(remaining, 5))
                        if result['success'] > 0:
                            stats['success'] += 1
                            logger.info(f"    ✅ 任务 [{task_name}] 执行成功")
                            # 关注成功但取关失败等部分失败单独计数
                            stats['fail'] += result.get('fail', 0)
                        else:
                            stats['fail'] += 1
                            logger.info(f"    ❌ 任务 [{task_name}] 执行失败")

                    elif event_type in ("interactive.favorite", "interactive.favorite.article"):
                        logger.info(f"    📌 任务 [{task_name}] 类型为收藏文章")
                        aid, ch = self.service._resolve_article_ref(task)
                        ok = self.api.favorite_article_task(task_id, aid, ch)
                        logger.info(f"    {'✅ 任务执行成功' if ok else '❌ 任务执行失败'} [{task_name}]")
                        stats['success' if ok else 'fail'] += 1

                    elif event_type in ("interactive.rating", "interactive.rating.article"):
                        logger.info(f"    📌 任务 [{task_name}] 类型为点赞文章")
                        aid, ch = self.service._resolve_article_ref(task)
                        ok = self.api.rating_article_task(task_id, aid, ch)
                        logger.info(f"    {'✅ 任务执行成功' if ok else '❌ 任务执行失败'} [{task_name}]")
                        stats['success' if ok else 'fail'] += 1

                    elif event_type == "interactive.comment":
                        logger.info(f"    ⏭️  任务 [{task_name}] 类型为评论，暂不支持自动执行，跳过")
                        stats['skip'] += 1

                    elif event_type in ("publish.baoliao_new", "publish.biji_new",
                                        "publish.yuanchuang_new", "publish.zhuanzai"):
                        logger.info(f"    ⏭️  任务 [{task_name}] 类型为发布内容，暂不支持自动执行，跳过")
                        stats['skip'] += 1

                    else:
                        logger.info(f"    ⏭️  未知任务类型: {event_type}，跳过")
                        stats['skip'] += 1

                    time.sleep(2)
                except Exception as e:
                    logger.error(f"    ❌ 执行任务 [{task_name}] 时发生异常: {e}")
                    stats['fail'] += 1

            logger.info("📊 互动任务执行统计:")
            logger.info(f"    ✅ 成功: {stats['success']} 个")
            logger.info(f"    ⚠️  失败: {stats['fail']} 个")
            logger.info(f"    ⏭️  跳过: {stats['skip']} 个")

            logger.info("💰 重新获取互动任务状态并领取奖励...")
            stats['reward'] = self.claim_interactive_task_rewards(stats['claimed_ids'])
            return stats
        except Exception as e:
            logger.error(f"❌ 处理互动任务时发生错误: {e}", exc_info=True)
            stats['fatal'] = f'互动模块异常: {e}'
            return stats

    def claim_interactive_task_rewards(self, exclude_ids=None):
        """领取互动任务奖励，返回 {'claimed','failed','rewards','ok'}。

        ok=False 表示没能取到任务列表，无法判断是否有奖励。
        exclude_ids 为本轮执行阶段已领掉的 task_id，避免重复领取。
        """
        claim = {'claimed': 0, 'failed': 0, 'rewards': [], 'ok': True}
        exclude_ids = exclude_ids or set()
        try:
            task_data = self.api.get_interactive_task_list()
            if task_data is None:
                logger.warning("    ⚠️  重新获取互动任务列表失败，无法领取奖励")
                claim['ok'] = False
                return claim
            tasks = self.service.parse_interactive_tasks(task_data)
            if not tasks:
                logger.info("    ℹ️  当前没有互动任务")
                return claim
            claimable = [t for t in tasks
                         if to_int(t.get('task_status')) == 3
                         and str(t.get('task_id')) not in exclude_ids]
            if not claimable:
                logger.info("    ℹ️  没有可领取的互动任务奖励")
                return claim
            logger.info(f"    🎁 发现 {len(claimable)} 个可领取奖励的互动任务")
            claimed = failed = 0
            for task in claimable:
                task_id = task.get('task_id', '')
                task_name = task.get('task_name', '未知任务')
                reward_text = task_reward_text(task)
                if self.api.receive_reward(task_id):
                    claimed += 1
                    logger.info(f"      ✅ [{task_name}] 奖励领取成功: {reward_text}")
                    claim['rewards'].append(reward_text or task_name)
                else:
                    failed += 1
                    logger.warning(f"      ❌ [{task_name}] 奖励领取失败")
                time.sleep(1)
            if claimed or failed:
                logger.info(f"  📊 互动任务奖励领取结果: 成功 {claimed} 个, 失败 {failed} 个")
            claim.update({'claimed': claimed, 'failed': failed})
            return claim
        except Exception as e:
            logger.error(f"    ❌ 领取互动任务奖励过程中发生错误: {e}")
            claim['ok'] = False
            return claim


def run_account_tasks(cookie, index):
    """
    单账号活动：众测 → 互动
    Returns: (msg, ok, stats)
    """
    print(f"\n==== 开始第{index}个帐号活动任务 ====")
    stats = {'zhongce': None, 'interactive': None}
    started = time.time()

    try:
        api = SmzdmAPI(cookie)
        try:
            manager = SmzdmTaskManager(api, f"第{index}个账号")

            do_zhongce = TASK_MODULES in ('both', 'zhongce')
            do_interactive = TASK_MODULES in ('both', 'interactive')

            if do_zhongce:
                stats['zhongce'] = manager.process_zhongce_tasks()

            if do_zhongce and do_interactive and RANDOM_SIGNIN:
                delay_time = random.uniform(10, 15)
                logger.info(f"[第{index}个账号] 模块切换，等待 {delay_time:.2f} 秒...")
                time.sleep(delay_time)

            if do_interactive:
                stats['interactive'] = manager.process_interactive_tasks()

            # 组装消息
            lines = [f"👤 第{index}个账号"]
            modules = [('众测任务', stats['zhongce']), ('互动任务', stats['interactive'])]
            for label, m in modules:
                if not m:
                    continue
                if m.get('fatal'):
                    lines.append(f"❌ {label}: 模块失败（{m['fatal']}）")
                elif m.get('unavailable'):
                    lines.append(f"ℹ️ {label}: {m['unavailable']}")
                elif m.get('success') or m.get('fail') or m.get('skip'):
                    lines.append(f"🎯 {label}: ✅{m.get('success', 0)} "
                                 f"⚠️{m.get('fail', 0)} ⏭️{m.get('skip', 0)}")
                else:
                    lines.append(f"🎯 {label}: 今日无任务")
            if not any(m for _, m in modules):
                lines.append("⚠️  未执行任何任务模块")

            # 今日获得 = 执行阶段当场领到的 + 收尾统一领到的
            claimed_names, claimed_count, failed_count = [], 0, 0
            stage_reward, claim_unknown = None, False
            for _, m in modules:
                if not m:
                    continue
                exec_rewards = m.get('claimed_rewards') or []
                claimed_names += exec_rewards
                claimed_count += len(exec_rewards)
                if m.get('stage_reward') is not None:
                    stage_reward = m.get('stage_reward')
                reward = m.get('reward')
                if reward:
                    claimed_count += reward.get('claimed', 0)
                    failed_count += reward.get('failed', 0)
                    claimed_names += reward.get('rewards') or []
                    if not reward.get('ok', True):
                        claim_unknown = True

            if stage_reward is True:
                lines.append("🎁 阶段性奖励: 已领取")
            elif stage_reward is False:
                lines.append("🎁 阶段性奖励: 领取失败")

            names = list(dict.fromkeys(n for n in claimed_names if n))
            if claimed_count or names:
                # 名称已去重，数量也用去重后的，避免「显示 1 个名字却说 2 项」
                total = len(names) or claimed_count
                if names:
                    shown = '、'.join(names[:5])
                    more = f" 等 {total} 项" if total > 5 else ""
                    lines.append(f"🎁 今日获得: {shown}{more}")
                else:
                    lines.append(f"🎁 今日获得: 已领取 {total} 项（接口未返回名称）")
            elif claim_unknown:
                lines.append("🎁 今日获得: 未知（任务列表获取失败）")
            elif failed_count:
                lines.append(f"🎁 今日获得: 无（{failed_count} 项领取失败）")
            else:
                lines.append("🎁 今日获得: 无")

            lines.append("")
            lines.append(f"🕐 完成时间: {fmt_clock()}｜耗时 {fmt_elapsed(time.time() - started)}")

            msg = "\n".join(lines)
            # 只要有模块整体失败（fatal），账号就判失败：
            # 否则「Cookie 失效 / 活动 ID 拿不到」会被一句「活动完成」盖住。
            ran = [m for _, m in modules if m]
            ok = bool(ran) and not any(m.get('fatal') for m in ran)
            print(f"{'✅ 活动任务完成' if ok else '❌ 活动任务失败'}")
            print(msg)
            return msg, ok, stats

        finally:
            api.close()

    except requests.exceptions.Timeout:
        error_msg = (f"❌ 账号{index}: 请求超时，网络连接可能有问题\n"
                     f"⏱️ 耗时 {fmt_elapsed(time.time() - started)}")
        print(error_msg)
        return error_msg, False, stats
    except requests.exceptions.ConnectionError:
        error_msg = (f"❌ 账号{index}: 网络连接错误，无法连接到服务器\n"
                     f"⏱️ 耗时 {fmt_elapsed(time.time() - started)}")
        print(error_msg)
        return error_msg, False, stats
    except Exception as e:
        error_msg = (f"❌ 账号{index}: 活动任务异常 - {str(e)}\n"
                     f"⏱️ 耗时 {fmt_elapsed(time.time() - started)}")
        print(error_msg)
        return error_msg, False, stats


def main():
    """入口，固定五段：依赖 → 配置 → 延迟 → 逐账号执行 → 汇总通知。

    模块配置与 Cookie 都是「配置」，必须在随机延迟之前校验完 ——
    上一版把延迟写在 Cookie 校验前面，配置错了还要先干等十几分钟。
    """
    if not require_dep("requests", PLATFORM):
        return EXIT_CONFIG

    if TASK_MODULES == "__invalid__":
        return notify_config_error(
            PLATFORM, "模块配置错误",
            f"SMZDM_TASK_MODULES={_TASK_MODULES_RAW!r} 无效",
            hints=[f"📝 可选值：{' / '.join(TASK_MODULE_CHOICES)}"])
    logger.info(f"📦 任务模块：{TASK_MODULES}")

    raw = env_str("SMZDM_COOKIE")
    if not raw:
        return notify_config_error(
            PLATFORM, "缺少 Cookie",
            "未找到 SMZDM_COOKIE 环境变量",
            hints=["🔑 与签到脚本共用该变量，配置一次即可"])

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
            result_msg, is_success, _ = run_account_tasks(cookie, i)
        except Exception as e:
            result_msg = f"处理异常：{type(e).__name__}: {e}"
            is_success = False
            logger.info(f"❌ 账号{i} {result_msg}")
        if is_success:
            success_count += 1
        details.append(f"{'✅' if is_success else '❌'} 账号{i}\n{str(result_msg).strip()}")
        account_delay(i, total, PLATFORM)

    extra = [f"🧩 任务模块：{TASK_MODULES}"]
    return notify_result(
        PLATFORM, total, success_count, total - success_count,
        extra=extra, details=details, elapsed=time.time() - run_start,
        footers=["🔑 与签到脚本共用 SMZDM_COOKIE；大量失败多为风控，建议错开 cron"])


if __name__ == "__main__":
    sys.exit(main())
