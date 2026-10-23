#!/usr/bin/python3
"""cron: 0 0 * * *
new Env('阿里云盘签到')

阿里云盘每日签到，多账号。依赖 requests、ql_common.py（需同目录）。

环境变量
  ALIYUN_REFRESH_TOKEN  必填，refresh_token，多账号换行分隔
  AUTO_UPDATE_TOKEN     默认 true，签到后回写刷新后的 token
  PRIVACY_MODE          默认 true 脱敏，false 显示完整 Token
  RANDOM_SIGNIN         默认 true，false 关闭全部延迟
  MAX_RANDOM_DELAY      默认 300，启动延迟上限（秒）
"""

import json
import os
import sys
try:
    import requests
except ImportError:  # 缺依赖不在这里崩，交由 main 的 require_dep 出结论
    requests = None
try:
    import urllib3
except ImportError:  # 同上：requests 没装好时 urllib3 多半也没有
    urllib3 = None
import time
import subprocess
import sqlite3
import hashlib
from datetime import datetime

try:
    from ql_common import (  # noqa: E402
        EXIT_CONFIG, account_delay, account_header, banner,
        env_bool, env_str, fmt_clock, fmt_elapsed, fmt_span, get_logger,
        notify_config_error, notify_result, report_duplicates, require_dep, split_accounts,
        split_lines, start_delay,
    )
except ImportError:  # 公共层没上传/没被订阅同步：给结论，不抛 Traceback
    print("❌ 缺少公共层 ql_common.py：它必须与本脚本放在同一目录")
    print("🔧 青龙「订阅」会同步整个仓库；手动上传时请把它一起传上去")
    sys.exit(2)

PLATFORM = "阿里云盘签到"
log = get_logger("aliyunpan_checkin")

# 写回被跳过（同名变量全被禁用）时的返回值：既不是成功也不是失败
WRITE_SKIPPED = "skipped"

if urllib3 is not None:
    urllib3.disable_warnings()


# 配置项（空值/1/yes/on 都能正确解析；裸 == "true" 在变量留空时会变成 False）
auto_update_token = env_bool("AUTO_UPDATE_TOKEN", True)
privacy_mode = env_bool("PRIVACY_MODE", True)  # 隐私模式


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


def update_qinglong_env_database(var_name, new_value, old_value=None):
    """通过数据库直接更新青龙面板环境变量。

    给 old_value 时只替换该行、其余行保留；找不到 old_value 才整体覆盖。
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

        cursor.execute(
            "SELECT id, value, status FROM envs WHERE name = ? ORDER BY id",
            (var_name,),
        )
        rows = cursor.fetchall()

        if rows:
            def _row_enabled(row):
                """青龙约定：status 为空或 0 视为启用；1 表示用户在面板里禁用了这条变量。"""
                return row[2] in (None, 0, '0')

            enabled_rows = [r for r in rows if _row_enabled(r)]
            disabled_count = len(rows) - len(enabled_rows)

            if not enabled_rows:
                # 同名变量全被禁用：青龙不会注入它们的值，这里也就不该动它们，
                # 否则等于把用户主动禁用的账号悄悄放回来。
                print(f"⚠️  {var_name} 的同名变量全部处于禁用状态，跳过写回（不会改动这些被禁用的变量）")
                conn.commit()
                return WRITE_SKIPPED

            print(f"🔄 更新现有环境变量: {var_name}")

            # 只合并「启用」的行，禁用行保持原样不动
            combined_lines = []
            for row in enabled_rows:
                combined_lines.extend(split_token_lines(row[1]))
            combined_value = '\n'.join(combined_lines)

            value_to_write = merge_multi_account_value(
                combined_value, new_value, old_value
            )

            if len(enabled_rows) > 1:
                print(f"⚠️  检测到 {len(enabled_rows)} 条启用的同名环境变量，已合并到第一条并清空其余条目")
                print("    （数据未删除，建议稍后在面板里手动删掉多余的那几条）")
            if disabled_count:
                print(f"ℹ️  另有 {disabled_count} 条同名变量处于禁用状态，已保留不动")

            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            main_id = enabled_rows[0][0]
            if 'updated_at' in columns:
                cursor.execute(
                    "UPDATE envs SET value = ?, updated_at = ? WHERE id = ?",
                    (value_to_write, current_time, main_id),
                )
            else:
                cursor.execute(
                    "UPDATE envs SET value = ? WHERE id = ?",
                    (value_to_write, main_id),
                )

            # 其余「启用」的同名行置空（不删除数据，只是不再重复注入）
            for row in enabled_rows[1:]:
                cursor.execute(
                    "UPDATE envs SET value = '' WHERE id = ?",
                    (row[0],),
                )
        else:
            # 创建新环境变量
            if new_value is None:
                # 删除模式下没有可删的东西
                print(f"ℹ️  未找到环境变量 {var_name}，无需清理")
                conn.commit()
                return True

            print(f"➕ 创建新环境变量: {var_name}")
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # 按实际表结构拼 INSERT，避免缺 remarks 等 NOT NULL 字段导致失败
            insert_cols = ['name', 'value']
            insert_vals = [var_name, new_value]
            if 'remarks' in columns:
                insert_cols.append('remarks')
                insert_vals.append('')
            if 'status' in columns:
                insert_cols.append('status')
                insert_vals.append(0)  # 青龙：0=启用，1=禁用，新建变量必须落成启用
            if 'created_at' in columns:
                insert_cols.append('created_at')
                insert_vals.append(current_time)
            if 'updated_at' in columns:
                insert_cols.append('updated_at')
                insert_vals.append(current_time)

            placeholders = ', '.join(['?'] * len(insert_cols))
            cursor.execute(
                f"INSERT INTO envs ({', '.join(insert_cols)}) VALUES ({placeholders})",
                insert_vals,
            )

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


def split_token_lines(value):
    """把环境变量值拆成有效 token 行（复用 ql_common.split_lines）。

    面板用 & 拼接同名多行变量，refresh_token 不含 &，故 & 与换行一起当分隔符是安全的。
    """
    return split_lines(value)


def merge_multi_account_value(current_value, new_value, old_value=None):
    """环境变量多账号「读-定位-替换」：只替换 old_value 那一行，其余保持原样。

    new_value 为 None 表示删除该行（清理重复 token）。定位不到旧 token 时，只剩一行就替换
    （避免 1 个账号变 2 个），多行才追加。输出统一去重：同一 token 只保留一份。

    参数：current_value 当前完整值；new_value 新 token 或 None；old_value 被替换/删除的旧 token。
    返回处理后的完整值（多行用换行连接）。
    """
    lines = split_token_lines(current_value)
    old = (old_value or '').strip()

    if new_value is None:
        if not old:
            return '\n'.join(lines)
        kept = [ln for ln in lines if ln != old]
        if len(kept) != len(lines):
            print(f"🧹 已从环境变量中移除重复 token（剩余 {len(kept)} 个账号）")
        else:
            print("ℹ️  环境变量中已无该重复 token，无需清理")
        return '\n'.join(kept)

    new = str(new_value).strip()
    if not new:
        return '\n'.join(lines)

    # 先对现有各种去重：同一个 token 出现两次 = 同一个账号，
    # 不先合并的话替换后两行就不相等了，去重会失效（又会变成 2 个账号）。
    unique_lines = []
    for ln in lines:
        if ln not in unique_lines:
            unique_lines.append(ln)
    if len(unique_lines) != len(lines):
        print(f"🧹 现有变量中有 {len(lines) - len(unique_lines)} 个重复 token，已合并")
    lines = unique_lines

    replaced = False
    out = []
    for line in lines:
        if not replaced and old and line == old:
            out.append(new)
            replaced = True
        else:
            out.append(line)

    if replaced:
        print(f"✅ 已在多账号变量中定位并替换对应 token（共 {len(lines)} 个账号）")
    elif not lines:
        out = [new]
    elif len(lines) == 1:
        # 单账号：旧值匹配不上（例如已被别的流程换过），直接替换，
        # 绝不能追加——追加正是「1 个账号变成 2 个账号」的元凶。
        print("⚠️  未匹配到旧 token，当前变量只有 1 行，直接替换（不追加）")
        out = [new]
    else:
        print("⚠️  未在现有变量中定位到旧 token，改为追加新值")
        out = lines + [new]

    deduped = []
    for ln in out:
        if ln not in deduped:
            deduped.append(ln)
    if len(deduped) != len(out):
        print(f"🧹 已去除 {len(out) - len(deduped)} 个重复 token（同一账号只保留一行）")
    return '\n'.join(deduped)


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
                except Exception:
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

        matched_envs = [env for env in envs_data.get("data", []) if env.get("name") == var_name]
        # 与数据库路径保持一致：面板里被禁用的同名变量不参与合并，也不清空，
        # 否则等于把用户主动禁用的账号重新放回来。
        # 青龙约定：status 为空或 0 为启用，1 为禁用。
        enabled_envs = [
            env for env in matched_envs
            if env.get("status") in (None, 0, "0")
        ]

        if matched_envs and not enabled_envs:
            print(f"⚠️  {var_name} 的同名变量全部处于禁用状态，跳过写回（不会改动这些被禁用的变量）")
            return WRITE_SKIPPED

        if enabled_envs:
            # 更新现有环境变量（读-定位-替换；同名多条时合并到第一条）
            print(f"🔄 更新现有环境变量: {var_name}")
            combined_lines = []
            for env in enabled_envs:
                combined_lines.extend(split_token_lines(env.get("value", "")))
            value_to_write = merge_multi_account_value(
                '\n'.join(combined_lines), new_value, old_value
            )

            env_id = enabled_envs[0].get("id") or enabled_envs[0].get("_id")
            update_data = {
                "name": var_name,
                "value": value_to_write,
                "id": env_id
            }
            response = requests.put(f"{api_base}/api/envs", headers=headers, json=update_data, timeout=10)

            # 其余「启用」的同名条目清空，避免同一账号被重复注入成多行
            for env in enabled_envs[1:]:
                extra_id = env.get("id") or env.get("_id")
                if extra_id is None:
                    continue
                print(f"🧹 清空多余的同名环境变量条目 id={extra_id}")
                requests.put(f"{api_base}/api/envs", headers=headers, json={
                    "name": var_name, "value": "", "id": extra_id
                }, timeout=10)
        else:
            # 创建新环境变量
            if new_value is None:
                print(f"ℹ️  未找到环境变量 {var_name}，无需清理")
                return True
            print(f"➕ 创建新环境变量: {var_name}")
            create_data = {
                "name": var_name,
                "value": new_value,
                "remarks": ""
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
        if new_value is None:
            print(f"ℹ️  未找到环境变量 {var_name}，无需清理")
            return True
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
            for updater in (update_qinglong_env_database,
                            update_qinglong_env_api,
                            update_qinglong_env_cmd):
                res = updater(var_name, new_value, old_value)
                if res is True:
                    return True
                if res == WRITE_SKIPPED:
                    return WRITE_SKIPPED

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
    """Docker 环境下的处理。

    容器内环境变量无法被脚本持久化修改（写 /tmp 下次启动即失效），故不再谎报成功，
    明确返回 False 并提示手动更新。
    """
    try:
        print("🐳 检测到Docker环境")
        print("⚠️  Docker 容器内无法自动持久化环境变量，自动更新不可用")
        if not privacy_mode:
            print(f"💡 请手动更新环境变量 {var_name}={new_value}")
        else:
            print(f"💡 请手动更新环境变量 {var_name}（明文已隐藏，可置 PRIVACY_MODE=false 查看）")
        return False
    except Exception as e:
        print(f"⚠️ Docker环境处理失败: {e}")
        return False


def update_local_env(var_name, new_value):
    """本地环境下的处理"""
    try:
        print("🏠 检测到本地环境...")

        # 尝试更新当前进程的环境变量（new_value 为 None 表示删除该行）
        if new_value is None:
            os.environ.pop(var_name, None)
            print(f"✅ 已从当前进程的环境变量中移除 {var_name}")
        else:
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
        # 否则日志显示「已自动写回」而下次运行仍用旧 token。
        print("⚠️ 未找到 .env 文件，仅更新了当前进程环境变量（重启后失效），"
              "请在面板里手动更新")
        return False

    except Exception as e:
        print(f"⚠️ 本地环境处理失败: {e}")
        return False


class AliYun:
    name = "阿里云盘"

    def __init__(self, refresh_token: str, index: int = 1, seen_accounts=None):
        self.refresh_token = refresh_token
        self.index = index
        self.new_refresh_token = None
        self.account_id = generate_account_id(refresh_token)
        # 本次运行已签到过的账号（手机号/用户名 → 首次出现的账号序号），
        # 用于识别「环境变量里两行其实是同一个账号」的重复配置
        self.seen_accounts = seen_accounts if seen_accounts is not None else {}

        # 明细展示用的字段
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
                except Exception:
                    return None, f"HTTP请求失败，状态码: {response.status_code}"

            try:
                result = response.json()
            except Exception:
                return None, "响应不是有效的JSON格式"

            access_token = result.get("access_token")
            new_refresh_token = result.get("refresh_token")

            # 记录有效期信息，供日志展示（服务端未返回时留空）
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
                    print("💾 新 token 已记录，签到结束后统一写回环境变量")

                return access_token, None
            else:
                return None, "响应中缺少access_token"

        except requests.exceptions.Timeout:
            return None, "请求超时，网络连接可能有问题"
        except requests.exceptions.ConnectionError:
            return None, "网络连接错误，无法连接到阿里云服务器"
        except Exception as e:
            return None, f"Token更新异常: {str(e)}"

    def persist_token(self):
        """签到结束后，把刷新出来的 refresh_token 写回环境变量。

        延迟写回的好处：先确认这个账号是不是重复配置，重复账号直接跳过，
        不会再去动环境变量，也就不会越写越多行。
        """
        if not self.new_refresh_token:
            return
        if not auto_update_token:
            print("💡 自动写回已禁用，请手动更新环境变量中的 refresh_token")
            if not privacy_mode:
                print(f"💡 新值: {self.new_refresh_token}")
            return

        print("🤖 正在尝试自动更新环境变量...")
        ok = update_environment_variable(
            "ALIYUN_REFRESH_TOKEN", self.new_refresh_token, self.refresh_token
        )
        self.token_update_ok = None if ok == WRITE_SKIPPED else bool(ok)

        if ok is True:
            print("✅ 环境变量自动更新成功")
            self.refresh_token = self.new_refresh_token
        elif ok == WRITE_SKIPPED:
            print("⚠️ 变量被禁用，未写回；请手动更新或启用该变量")
        else:
            print("⚠️ 环境变量自动更新失败，请手动更新")
            if not privacy_mode:
                print(f"💡 请手动设置: ALIYUN_REFRESH_TOKEN={self.new_refresh_token}")

    def get_user_info(self, access_token):
        """获取用户信息 + 存储空间（一次请求拿全）。

        user. 与 api. 打的是同一个 /v2/user/get，但只有 api. 域认 Bearer token。
        返回 (nick_name, display_phone, used_gb, total_gb)。
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

            user_name = result.get("user_name", "未知用户")
            nick_name = result.get("nick_name", user_name)
            phone = result.get("phone", "")
            display_phone = mask_sensitive_data(phone, "phone") if phone else ""

            print(f"👤 用户: {nick_name}")
            if display_phone:
                print(f"📱 手机: {display_phone}")

            personal_space = result.get("personal_space_info", {}) or {}
            used_size = personal_space.get("used_size", 0)
            total_size = personal_space.get("total_size", 0)

            used_gb = round(used_size / (1024**3), 2) if used_size > 0 else 0
            total_gb = round(total_size / (1024**3), 2) if total_size > 0 else 0

            return nick_name, display_phone, used_gb, total_gb

        except Exception as e:
            print(f"❌ 获取用户信息异常: {e}")
            return "未知用户", "", 0, 0


    def sign(self, access_token):
        """执行签到 + 领取奖励，返回 (message, success, reward_info)。

        先 POST /v1/activity/sign_in_list 完成签到（返回本月累计天数），再 POST sign_in_reward 领当日奖励；
        两个请求都必须带 `_rx-s: mobile`。
        """
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        try:
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
        """领取当日签到奖励，返回奖励描述（失败返回 ""）。

        2026 年起该接口有客户端版本风控，非官方 App 返回 403；此时退化为从 sign_in_list 日志解析，
        签到本身不受影响。
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
        """从 signInLogs 解析今日奖励（领奖接口无信息时的兜底）。

        整月记录里 status='normal' 的不止今天，必须取「最后一条 normal」，否则会把月初奖励报成今日；
        是否到账以 rewardAmount 为准（isReward 在部分账号恒为 False）。
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
        access_span = fmt_span(meta.get("access_expires_in"))
        refresh_span = fmt_span(meta.get("refresh_expires_in"))
        parts = []
        if access_span:
            parts.append(f"access {access_span}（自动刷新）")
        parts.append(f"refresh {refresh_span}" if refresh_span else "refresh 长期有效")
        return ["🔑 " + "｜".join(parts)]

    def main(self):
        """主执行函数

        Returns: (message:str, success:bool, skipped:bool)
                 skipped=True 表示这个账号和前面某个账号是同一个，已跳过。
        """
        started = time.time()
        print(f"\n==== 账号{self.index} 开始签到 ====")

        # 1. 更新Token
        access_token, error_msg = self.update_token()
        if not access_token:
            full_error_msg = f"""❌ Token 已失效: {error_msg}
⏱️ 耗时: {fmt_elapsed(time.time() - started)}
🔧 重新抓取: 网页版登录 https://www.aliyundrive.com → F12 → Application → Local Storage → 复制 refresh_token → 更新 ALIYUN_REFRESH_TOKEN"""

            print(f"❌ {full_error_msg}")
            return full_error_msg, False, False

        # 2. 获取用户信息 + 存储空间（合并为一次请求）
        user_name, display_phone, used_gb, total_gb = self.get_user_info(access_token)

        # 2.5 重复账号拦截：环境变量里有多行但指向同一个账号时，只签一次
        account_key = display_phone or user_name
        if account_key and account_key not in ("未知用户", "未知", "***"):
            first_index = self.seen_accounts.get(account_key)
            if first_index is not None:
                print(f"♻️  该账号与账号{first_index} 是同一个阿里云盘账号，跳过本次签到")

                # 顺手把这条多余的 token 从环境变量里删掉，下次运行就不会再重复
                cleaned = ""
                if auto_update_token:
                    if update_environment_variable("ALIYUN_REFRESH_TOKEN", None, self.refresh_token):
                        cleaned = "\n🧹 已从环境变量中移除这条重复 token"
                        print("✅ 已清理环境变量中的重复 token")
                    else:
                        print("⚠️  清理重复 token 失败，请在面板里手动删掉多余那一行")

                dup_msg = (f"♻️ 重复账号: 与 账号{first_index} 是同一个阿里云盘账号"
                           f"（{account_key}）\n"
                           f"📝 本次已跳过签到（不重复占用签到次数）"
                           f"{cleaned}\n"
                           f"💡 若只想保留一个账号，请把 ALIYUN_REFRESH_TOKEN "
                           f"改成只有一行 token")
                return dup_msg, True, True
            self.seen_accounts[account_key] = self.index

        # 3. 执行签到（含领取奖励）
        sign_msg, is_success, reward_info = self.sign(access_token)

        # 4. 签到结束再写回 token（重复账号已在上一步提前返回，不会写回）
        self.persist_token()

        elapsed = time.time() - started

        # 5. 组合结果消息（输出到日志）
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

        final_msg += "\n" + "\n".join(self.token_lines())

        # 结果直接打到日志
        print(final_msg)
        return final_msg, is_success, False


def main():
    """入口，固定五段：依赖 → 配置 → 延迟 → 逐账号执行 → 汇总通知。

    顺序不能换：先拦环境问题（依赖/配置），再花时间等随机延迟。
    配置错了却先睡十几分钟，是上一版被吐槽的点之一。
    """
    if not require_dep("requests", PLATFORM):
        return EXIT_CONFIG

    raw = env_str("ALIYUN_REFRESH_TOKEN")
    if not raw:
        return notify_config_error(
            PLATFORM, "缺少 Token",
            "未找到 ALIYUN_REFRESH_TOKEN 环境变量",
            hints=[
                "🔑 网页版登录 aliyundrive.com → F12 → Application → Local Storage",
                "💡 复制 refresh_token 的值，多账号每行一个",
                "📝 变量建了但值为空，同样会走到这里",
            ])

    tokens, dropped = split_accounts(raw)
    report_duplicates("ALIYUN_REFRESH_TOKEN", dropped)
    if not tokens:
        return notify_config_error(
            PLATFORM, "Token 为空",
            "ALIYUN_REFRESH_TOKEN 中没有任何有效 token（可能只有空白）",
            hints=["📝 重新粘贴一次，多个账号每行一个"])

    banner(PLATFORM, len(tokens))
    log(f"🤖 自动更新 Token：{'已启用' if auto_update_token else '已禁用'}")
    log(f"🔒 隐私保护：{'已启用' if privacy_mode else '已禁用'}")
    start_delay(PLATFORM)

    run_start = time.time()
    total = len(tokens)
    seen_accounts = {}
    results = []
    success_count = 0
    skipped_count = 0

    for index, token in enumerate(tokens, 1):
        account_header(index, f"账号{index}", total)
        try:
            account = AliYun(token, index, seen_accounts)
            message, is_success, skipped = account.main()
            account_id = account.account_id
        except Exception as e:
            message = f"执行异常：{type(e).__name__}: {e}"
            is_success, skipped = False, False
            account_id = f"账号{index}"
            log(f"❌ 账号{index} {message}")

        if skipped:
            skipped_count += 1
        elif is_success:
            success_count += 1
        results.append({
            "index": index,
            "success": is_success,
            "skipped": skipped,
            "message": str(message or "").strip(),
            "account_id": account_id,
        })
        account_delay(index, total, PLATFORM)

    failed = total - success_count - skipped_count
    details = []
    for r in results:
        icon = "♻️" if r["skipped"] else ("✅" if r["success"] else "❌")
        details.append(f"{icon} 账号{r['index']}\n{r['message'] or '（无输出）'}")

    extra = []
    if skipped_count:
        extra.append(f"♻️ 重复账号已跳过 {skipped_count} 个（同一账号只该保留一行）")

    # 有跳过但无失败时，「成功 x/y」会显得少了一个账号，标题里点明更清楚
    title = None
    if failed == 0 and skipped_count:
        title = f"{PLATFORM}完成｜{success_count}/{total}（重复跳过 {skipped_count}）"

    return notify_result(
        PLATFORM, total, success_count, failed,
        title=title, extra=extra, details=details,
        elapsed=time.time() - run_start,
        footers=["🔑 refresh_token 会轮换，脚本会自动写回；写回失败时手动更新"])


if __name__ == "__main__":
    sys.exit(main())
