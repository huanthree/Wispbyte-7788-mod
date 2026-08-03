#!/usr/bin/env python3

import os
import sys
import time
import json
import logging
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional

import requests
from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

# ====================== 全局配置 ======================
LOGIN_URL = "https://wispbyte.com/client"
DASHBOARD_URL = "https://wispbyte.com/client/dashboard"
CONSOLE_URL_TEMPLATE = "https://wispbyte.com/client/servers/{identifier}/console"
REWARD_VIDEO_URL = "https://wispbyte.com/client/reward-video"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", str(Path.cwd()))
OUTPUT_DIR = Path(WORKSPACE) / "output/screenshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("wispbyte_restart")

for _noisy in ("seleniumbase", "selenium", "urllib3", "undetected_chromedriver"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


# ====================== 工具函数 ======================
def mask_email(email: str) -> str:
    if '@' not in email:
        return email[:1] + "***"
    local, domain = email.split('@', 1)
    masked_local = local[:1] + "***" if local else "***"
    if '.' in domain:
        parts = domain.split('.')
        tld = parts[-1]
        first_char = domain[0]
        masked_domain = f"{first_char}***.{tld}"
    else:
        masked_domain = domain[:1] + "***"
    return f"{masked_local}@{masked_domain}"


def mask_server_id(identifier: str) -> str:
    if not identifier:
        return "***"
    if len(identifier) <= 4:
        return "***"
    return identifier[:2] + "***" + identifier[-2:]


def log(msg: str, level: str = "INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    logger.info(f"{prefix} {msg}")


def send_tg_photo(token: str, chat_id: str, photo_path: str, caption: str):
    if not token or not chat_id:
        return
    if not photo_path or not os.path.exists(photo_path):
        log(f"截图文件不存在: {photo_path}", "WARN")
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=30
            )
        resp.raise_for_status()
        log("Telegram 图片通知发送成功")
    except Exception as e:
        log(f"Telegram 通知异常: {e}", "ERROR")


def restart_warp():
    log("正在重启 WARP 以更换 IP...")
    try:
        old_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"当前 IP: {old_ip}")
    except Exception:
        old_ip = "未知"
    try:
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "disconnect"],
                       check=False, timeout=20, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "connect"],
                       check=False, timeout=20, capture_output=True)
        time.sleep(8)
        new_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"WARP 重连成功，新 IP: {new_ip}")
        return True
    except Exception as e:
        log(f"WARP 重连失败: {e}", "WARN")
        return False


def take_screenshot(sb, account_index: int, suffix: str) -> str:
    timestamp = datetime.now().strftime("%H%M%S")
    filename = f"acc{account_index}-{suffix}-{timestamp}.png"
    filepath = str(OUTPUT_DIR / filename)
    try:
        sb.save_screenshot(filepath)
        log(f"📸 截图保存: {filepath}")
        return filepath
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return ""


# ====================== 页面处理与 CSS 屏蔽 ======================
def block_ads_modals(sb):
    css = """
    .wisp-offer-modal, .instagram-modal, .qc-cmp2-summary-section {
        display: none !important;
    }
    """
    try:
        sb.execute_script(f'''
            var style = document.createElement('style');
            style.textContent = {json.dumps(css)};
            document.head.appendChild(style);
        ''')
        log("✅ 已注入广告屏蔽 CSS")
    except Exception as e:
        log(f"注入屏蔽 CSS 失败: {e}", "WARN")


# ====================== Cloudflare Turnstile 验证处理 ======================
def check_turnstile_solved(sb) -> bool:
    try:
        return bool(sb.execute_script('''
            var inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value && inp.value.length > 20) return true;
            var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe && iframe.getAttribute("data-state") === "solved") return true;
            var success = document.getElementById('success');
            return !!(success && getComputedStyle(success).display !== 'none');
        '''))
    except Exception:
        return False


def wait_for_turnstile_success(sb, timeout: int = 30) -> bool:
    log("等待 Turnstile 验证...")
    start = time.time()
    last_click = 0
    while time.time() - start < timeout:
        if check_turnstile_solved(sb):
            log("✅ Turnstile 验证成功")
            return True
        if time.time() - last_click > 3:
            try:
                sb.uc_gui_click_captcha()
                last_click = time.time()
                log("点击 Turnstile")
            except Exception as e:
                log(f"点击 Turnstile 异常: {e}", "WARN")
        time.sleep(1)
    log("⏰ Turnstile 验证超时", "WARN")
    return False


def handle_restart_turnstile_modal(sb, timeout: int = 90) -> bool:
    log("等待 CF Turnstile 重启验证弹窗...")
    start = time.time()
    modal_appeared = False

    for _ in range(20):
        try:
            exists = sb.execute_script('''
                var el = document.querySelector('.wisp-start-captcha-modal');
                return !!(el && getComputedStyle(el).display !== 'none');
            ''')
            if exists:
                modal_appeared = True
                log("CF Turnstile 弹窗已出现")
                break
        except Exception:
            pass
        time.sleep(1)

    if not modal_appeared:
        log("CF Turnstile 弹窗未出现，可能无需验证", "WARN")
        return True

    last_click = 0
    while time.time() - start < timeout:
        try:
            modal_visible = sb.execute_script('''
                var el = document.querySelector('.wisp-start-captcha-modal');
                return !!(el && getComputedStyle(el).display !== 'none');
            ''')
            if not modal_visible:
                log("✅ CF Turnstile 弹窗已关闭，验证完成")
                return True

            if check_turnstile_solved(sb):
                log("Turnstile 已解决，等待弹窗自动关闭...")
                for _ in range(15):
                    closed = sb.execute_script('''
                        var el = document.querySelector('.wisp-start-captcha-modal');
                        return !(el && getComputedStyle(el).display !== 'none');
                    ''')
                    if closed:
                        log("✅ 弹窗已自动关闭")
                        return True
                    time.sleep(1)
                try:
                    sb.execute_script('''
                        var btn = document.querySelector(
                            '.wisp-start-captcha-btn[data-action="cancel"],' +
                            '.wisp-start-captcha-modal button[type="submit"],' +
                            '.wisp-start-captcha-modal .submit-btn'
                        );
                        if (btn) btn.click();
                    ''')
                    time.sleep(2)
                    return True
                except Exception:
                    pass
                return True

            now = time.time()
            if now - last_click > 3:
                try:
                    sb.uc_gui_click_captcha()
                    last_click = now
                    log("CF弹窗内点击 Turnstile (uc_gui)")
                except Exception:
                    try:
                        sb.execute_script('''
                            var ts = document.querySelector(
                                '.wisp-start-captcha-modal .cf-turnstile,' +
                                '.wisp-start-captcha-modal iframe'
                            );
                            if (ts) ts.click();
                        ''')
                        last_click = now
                        log("CF弹窗内点击 Turnstile (JS)")
                    except Exception as e:
                        log(f"CF弹窗 Turnstile 点击失败: {e}", "WARN")

        except Exception as e:
            log(f"CF Turnstile 弹窗处理异常: {e}", "WARN")

        time.sleep(1)

    return True


# ====================== 广告看片流程逻辑 ======================
def _get_page_situation(sb) -> str:
    try:
        current_url = sb.get_current_url()
    except Exception:
        return 'unknown'

    if "reward-video" in current_url or "reward_video" in current_url:
        return 'reward'

    try:
        adblocker = sb.execute_script('''
            var box = document.querySelector('.check-box');
            var title = document.querySelector('.check-title');
            return !!(box || (title && title.textContent.toLowerCase().includes('adblocker')));
        ''')
        if adblocker:
            return 'adblocker'
    except Exception:
        pass

    try:
        has_embed_btn = sb.execute_script('''
            return !!(document.getElementById('embedWatchBtn') || 
                      document.getElementById('embedPlayBtn'));
        ''')
        if has_embed_btn:
            return 'reward'
    except Exception:
        pass

    return 'unknown'


def _dismiss_alert_if_present(sb) -> bool:
    try:
        alert = sb.driver.switch_to.alert
        alert_text = alert.text
        log(f"发现 Alert 弹窗: {alert_text[:100]}")
        alert.accept()
        log("✅ Alert 弹窗已关闭")
        time.sleep(1)
        return True
    except Exception:
        return False


def _handle_adblocker_page(sb) -> bool:
    log("检测到广告拦截器页面，尝试点击 'Check again'...")
    try:
        sb.execute_script('''
            var btn = document.getElementById('recheck-btn');
            if (btn) btn.click();
        ''')
        log("✅ 已点击 'Check again'")
        time.sleep(3)
        return True
    except Exception as e:
        log(f"点击 'Check again' 失败: {e}", "WARN")
        return False


def _wait_for_reward_btn_ready(sb, timeout: int = 90) -> bool:
    log(f"等待广告视频加载就绪（最长 {timeout}s）...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = sb.execute_script('''
                var btn = document.getElementById('embedWatchBtn');
                var panel = document.getElementById('embedPlayBtn');
                if (!btn || !panel) return {ready: false};
                var panelDisplay = window.getComputedStyle(panel).display;
                var btnDisplay = window.getComputedStyle(btn).display;
                var btnVis = window.getComputedStyle(btn).visibility;
                if (panelDisplay === 'none') return {ready: false};
                return { ready: btnDisplay !== 'none' && btnVis !== 'hidden' };
            ''')
            if result and result.get('ready'):
                log("✅ 广告已就绪，Watch ad 按钮可点击")
                return True
        except Exception:
            pass
        time.sleep(2)

    log(f"⏰ 广告按钮等待超时 ({timeout}s)", "WARN")
    return False


def _click_watch_ad_btn(sb) -> bool:
    log("点击 'Watch ad to continue' 按钮...")
    methods = [
        lambda: sb.execute_script('''
            var btn = document.getElementById('embedWatchBtn');
            if (!btn) return false;
            btn.click();
            return true;
        '''),
        lambda: (sb.click('#embedWatchBtn') or True),
    ]
    for i, method in enumerate(methods, 1):
        try:
            if method():
                log(f"✅ 广告按钮点击成功（方式{i}）")
                time.sleep(1)
                return True
        except Exception as e:
            log(f"广告按钮点击方式{i}失败: {e}", "WARN")
    return False


def _wait_for_ad_completion(sb, identifier: str, timeout: int = 300) -> bool:
    safe_id = mask_server_id(identifier)
    log(f"广告开始播放，等待完成: {safe_id}")
    start = time.time()
    console_path = f"/servers/{identifier}/console"

    while time.time() - start < timeout:
        try:
            current_url = sb.get_current_url()
            if "rewardDone=1" in current_url or console_path in current_url:
                log(f"✅ 广告播放完成: {safe_id}")
                return True

            status_info = sb.execute_script('''
                var st = document.getElementById('embedStatus');
                if (!st) return '';
                return st.textContent || '';
            ''')
            if any(kw in status_info.lower() for kw in ['starting', 'saving', 'returning', 'session']):
                log(f"✅ 广告完成 [embedStatus 检测成功]: {safe_id}")
                time.sleep(6)
                return True
        except Exception:
            pass
        time.sleep(3)

    log(f"⚠️ 广告等待超时: {safe_id}", "WARN")
    return False


def handle_reward_ad_flow(sb, identifier: str, console_url: str) -> bool:
    safe_id = mask_server_id(identifier)
    log(f"广告流程处理开始: {safe_id}")
    start = time.time()

    while time.time() - start < 15:
        if _dismiss_alert_if_present(sb):
            log("✅ 已处理 Alert 弹窗（无广告），继续CF验证")
            return True

        situation = _get_page_situation(sb)
        if situation == 'reward':
            return _execute_reward_ad_watch(sb, identifier)
        elif situation == 'adblocker':
            _handle_adblocker_page(sb)
            time.sleep(3)
            continue
        elif situation == 'console':
            log("当前在控制台页面，无需处理广告")
            return True
        time.sleep(1)

    return True


def _execute_reward_ad_watch(sb, identifier: str) -> bool:
    safe_id = mask_server_id(identifier)
    log(f"进入广告观看流程: {safe_id}")

    if not _wait_for_reward_btn_ready(sb, timeout=90):
        _dismiss_alert_if_present(sb)
        return True

    _dismiss_alert_if_present(sb)
    if _click_watch_ad_btn(sb):
        _wait_for_ad_completion(sb, identifier, timeout=300)
        _dismiss_alert_if_present(sb)

    return True


# ====================== 核心登录逻辑 ======================
def login(sb, email: str, password: str) -> bool:
    log("访问登录页...")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
    time.sleep(4)

    try:
        sb.wait_for_element_visible('input#email', timeout=15)
        log("✅ 找到登录表单")
    except TimeoutException:
        log("未找到登录表单，重新连入...", "WARN")
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
        time.sleep(5)
        try:
            sb.wait_for_element_visible('input#email', timeout=10)
        except TimeoutException:
            log("无法载入登录表单", "ERROR")
            return False

    log("填写登录凭据...")
    sb.type('input#email', email)
    time.sleep(0.5)
    sb.type('input#password', password)
    time.sleep(0.5)

    if not wait_for_turnstile_success(sb, timeout=35):
        log("登录 Turnstile 未通过", "ERROR")
        return False

    log("点击提交登录...")
    try:
        sb.click('button.login-btn')
    except Exception:
        sb.execute_script('document.querySelector("form#login-form").submit()')

    log("等待控制台响应...")
    for _ in range(15):
        if "/dashboard" in sb.get_current_url():
            log("已成功登录并进入仪表盘")
            break
        time.sleep(1)
    else:
        log("登录后跳转异常", "ERROR")
        return False

    block_ads_modals(sb)
    return True


# ====================== 服务器列表与重启 ======================
def get_servers(sb) -> List[str]:
    log("获取服务器列表...")
    try:
        result = sb.execute_async_script('''
            var callback = arguments[arguments.length - 1];
            fetch('/client/api/servers/status', {
                method: 'GET',
                headers: { 'Accept': 'application/json' }
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.servers) {
                    callback(data.servers.map(function(s) { return s.identifier; }));
                } else {
                    callback([]);
                }
            })
            .catch(function() { callback([]); });
        ''')
        if result and isinstance(result, list):
            ids = [str(i) for i in result if i]
            if ids:
                log(f"成功通过 API 获取到 {len(ids)} 台服务器")
                return ids
    except Exception as e:
        log(f"API 请求错误: {e}", "WARN")

    return []


def restart_server(sb, identifier: str) -> bool:
    console_url = CONSOLE_URL_TEMPLATE.format(identifier=identifier)
    safe_id = mask_server_id(identifier)
    log(f"开始重启服务器: {safe_id}")

    sb.get(console_url)
    time.sleep(5)
    block_ads_modals(sb)

    start_btn = None
    for btn_sel in ['button#start-btn', 'button#restart-btn']:
        try:
            start_btn = sb.wait_for_element_visible(btn_sel, timeout=8)
            break
        except Exception:
            continue

    if not start_btn:
        log("未识别到 Start/Restart 按钮", "ERROR")
        return False

    try:
        start_btn.click()
        log("✅ 已点击操作按钮")
    except Exception:
        sb.execute_script("document.querySelector('#start-btn, #restart-btn').click()")

    time.sleep(3)

    log("=== 处理广告及弹窗流程 ===")
    handle_reward_ad_flow(sb, identifier, console_url)

    if identifier not in sb.get_current_url():
        sb.get(console_url)
        time.sleep(5)

    block_ads_modals(sb)

    log("=== 处理重启 Turnstile 验证 ===")
    handle_restart_turnstile_modal(sb, timeout=90)

    log(f"轮询服务器运行状态: {safe_id}")
    poll_start = time.time()
    while time.time() - poll_start < 60:
        try:
            status = sb.execute_async_script(f'''
                var callback = arguments[arguments.length - 1];
                fetch('/client/api/servers/status')
                .then(res => res.json())
                .then(data => {{
                    var s = data.servers.find(item => item.identifier === "{identifier}");
                    callback(s ? s.current_state : null);
                }}).catch(() => callback(null));
            ''')
            if status and 'running' in str(status).lower():
                log(f"✅ 服务器 {safe_id} 已成功运行！")
                return True
        except Exception:
            pass
        time.sleep(5)

    return False


# ====================== 账号迭代处理 ======================
def process_account(idx: int, email: str, password: str, tg_token: str, tg_chat: str):
    log(f"运行账号 [{idx}]: {mask_email(email)}")
    user_data_dir = tempfile.mkdtemp(prefix=f"wisp_usr_{idx}_")

    # 重点：指定 xvfb=True 开启 Linux 虚拟桌面映射，保障 uc_gui 物理点击生效
    with SB(uc=True, test=True, locale="en", xvfb=True,
            user_data_dir=user_data_dir,
            chromium_arg="--disable-blink-features=AutomationControlled") as sb:
        try:
            if not login(sb, email, password):
                screenshot = take_screenshot(sb, idx, "login-fail")
                send_tg_photo(tg_token, tg_chat, screenshot,
                              f"❌ 登录失败\n账号: {mask_email(email)}\n\nWispbyte Auto Restart")
                return

            servers = get_servers(sb)
            if not servers:
                screenshot = take_screenshot(sb, idx, "no-server")
                send_tg_photo(tg_token, tg_chat, screenshot,
                              f"❌ 未抓取到服务器\n账号: {mask_email(email)}\n\nWispbyte Auto Restart")
                return

            for si, server_id in enumerate(servers, start=1):
                success = restart_server(sb, server_id)
                suffix = f"done-{si}"
                screenshot = take_screenshot(sb, idx, suffix)
                status_icon = "✅" if success else "❌"
                caption = (
                    f"{status_icon} 重启结果: {'成功' if success else '失败'}\n\n"
                    f"账号: {mask_email(email)}\n"
                    f"服务器: {server_id}\n\n"
                    f"Wispbyte Auto Restart"
                )
                send_tg_photo(tg_token, tg_chat, screenshot, caption)

        except Exception as e:
            log(f"账号运行异常: {e}", "ERROR")
            screenshot = take_screenshot(sb, idx, "exception")
            send_tg_photo(tg_token, tg_chat, screenshot,
                          f"❌ 运行遇到致命异常\n账号: {mask_email(email)}\n异常信息: {str(e)[:150]}")


# ====================== 主入口与配置加载 ======================
def load_accounts() -> List[Tuple[str, str]]:
    accounts = []
    for i in range(1, 6):
        raw = os.environ.get(f"WISPBYTE_{i}")
        if not raw:
            continue
        parts = raw.split("-----")
        if len(parts) >= 2:
            email, password = parts[0].strip(), parts[1].strip()
            if email and password:
                accounts.append((email, password))
    return accounts


def main():
    tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TG_CHAT_ID", "").strip()

    all_accounts = load_accounts()
    if not all_accounts:
        log("未配置任何 WISPBYTE 环境变量账号，程序退出", "ERROR")
        sys.exit(1)

    target_raw = os.environ.get("INPUT_ACCOUNTS", "").strip()
    selected = []

    if target_raw:
        targets = [t.strip().lower() for t in target_raw.split(",") if t.strip()]
        for idx, (email, pwd) in enumerate(all_accounts, start=1):
            if email.lower() in targets:
                selected.append((idx, email, pwd))
    else:
        selected = [(idx, email, pwd) for idx, (email, pwd) in enumerate(all_accounts, start=1)]

    for run_order, (idx, email, password) in enumerate(selected):
        if run_order > 0:
            restart_warp()
        process_account(idx, email, password, tg_token, tg_chat)
        if run_order < len(selected) - 1:
            time.sleep(5)

    log("所有指定账号流程运行完毕")


if __name__ == "__main__":
    main()
