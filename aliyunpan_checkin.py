#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
cron: 0 0 * * *
new Env('阿里云盘签到')

阿里云盘每日签到（青龙单文件），支持多账号。

环境变量
  ALIYUN_REFRESH_TOKEN       必填，阿里云盘 refresh_token，多账号用换行分隔
  AUTO_UPDATE_TOKEN          可选，默认 true；签到后自动把刷新过的 token 写回青龙环境变量
  PRIVACY_MODE               可选，默认 true（脱敏）；置 false 在日志里显示完整 Token
  SHOW_TOKEN_IN_NOTIFICATION 可选，默认 false；置 true 时推送通知的正文包含 Token
  RANDOM_SIGNIN              可选，默认 true，延迟开关；置 false 关闭全部等待（启动 + 账号间）
  MAX_RANDOM_DELAY           可选，默认 600，启动随机延迟上限（秒）
"""

import json
import os
import sys
import requests
import urllib3
import random
import time
import subprocess
import sqlite3
import hashlib
from datetime import datetime, timedelta

urllib3.disable_warnings()

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

# ---------------- 通知内容辅助 ----------------
def fmt_elapsed(seconds):
    """把耗时秒数渲染成易读文本：1.2 秒 / 3 分 05 秒 / 1 小时 02 分 09 秒。"""
    total = max(0.0, float(seconds or 0))
    if total < 60:
        return f"{total:.1f} 秒"
    minutes, secs = divmod(int(round(total)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes:02d} 分 {secs:02d} 秒"
    return f"{minutes} 分 {secs:02d} 秒"


def fmt_clock(ts=None):
    """时间戳渲染为 YYYY-MM-DD HH:MM:SS。"""
    return datetime.fromtimestamp(ts if ts else time.time()).strftime('%Y-%m-%d %H:%M:%S')


def fmt_seconds_span(seconds):
    """把「有效秒数」渲染成 2.0 小时 / 30.0 天。"""
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

# 随机延迟配置
max_random_delay = int(os.getenv("MAX_RANDOM_DELAY", "600"))
random_signin = os.getenv("RANDOM_SIGNIN", "true").lower() == "true"

# 配置项
auto_update_token = os.getenv("AUTO_UPDATE_TOKEN", "true").lower() == "true"
privacy_mode = os.getenv("PRIVACY_MODE", "true").lower() == "true"  # 隐私模式
show_token_in_notification = os.getenv("SHOW_TOKEN_IN_NOTIFICATION", "false").lower() == "true"  # 通知中是否显示token

def mask_sensitive_data(data, data_type="token"):
    """脱敏处理敏感数据"""
    if not data:
        return "未知"
    
    if data_type == "token":
        if len(data) <= 10:
            return "*" * len(data)
        return f"{data[:6]}...{data[-4:]}"
    elif data_type == "phone":
        if len(data) >= 7:
            return f"{data[:3]}****{data[-4:]}"
        return "***"
    elif data_type == "email":
        if "@" in data:
            parts = data.split("@")
            username = parts[0]
            domain = parts[1]
            if len(username) <= 2:
                masked_username = "*" * len(username)
            else:
                masked_username = f"{username[:2]}{'*' * (len(username) - 2)}"
            return f"{masked_username}@{domain}"
        return "***@***.***"
    else:
        return str(data)

def generate_account_id(token):
    """生成账号唯一标识（用于区分多账号，不暴露真实信息）"""
    if not token:
        return "未知账号"
    # 使用token的MD5值前8位作为账号标识
    hash_obj = hashlib.md5(token.encode())
    return f"账号{hash_obj.hexdigest()[:8].upper()}"

def format_time_remaining(seconds):
    """格式化时间显示"""
    if seconds <= 0:
        return "立即执行"
    hours, minutes = divmod(seconds, 3600)
    minutes, secs = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分{secs}秒"
    elif minutes > 0:
        return f"{minutes}分{secs}秒"
    else:
        return f"{secs}秒"

def wait_with_countdown(delay_seconds, task_name):
    """带倒计时的随机延迟等待"""
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

def update_qinglong_env_database(var_name, new_value, old_value=None):
    """通过数据库直接更新青龙面板环境变量

    多账号安全：环境变量里可能用换行存放多个 token。本函数只做
    「读-定位-替换」：若给了 old_value，则在原值中把 old_value 那一行
    替换掉，其余行原样保留；找不到 old_value 时才整体覆盖。
    """
    conn = None
    try:
        print("🔍 尝试通过数据库更新青龙面板环境变量...")
        
        # 青龙面板数据库路径
        db_paths = [
            "/ql/data/db/database.sqlite",
            "/ql/db/database.sqlite",
            "/ql/data/database.sqlite"
        ]
        
        db_path = None
        for path in db_paths:
            if os.path.exists(path):
                db_path = path
                print(f"📍 找到数据库文件: {path}")
                break
        
        if not db_path:
            print("❌ 未找到青龙面板数据库文件")
            return False
        
        # 连接数据库
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 先查询表结构
        cursor.execute("PRAGMA table_info(envs)")
        columns = [column[1] for column in cursor.fetchall()]
        
        # 查询现有环境变量（按 id 定位，避免同名多行被一起改）
        cursor.execute(
            "SELECT id, value FROM envs WHERE name = ? ORDER BY id LIMIT 1",
            (var_name,),
        )
        existing_env = cursor.fetchone()
        
        if existing_env:
            print(f"🔄 更新现有环境变量: {var_name}")
            # 按 id 精确更新单行，且做「读-定位-替换」
            value_to_write = merge_multi_account_value(
                existing_env[1], new_value, old_value
            )
            if 'updated_at' in columns:
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute(
                    "UPDATE envs SET value = ?, updated_at = ? WHERE id = ?",
                    (value_to_write, current_time, existing_env[0]),
                )
            else:
                cursor.execute(
                    "UPDATE envs SET value = ? WHERE id = ?",
                    (value_to_write, existing_env[0]),
                )
        else:
            # 创建新环境变量
            print(f"➕ 创建新环境变量: {var_name}")
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            if 'updated_at' in columns and 'created_at' in columns:
                cursor.execute("""
                    INSERT INTO envs (name, value, created_at, updated_at, status) 
                    VALUES (?, ?, ?, ?, ?)
                """, (var_name, new_value, current_time, current_time, 1))
            else:
                # 简化版本，只插入必要字段
                cursor.execute("INSERT INTO envs (name, value) VALUES (?, ?)", 
                             (var_name, new_value))
        
        # 提交更改
        conn.commit()
        print(f"✅ 成功通过数据库更新环境变量 {var_name}")
        return True
        
    except Exception as e:
        print(f"❌ 数据库更新失败: {e}")
        return False
    finally:
        # 无论成功失败都关闭连接，避免泄漏
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def merge_multi_account_value(current_value, new_value, old_value=None):
    """环境变量多账号读-定位-替换。

    环境变量可能长这样（3 个账号，换行分隔）：
        tokenA
        tokenB
        tokenC

    当账号 B 刷新出 new_tokenB 时，只应替换第二行，得到：
        tokenA
        new_tokenB
        tokenC

    而不是把整个变量覆盖成 new_tokenB（旧逻辑的致命 bug）。

    参数:
        current_value: 环境变量当前完整值
        new_value:     新的单个 token
        old_value:     被替换的旧 token（用于定位）
    返回:
        替换后的完整值；若无法定位 old_value 则用 new_value 整体覆盖。
    """
    if not current_value:
        return new_value
    if not old_value:
        # 没有旧值可定位，退化：单账号场景直接覆盖
        return new_value

    lines = current_value.replace('\r\n', '\n').split('\n')
    replaced = False
    new_lines = []
    for line in lines:
        if not replaced and line.strip() == old_value.strip():
            new_lines.append(new_value)
            replaced = True
        else:
            new_lines.append(line)

    if replaced:
        print(f"✅ 已在多账号变量中定位并替换对应 token（共 {len(lines)} 个账号）")
        return '\n'.join(new_lines)

    # 找不到旧值：可能换了账号，追加而不是覆盖，避免丢号
    print("⚠️  未在现有变量中定位到旧 token，改为追加新值")
    return current_value.rstrip('\n') + '\n' + new_value


def update_qinglong_env_api(var_name, new_value, old_value=None):
    """通过青龙面板API更新环境变量"""
    try:
        print("🔍 尝试通过青龙面板API更新环境变量...")
        
        # 查找青龙面板配置文件
        config_paths = [
            "/ql/config/auth.json",
            "/ql/data/config/auth.json",
            "/ql/config/config.json"
        ]
        
        config_data = None
        for config_path in config_paths:
            if os.path.exists(config_path):
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        config_data = json.load(f)
                    print(f"📍 找到配置文件: {config_path}")
                    break
                except:
                    continue
        
        if not config_data:
            print("❌ 未找到青龙面板配置文件")
            return False
        
        # 获取API配置
        token = config_data.get('token') or config_data.get('auth', {}).get('token')
        if not token:
            print("❌ 配置文件中未找到token")
            return False
        
        # API基础URL
        api_base = "http://localhost:5700"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # 查询现有环境变量
        print("🔍 查询现有环境变量...")
        response = requests.get(f"{api_base}/api/envs", headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"❌ 查询环境变量失败: {response.status_code}")
            return False
        
        envs_data = response.json()
        if not envs_data.get("code") == 200:
            print(f"❌ API返回错误: {envs_data}")
            return False
        
        existing_env = None
        for env in envs_data.get("data", []):
            if env.get("name") == var_name:
                existing_env = env
                break
        
        if existing_env:
            # 更新现有环境变量（同样做读-定位-替换，保住其他账号）
            print(f"🔄 更新现有环境变量: {var_name}")
            env_id = existing_env.get("id") or existing_env.get("_id")
            value_to_write = merge_multi_account_value(
                existing_env.get("value", ""), new_value, old_value
            )
            update_data = {
                "name": var_name,
                "value": value_to_write,
                "id": env_id
            }
            response = requests.put(f"{api_base}/api/envs", headers=headers, json=update_data, timeout=10)
        else:
            # 创建新环境变量
            print(f"➕ 创建新环境变量: {var_name}")
            create_data = {
                "name": var_name,
                "value": new_value
            }
            response = requests.post(f"{api_base}/api/envs", headers=headers, json=create_data, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("code") == 200:
                print(f"✅ 成功通过API更新环境变量 {var_name}")
                return True
            else:
                print(f"❌ API操作失败: {result}")
                return False
        else:
            print(f"❌ API请求失败: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ API更新失败: {e}")
        return False

def get_qinglong_env_value(var_name):
    """尝试读取青龙环境变量的完整值，读不到返回 None。

    青龙各版本的输出格式不一致，这里做多重兜底解析；
    只要不能确信拿到完整值，就返回 None 让调用方放弃自动更新。
    """
    cmds = [
        ['ql', 'envs', 'get', var_name],
        ['ql', 'envs', 'show', var_name],
    ]
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except Exception:
            continue
        if r.returncode != 0:
            continue
        out = (r.stdout or '').strip()
        if not out:
            continue
        # 尝试 JSON 解析：青龙新版返回 {"value": "..."} 或 [{"value": ...}]
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict) and 'value' in parsed:
                return parsed['value']
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                if 'value' in parsed[0]:
                    return parsed[0]['value']
        except Exception:
            pass
        # 退化：若输出中只有一行且不像"表格"，直接当作值
        lines = [ln for ln in out.split('\n') if ln.strip()]
        if len(lines) == 1 and var_name not in lines[0]:
            return lines[0].strip()
    return None


def update_qinglong_env_cmd(var_name, new_value, old_value=None):
    """使用ql命令更新环境变量"""
    try:
        print("🔍 尝试使用ql命令...")
        
        # 检查是否存在ql命令
        result = subprocess.run(['which', 'ql'], capture_output=True, text=True)
        if result.returncode != 0:
            print("⚠️ 未找到ql命令")
            return False
        
        # 先查询现有环境变量
        print("🔍 查询现有环境变量...")
        cmd_list = ['ql', 'envs', 'ls']
        result = subprocess.run(cmd_list, capture_output=True, text=True, timeout=15)

        env_exists = False
        if result.returncode == 0:
            # 检查输出中是否包含目标变量
            if var_name in result.stdout:
                env_exists = True
                print(f"📋 找到现有环境变量: {var_name}")

        if env_exists:
            # 关键安全护栏：ql envs ls 只列名字、读不到完整值，
            # 直接 `ql envs update` 会把整个变量覆盖成单个 token，
            # 导致多账号其余 token 丢失。这里在无法读取原值时**拒绝执行**。
            current_value = get_qinglong_env_value(var_name)
            if current_value is None:
                print("⚠️  无法安全读取现有环境变量值，为避免误删其他账号的 token，")
                print("    已跳过自动更新，请手动更新（或改用数据库/API 方式）。")
                return False

            print(f"🔄 更新现有环境变量: {var_name}")
            value_to_write = merge_multi_account_value(
                current_value, new_value, old_value
            )
            cmd_update = ['ql', 'envs', 'update', var_name, value_to_write]
            result = subprocess.run(cmd_update, capture_output=True, text=True, timeout=15)

            if result.returncode == 0:
                print(f"✅ 成功更新环境变量 {var_name}")
                return True
            else:
                print(f"❌ 更新失败: {result.stderr}")
                return False
        
        # 添加新环境变量
        print(f"➕ 添加环境变量: {var_name}")
        cmd_add = ['ql', 'envs', 'add', var_name, new_value]
        result = subprocess.run(cmd_add, capture_output=True, text=True, timeout=15)
        
        if result.returncode == 0:
            print(f"✅ 成功添加环境变量 {var_name}")
            return True
        else:
            print(f"❌ 添加失败: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        print("⚠️ ql命令执行超时")
        return False
    except Exception as e:
        print(f"⚠️ ql命令方法失败: {e}")
        return False

def update_environment_variable(var_name, new_value, old_value=None):
    """更新环境变量（支持多种环境）"""
    if not auto_update_token:
        print("🔧 自动更新Token功能已禁用")
        return False
    
    try:
        print(f"🔄 正在尝试自动更新环境变量 {var_name}...")
        
        # 检查是否在青龙面板环境
        if os.path.exists('/ql'):
            print("🐉 检测到青龙面板环境，尝试多种更新方式...")
            
            # 方法1: 数据库直接更新（最可靠）
            if update_qinglong_env_database(var_name, new_value, old_value):
                return True
            
            # 方法2: API更新
            if update_qinglong_env_api(var_name, new_value, old_value):
                return True
            
            # 方法3: ql命令更新
            if update_qinglong_env_cmd(var_name, new_value, old_value):
                return True
            
            print("❌ 所有青龙面板更新方式都失败了")
            return False
        
        # 检查是否在Docker环境
        elif os.path.exists('/.dockerenv'):
            return update_docker_env(var_name, new_value)
        
        # 其他环境（本地运行等）
        else:
            return update_local_env(var_name, new_value)
            
    except Exception as e:
        print(f"❌ 自动更新环境变量失败: {e}")
        return False

def update_docker_env(var_name, new_value):
    """Docker环境下的处理

    ⚠️ 注意：容器内的环境变量无法被脚本持久化修改（写在 /tmp 的文件
    下次启动就没了，也没有任何程序会去读它）。因此这里**不再谎报成功**，
    而是明确返回 False 并提示用户手动更新。
    """
    try:
        print("🐳 检测到Docker环境")
        print("⚠️  Docker 容器内无法自动持久化环境变量，自动更新不可用")
        if not privacy_mode:
            print(f"💡 请手动更新环境变量 {var_name}={new_value}")
        else:
            print(f"💡 请手动更新环境变量 {var_name}（明文已隐藏，可用 SHOW_TOKEN_IN_NOTIFICATION=true 查看）")
        return False
    except Exception as e:
        print(f"⚠️ Docker环境处理失败: {e}")
        return False

def update_local_env(var_name, new_value):
    """本地环境下的处理"""
    try:
        print("🏠 检测到本地环境...")
        
        # 尝试更新当前进程的环境变量
        os.environ[var_name] = new_value
        print(f"✅ 已更新当前进程的环境变量 {var_name}")
        
        # 尝试写入到 .env 文件
        env_files = ['.env', '.env.local', 'config.env']
        
        for env_file in env_files:
            if os.path.exists(env_file):
                try:
                    # 读取现有内容
                    with open(env_file, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    
                    # 更新或添加变量
                    updated = False
                    new_lines = []
                    
                    for line in lines:
                        stripped = line.strip()
                        prefix = 'export ' if stripped.startswith('export ') else ''
                        if stripped[len(prefix):].startswith(f'{var_name}='):
                            new_lines.append(f'{prefix}{var_name}={new_value}\n')
                            updated = True
                        else:
                            new_lines.append(line)
                    
                    if not updated:
                        new_lines.append(f'{var_name}={new_value}\n')
                    
                    # 写回文件
                    with open(env_file, 'w', encoding='utf-8') as f:
                        f.writelines(new_lines)
                    
                    print(f"✅ 已更新 {env_file} 文件")
                    return True
                    
                except Exception as e:
                    print(f"⚠️ 更新 {env_file} 失败: {e}")
                    continue
        
        # 只改了当前进程，进程退出即失效：不能当成功汇报，
        # 否则通知写「已自动写回」而下次运行仍用旧 token。
        print("⚠️ 未找到 .env 文件，仅更新了当前进程环境变量（重启后失效），"
              "请在面板里手动更新")
        return False
        
    except Exception as e:
        print(f"⚠️ 本地环境处理失败: {e}")
        return False

class AliYun:
    name = "阿里云盘"

    def __init__(self, refresh_token: str, index: int = 1):
        self.refresh_token = refresh_token
        self.index = index
        self.new_refresh_token = None
        self.account_id = generate_account_id(refresh_token)

        # 通知明细用的字段
        self.signed_at = None      # 签到成功时刻
        self.token_meta = {}       # access/refresh token 有效期信息
        self.reward_info = ""      # 今日奖励描述
        self.token_update_ok = None  # 写回结果：None=未尝试 / True=成功 / False=失败

    def update_token(self):
        """更新访问令牌"""
        try:
            print("🔄 正在更新访问令牌...")
            if privacy_mode:
                print(f"🔍 Token预览: {mask_sensitive_data(self.refresh_token, 'token')}")
            else:
                print(f"🔍 Token预览: {self.refresh_token[:20]}...{self.refresh_token[-10:]}")
            
            url = "https://auth.aliyundrive.com/v2/account/token"
            data = {"grant_type": "refresh_token", "refresh_token": self.refresh_token}
            
            response = requests.post(url=url, json=data, timeout=15)
            print(f"🔍 响应状态码: {response.status_code}")
            
            if response.status_code != 200:
                try:
                    error_detail = response.json()
                    error_msg = error_detail.get('message', '未知错误')
                    error_code = error_detail.get('code', 'UNKNOWN')
                    
                    if response.status_code == 400:
                        if 'InvalidParameter.RefreshToken' in str(error_detail):
                            return None, "refresh_token无效或已过期，请重新获取"
                        elif 'refresh_token' in str(error_detail).lower():
                            return None, "refresh_token格式错误或已失效"
                        else:
                            return None, f"请求参数错误: {error_msg}"
                    elif response.status_code == 401:
                        return None, "refresh_token已过期，需要重新登录获取"
                    else:
                        return None, f"HTTP {response.status_code}: {error_msg}"
                except:
                    return None, f"HTTP请求失败，状态码: {response.status_code}"
                    
            try:
                result = response.json()
            except:
                return None, "响应不是有效的JSON格式"
                
            access_token = result.get("access_token")
            new_refresh_token = result.get("refresh_token")

            # 记录有效期信息，供通知展示（服务端未返回时留空）
            self.token_meta = {
                "issued_at": time.time(),
                "access_expires_in": result.get("expires_in"),
                "refresh_expires_in": result.get("refresh_token_expires_in"),
            }

            if access_token:
                print("✅ 访问令牌更新成功")
                
                # 检查是否有新的refresh_token
                if new_refresh_token and new_refresh_token != self.refresh_token:
                    if privacy_mode:
                        print(f"🔄 检测到新的refresh_token: {mask_sensitive_data(new_refresh_token, 'token')}")
                    else:
                        print(f"🔄 检测到新的refresh_token: {new_refresh_token[:20]}...{new_refresh_token[-10:]}")
                    
                    self.new_refresh_token = new_refresh_token
                    
                    # 尝试自动更新环境变量
                    if auto_update_token:
                        print("🤖 正在尝试自动更新环境变量...")
                        success = update_environment_variable("ALIYUN_REFRESH_TOKEN", new_refresh_token, self.refresh_token)
                        self.token_update_ok = bool(success)

                        if success:
                            print("✅ 环境变量自动更新成功")
                            # 更新当前实例的token
                            self.refresh_token = new_refresh_token
                        else:
                            print("⚠️ 环境变量自动更新失败，请手动更新")
                            if not privacy_mode:
                                print(f"💡 请手动设置: ALIYUN_REFRESH_TOKEN={new_refresh_token}")
                    else:
                        print("💡 建议手动更新环境变量中的refresh_token为新值")
                        if not privacy_mode:
                            print(f"💡 新值: {new_refresh_token}")
                
                return access_token, None
            else:
                return None, f"响应中缺少access_token"
                
        except requests.exceptions.Timeout:
            return None, "请求超时，网络连接可能有问题"
        except requests.exceptions.ConnectionError:
            return None, "网络连接错误，无法连接到阿里云服务器"
        except Exception as e:
            return None, f"Token更新异常: {str(e)}"

    def get_user_info(self, access_token):
        """获取用户信息 + 存储空间（合并为一次请求）

        原来 get_user_info 用 user. 域、get_storage_info 用 api. 域，
        但两者打的是同一个 /v2/user/get 接口，返回体里同时包含
        user_name / nick_name / phone 和 personal_space_info。
        实测 api. 域才认 Bearer token（user. 域会报 MissingRequestHeader），
        因此统一走 api. 域一次拿全。

        Returns: (nick_name, display_phone, used_gb, total_gb)
        """
        try:
            print("👤 正在获取用户信息与存储空间...")
            url = "https://api.aliyundrive.com/v2/user/get"
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }

            response = requests.post(url=url, headers=headers, json={}, timeout=15)
            if response.status_code != 200:
                print(f"⚠️ 获取用户信息失败，状态码: {response.status_code}")
                return "未知用户", "", 0, 0

            result = response.json()

            # ---- 用户信息 ----
            user_name = result.get("user_name", "未知用户")
            nick_name = result.get("nick_name", user_name)
            phone = result.get("phone", "")
            display_phone = mask_sensitive_data(phone, "phone") if phone else ""

            print(f"👤 用户: {nick_name}")
            if display_phone:
                print(f"📱 手机: {display_phone}")

            # ---- 存储空间 ----
            personal_space = result.get("personal_space_info", {}) or {}
            used_size = personal_space.get("used_size", 0)
            total_size = personal_space.get("total_size", 0)

            used_gb = round(used_size / (1024**3), 2) if used_size > 0 else 0
            total_gb = round(total_size / (1024**3), 2) if total_size > 0 else 0

            if total_gb > 0:
                usage_percent = round((used_gb / total_gb) * 100, 1)
                print(f"💾 存储空间: {used_gb}GB / {total_gb}GB ({usage_percent}%)")

            return nick_name, display_phone, used_gb, total_gb

        except Exception as e:
            print(f"❌ 获取用户信息异常: {e}")
            return "未知用户", "", 0, 0

    def get_storage_info(self, access_token):
        """[已废弃] 兼容保留：单独获取存储空间。

        新代码请直接用 get_user_info()，它一次请求同时返回用户与存储信息。
        """
        _, _, used_gb, total_gb = self.get_user_info(access_token)
        return used_gb, total_gb

    def sign(self, access_token):
        """执行签到 + 领取奖励

        正确流程（阿里云盘官方 App 行为）：
          1. POST /v1/activity/sign_in_list   body {"_rx-s": "mobile"}
             —— 该请求本身会完成"今日签到"，返回本月累计天数 signInCount
          2. POST /v1/activity/sign_in_reward body {"signInDay": signInCount}
             —— 领取当日奖励
        注意：两个接口都必须带 `_rx-s: mobile`，否则服务端不认。

        Returns: (message:str, success:bool, reward_info:str)
                 三个返回值恒定，避免调用方解包失败。
        """
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        try:
            # ---------- 1. 签到 ----------
            print("📝 正在执行签到...")
            url = "https://member.aliyundrive.com/v1/activity/sign_in_list"
            response = requests.post(url=url, headers=headers,
                                     json={"_rx-s": "mobile"}, timeout=15)
            print(f"🔍 签到响应状态码: {response.status_code}")

            if response.status_code != 200:
                try:
                    error_msg = response.json().get("message", f"HTTP {response.status_code}")
                except Exception:
                    error_msg = f"签到请求失败，HTTP状态码: {response.status_code}"
                return error_msg, False, ""

            try:
                result = response.json()
            except Exception:
                return "签到响应不是有效JSON", False, ""

            # 业务层校验：拿到 result.signInCount 才算签到接口正常
            sign_result = result.get("result") or {}
            sign_days = sign_result.get("signInCount")
            if not result.get("success", False) or sign_days is None:
                error_msg = result.get("message", "签到失败（未返回 signInCount）")
                print(f"❌ 签到失败: {error_msg}")
                return error_msg, False, ""

            print(f"📅 本月累计签到: {sign_days}天")

            # ---------- 2. 领取奖励 ----------
            reward_info = self._claim_reward(access_token, sign_days, headers)

            # 如果领奖接口没给信息，退化为从签到日志里推断
            if not reward_info:
                reward_info = self._parse_reward_from_logs(sign_result)
            if not reward_info:
                reward_info = "首次签到完成" if sign_days == 1 else "签到完成"

            print(f"✅ 签到成功，累计{sign_days}天")
            self.signed_at = time.time()
            self.reward_info = reward_info
            return f"签到成功，累计{sign_days}天", True, reward_info

        except requests.exceptions.Timeout:
            return "签到请求超时", False, ""
        except requests.exceptions.ConnectionError:
            return "签到网络连接错误", False, ""
        except Exception as e:
            print(f"❌ 签到异常: {e}")
            return f"签到异常: {str(e)}", False, ""

    def _claim_reward(self, access_token, sign_days, headers):
        """领取当日签到奖励，返回奖励描述（失败返回 ""）

        说明：2026 年起 sign_in_reward 接口新增了「客户端版本」风控，
        非官方 App 客户端调用会返回 403 请升级最新客户端访问。
        这不是脚本 bug，而是服务端主动拒绝第三方领取。
        此时奖励信息会退化为从 sign_in_list 日志中解析，
        签到本身（sign_in_list）不受影响。
        """
        try:
            print("🎁 正在领取签到奖励...")
            url = ("https://member.aliyundrive.com/v1/activity/"
                   "sign_in_reward?_rx-s=mobile")
            response = requests.post(
                url=url,
                headers=headers,
                json={"signInDay": sign_days},
                timeout=15,
            )

            # 服务端版本风控：明确区分，不误导用户以为是登录失效
            if response.status_code == 403:
                try:
                    msg = response.json().get("message", "")
                except Exception:
                    msg = ""
                if "升级" in msg or "客户端" in msg:
                    print("ℹ️  领奖接口被服务端版本风控拒绝（非脚本问题，")
                    print("    签到本身已完成，奖励将无法通过接口领取）")
                else:
                    print(f"ℹ️  领奖被拒绝（HTTP 403）: {msg}")
                return ""

            if response.status_code != 200:
                print(f"⚠️  领奖失败，HTTP状态码: {response.status_code}")
                return ""

            body = response.json()
            if not body.get("success", False):
                print(f"⚠️  领奖失败: {body.get('message', '未知错误')}")
                return ""

            reward = body.get("result") or {}
            name = reward.get("name") or ""
            desc = reward.get("description") or ""
            # 有些账号发的是空间/延期卡，name 为空但 description 有值
            text = f"{name}{desc}".strip()
            if text:
                print(f"🎁 今日奖励: {text}")
                return text

            # 兜底：部分返回体用 rewardAmount / type
            amount = reward.get("rewardAmount") or 0
            rtype = reward.get("type") or ""
            if rtype or amount:
                text = f"{rtype} x{amount}" if amount else f"{rtype}"
                print(f"🎁 今日奖励: {text}")
                return text
            return ""
        except Exception as e:
            print(f"⚠️  领奖异常: {e}")
            return ""

    @staticmethod
    def _parse_reward_from_logs(sign_result):
        """从 signInLogs 中解析**今日**奖励（领奖接口无信息时的兜底）

        坑：signInLogs 是整月 31 天的记录，且 status='normal' 的不止今天
        （本月已签到的每一天都是 normal）。因此必须按"最后一条 normal"
        （即当天）来取，不能取第一条 —— 否则会把月初第 1 天的奖励
        误报成今日奖励。

        奖励是否到账以 rewardAmount 为准（isReward 在部分账号下恒为 False，
        不可靠，仅作参考）。
        """
        logs = sign_result.get("signInLogs") or []
        # 取「最后一条 normal」，即今日签到记录
        today_log = None
        for log in logs:
            if log.get("status") == "normal":
                today_log = log
        if not today_log:
            return ""

        reward_type = today_log.get("type", "") or ""
        reward_amount = today_log.get("rewardAmount", 0) or 0
        reward_obj = today_log.get("reward", {}) or {}

        if reward_type == "postpone":
            desc = f"延期卡 x{reward_amount}" if reward_amount else "延期卡"
        elif reward_type == "backupSpaceMb":
            desc = (f"备份空间 {reward_amount}MB" if reward_amount
                    else "备份空间")
        else:
            name = reward_obj.get("name") or ""
            sub = reward_obj.get("description") or ""
            if name or sub:
                desc = f"{name}{sub}"
            elif reward_amount:
                desc = f"{reward_type} x{reward_amount}"
            else:
                desc = reward_type

        if not desc:
            return ""
        # rewardAmount=0 通常表示该项本身不发实物奖励（如"备份空间"占位）
        if reward_amount <= 0:
            return f"{desc}（无额外奖励）"
        return desc

    def token_lines(self):
        """凭证有效期行：仅在有实际时长时输出，避免冗长说明。"""
        meta = self.token_meta or {}
        access_span = fmt_seconds_span(meta.get("access_expires_in"))
        refresh_span = fmt_seconds_span(meta.get("refresh_expires_in"))
        parts = []
        if access_span:
            parts.append(f"access {access_span}（自动刷新）")
        parts.append(f"refresh {refresh_span}" if refresh_span else "refresh 长期有效")
        return ["🔑 " + "｜".join(parts)]

    def main(self):
        """主执行函数"""
        started = time.time()
        print(f"\n==== 账号{self.index} 开始签到 ====")
        
        # 1. 更新Token
        access_token, error_msg = self.update_token()
        if not access_token:
            full_error_msg = f"""❌ Token 已失效: {error_msg}
⏱️ 耗时: {fmt_elapsed(time.time() - started)}
🔧 重新抓取: 网页版登录 https://www.aliyundrive.com → F12 → Application → Local Storage → 复制 refresh_token → 更新 ALIYUN_REFRESH_TOKEN"""
            
            print(f"❌ {full_error_msg}")
            return full_error_msg, False
        
        # 2. 获取用户信息 + 存储空间（合并为一次请求）
        user_name, display_phone, used_gb, total_gb = self.get_user_info(access_token)

        # 3. 执行签到（含领取奖励）
        sign_msg, is_success, reward_info = self.sign(access_token)
        elapsed = time.time() - started
        
        # 5. 组合结果消息（通知用）
        final_msg = f"👤 账号: {user_name}"
        
        if display_phone:
            final_msg += f"\n📱 手机: {display_phone}"
            
        if total_gb > 0:
            usage_percent = round((used_gb / total_gb) * 100, 1)
            final_msg += f"\n💾 存储: {used_gb}GB / {total_gb}GB ({usage_percent}%)"
            
        final_msg += f"""
📝 签到: {sign_msg}"""

        if reward_info:
            final_msg += f"\n🎁 今日获得: {reward_info}"
        elif is_success:
            final_msg += "\n🎁 今日获得: 无（接口未返回奖励）"
        else:
            final_msg += "\n🎁 今日获得: 无（签到未成功）"

        if self.signed_at:
            final_msg += f"\n🕐 签到时间: {fmt_clock(self.signed_at)}"
        else:
            final_msg += f"\n🕐 完成时间: {fmt_clock()}"

        final_msg += f"（耗时 {fmt_elapsed(elapsed)}）"

        # Token 更新状态：必须区分「写回成功」与「仅检测到新值」
        if self.new_refresh_token:
            if not auto_update_token:
                final_msg += "\n🔄 Token: 检测到新 token，请手动更新"
            elif self.token_update_ok:
                final_msg += "\n🔄 Token: 已自动写回"
            elif self.token_update_ok is False:
                final_msg += "\n🔄 Token: ⚠️ 写回失败，请手动更新 ALIYUN_REFRESH_TOKEN"
            else:
                final_msg += "\n🔄 Token: 检测到新 token，请手动更新"
            
            # 只在明确允许时显示token
            if show_token_in_notification:
                final_msg += f"\n💡 新token: {mask_sensitive_data(self.new_refresh_token, 'token')}"

        final_msg += "\n" + "\n".join(self.token_lines())
        
        print(f"{'✅ 签到成功' if is_success else '❌ 签到失败'}")
        return final_msg, is_success

def main():
    """主程序入口"""
    print(f"==== 阿里云盘签到开始 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")
    
    # 显示配置状态
    print(f"🤖 自动更新Token: {'已启用' if auto_update_token else '已禁用'}")
    print(f"🔒 隐私保护模式: {'已启用' if privacy_mode else '已禁用'}")
    print(f"🔑 通知显示Token: {'是' if show_token_in_notification else '否'}")
    
    # 随机延迟（整体延迟）
    if random_signin:
        delay_seconds = random.randint(0, max_random_delay)
        if delay_seconds > 0:
            signin_time = datetime.now() + timedelta(seconds=delay_seconds)
            print(f"🎲 随机延迟: {format_time_remaining(delay_seconds)}")
            wait_with_countdown(delay_seconds, "阿里云盘签到")
    
    # 获取refresh_token配置
    aliyun_tokens = os.getenv("ALIYUN_REFRESH_TOKEN", "")
    
    if not aliyun_tokens:
        error_msg = """❌ 未找到ALIYUN_REFRESH_TOKEN环境变量

🔧 获取refresh_token的方法:
1. 打开阿里云盘网页版: https://www.aliyundrive.com/
2. 登录您的账号  
3. 按F12打开开发者工具
4. 切换到Application标签页
5. 在左侧找到Local Storage → https://www.aliyundrive.com
6. 找到token项，复制refresh_token的值
7. 在青龙面板中添加环境变量ALIYUN_REFRESH_TOKEN

💡 提示: refresh_token通常很长，以字母开头"""
        
        print(error_msg)
        notify("阿里云盘签到失败", error_msg)
        return

    # 支持多账号（使用换行符分割，支持token中包含&符号）
    tokens = [token.strip() for token in aliyun_tokens.replace('\r\n', '\n').split('\n') if token.strip()]
    
    print(f"📝 共发现 {len(tokens)} 个账号")
    
    success_count = 0
    total_count = len(tokens)
    results = []
    run_start = time.time()
    
    for index, token in enumerate(tokens):
        try:
            # 账号间随机等待
            if index > 0 and random_signin:
                delay = random.uniform(10, 20)
                print(f"⏱️  随机等待 {delay:.1f} 秒后处理下一个账号...")
                time.sleep(delay)
            
            # 执行签到
            aliyun = AliYun(token, index + 1)
            result_msg, is_success = aliyun.main()
            
            if is_success:
                success_count += 1
            
            results.append({
                'index': index + 1,
                'success': is_success,
                'message': result_msg,
                'account_id': aliyun.account_id,
                'reward': aliyun.reward_info,
            })
            
            # 发送单个账号通知
            status = "成功" if is_success else "失败"
            title = f"阿里云盘账号{index + 1}签到{status}"
            
            notify(title, result_msg)
            
        except Exception as e:
            error_msg = (f"账号{index + 1}: 执行异常 - {str(e)}\n"
                         f"⏱️ 本次耗时: {fmt_elapsed(time.time() - run_start)}")
            print(f"❌ {error_msg}")

            # 保持 results 与账号数一致，避免汇总明细数量对不上
            results.append({
                'index': index + 1,
                'success': False,
                'message': error_msg,
                'account_id': f"账号{index + 1}",
                'reward': '',
            })

            title = f"阿里云盘账号{index + 1}签到失败"
            notify(title, error_msg)
    
    # 发送汇总通知
    if total_count > 1:
        total_elapsed = time.time() - run_start
        summary_msg = (f"📊 {total_count} 个账号｜✅ {success_count}｜"
                       f"❌ {total_count - success_count}｜"
                       f"成功率 {success_count/total_count*100:.0f}%\n"
                       f"⏱️ 耗时 {fmt_elapsed(total_elapsed)}")

        # 明细（最多 5 个账号）
        if len(results) <= 5:
            for result in results:
                status_icon = "✅" if result['success'] else "❌"
                line = f"\n{status_icon} 账号{result['index']}"
                if result.get('reward'):
                    line += f"：{result['reward']}"
                summary_msg += line
        
        notify("阿里云盘签到汇总", summary_msg)
    
    print(f"\n==== 阿里云盘签到完成 - 成功{success_count}/{total_count} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====")
    # 有账号失败时以非零退出，避免青龙把失败的运行显示成绿色
    if total_count and success_count < total_count:
        sys.exit(1)

if __name__ == "__main__":
    main()
