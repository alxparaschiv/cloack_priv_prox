"""Appium-driven Instagram automation for /ig_setup_private (Shard D).

Drives a GeeLark Android cloud phone via Appium-over-ADB to:
  - log into IG (username + password + optional 2FA)
  - dismiss save-login dialog
  - set bio + optional link in bio
  - set profile picture from a local image file
  - toggle the account to Private

Methods ported from reel-bot-Carolina/reel_bot.py IGDriver class (lines
~41060-41500). XPath selectors kept verbatim — they're field-tested on
the current Instagram-for-Android build and any cosmetic change will
require maintenance in BOTH repos.

Top-level entry point: setup_ig_account(adb_ip, adb_port, glogin_pwd,
                                       username, password, totp_secret,
                                       bio, link, local_pic_path,
                                       send_progress)
which orchestrates the full flow and returns (ok, msg).

All Appium calls are synchronous (Appium's Python client is sync-only).
Callers in async code should wrap with `await asyncio.to_thread(...)`.
"""
import os
import time
import random
import logging
import subprocess

logger = logging.getLogger(__name__)


class IGAppiumDriver:
    """Drives Instagram on a GeeLark cloud phone via Appium+UiAutomator2."""

    APPIUM_URL = 'http://127.0.0.1:4723/wd/hub'
    IG_PACKAGE = 'com.instagram.android'

    def __init__(self, adb_ip, adb_port, glogin_pwd=None, send_progress=None):
        self.adb_ip = adb_ip
        self.adb_port = int(adb_port)
        self.adb_addr = f"{adb_ip}:{adb_port}"
        self.glogin_pwd = glogin_pwd
        self.send_progress = send_progress
        self.driver = None
        self._rand = random.Random()

    def _log(self, text, screenshot_bytes=None):
        logger.info(f"[IGAppium] {text}")
        if self.send_progress:
            try:
                self.send_progress(text, screenshot_bytes)
            except Exception as e:
                logger.warning(f"[IGAppium] progress callback err: {e}")

    # ─── ADB connect + permissions ─────────────────────────────────────────

    def adb_connect(self):
        subprocess.run(['adb', 'disconnect', self.adb_addr], capture_output=True, timeout=10)
        time.sleep(1)
        r = subprocess.run(['adb', 'connect', self.adb_addr],
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).lower()
        if 'connected' not in out and 'already' not in out:
            raise RuntimeError(f"adb connect failed: {r.stdout} {r.stderr}")
        self._log(f"adb connected: {self.adb_addr}")
        subprocess.run(['adb', '-s', self.adb_addr, 'wait-for-device'],
                       timeout=60, capture_output=True)
        time.sleep(2)
        # GeeLark requires a one-time glogin handshake before shell commands
        if self.glogin_pwd:
            r = subprocess.run(
                ['adb', '-s', self.adb_addr, 'shell', 'glogin', self.glogin_pwd],
                capture_output=True, text=True, timeout=20,
            )
            if 'success' not in (r.stdout + r.stderr).lower():
                raise RuntimeError(f"glogin failed: {r.stdout} {r.stderr}")
            self._log("glogin success")

    def adb(self, *args, timeout=10):
        return subprocess.run(['adb', '-s', self.adb_addr] + list(args),
                              capture_output=True, text=True, timeout=timeout)

    def grant_media_perms(self):
        perms = [
            'android.permission.READ_MEDIA_IMAGES',
            'android.permission.READ_MEDIA_VIDEO',
            'android.permission.READ_EXTERNAL_STORAGE',
            'android.permission.WRITE_EXTERNAL_STORAGE',
            'android.permission.POST_NOTIFICATIONS',
            'android.permission.ACCESS_MEDIA_LOCATION',
            'android.permission.CAMERA',
        ]
        for p in perms:
            self.adb('shell', 'pm', 'grant', self.IG_PACKAGE, p)
        # Force-stop so new perms take effect on next launch
        self.adb('shell', 'am', 'force-stop', self.IG_PACKAGE)
        self._log("media perms granted + IG force-stopped")

    def push_image(self, local_path, remote_name=None):
        """Push a local image to /sdcard/Pictures/ + broadcast media scan.
        Returns the remote path on success, None on failure."""
        if not os.path.exists(local_path):
            self._log(f"⚠️ local pic missing: {local_path}")
            return None
        remote_name = remote_name or os.path.basename(local_path).replace(' ', '_')
        remote = f"/sdcard/Pictures/{remote_name}"
        self.adb('shell', 'mkdir', '-p', '/sdcard/Pictures')
        push = subprocess.run(
            ['adb', '-s', self.adb_addr, 'push', local_path, remote],
            capture_output=True, text=True, timeout=120,
        )
        if push.returncode != 0:
            self._log(f"⚠️ adb push failed: {push.stderr[:200]}")
            return None
        self.adb('shell', 'am', 'broadcast', '-a',
                 'android.intent.action.MEDIA_SCANNER_SCAN_FILE',
                 '-d', f"file://{remote}")
        return remote

    # ─── Appium driver lifecycle ───────────────────────────────────────────

    def connect(self):
        from appium import webdriver as _aw
        from appium.options.android import UiAutomator2Options

        # Clean any stale uiautomator2 server first — prevents the
        # "instrumentation process cannot be initialized within 90000ms" error
        for srv in ['io.appium.uiautomator2.server', 'io.appium.uiautomator2.server.test']:
            self.adb('shell', 'am', 'force-stop', srv)
            self.adb('shell', 'pm', 'clear', srv)
        time.sleep(2)

        opts = UiAutomator2Options()
        opts.platform_name = 'Android'
        opts.automation_name = 'UiAutomator2'
        opts.udid = self.adb_addr
        opts.no_reset = True
        opts.full_reset = False
        opts.new_command_timeout = 600
        opts.skip_unlock = True
        opts.system_port = 8200 + self._rand.randint(0, 99)
        opts.set_capability('appium:uiautomator2ServerInstallTimeout', 90000)
        opts.set_capability('appium:uiautomator2ServerLaunchTimeout', 90000)
        opts.set_capability('appium:disableWindowAnimation', True)
        self.driver = _aw.Remote(self.APPIUM_URL, options=opts)
        self.driver.implicitly_wait(5)
        self._log("Appium connected")

    def disconnect(self):
        if self.driver:
            try: self.driver.quit()
            except Exception: pass
            self.driver = None
        try:
            subprocess.run(['adb', 'disconnect', self.adb_addr],
                           capture_output=True, timeout=10)
        except Exception: pass

    def screenshot(self):
        if not self.driver:
            return None
        try:
            return self.driver.get_screenshot_as_png()
        except Exception as e:
            logger.warning(f"[IGAppium] screenshot err: {e}")
            return None

    # ─── Helpers ───────────────────────────────────────────────────────────

    def _sleep(self, lo=0.5, hi=1.5):
        time.sleep(self._rand.uniform(lo, hi))

    def _human_type(self, element, text, char_lo=0.05, char_hi=0.18):
        for ch in str(text):
            element.send_keys(ch)
            time.sleep(self._rand.uniform(char_lo, char_hi))

    def _tap(self, x, y):
        self.driver.tap([(int(x), int(y))])

    def _swipe(self, x1, y1, x2, y2, ms=None):
        if ms is None:
            ms = self._rand.randint(400, 900)
        self.driver.swipe(int(x1), int(y1), int(x2), int(y2), int(ms))

    def find_first_visible(self, selectors, timeout=10):
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        for by, value in selectors:
            try:
                el = WebDriverWait(self.driver, timeout).until(
                    EC.visibility_of_element_located((by, value))
                )
                return el
            except Exception:
                continue
        return None

    # ─── Instagram actions ─────────────────────────────────────────────────

    def open_ig(self):
        self.driver.activate_app(self.IG_PACKAGE)
        self._sleep(2, 4)
        self._log("IG opened", self.screenshot())

    def login(self, handle, password, totp_seed):
        """Returns 'ok' | 'challenge' on the post-login screen state."""
        from appium.webdriver.common.appiumby import AppiumBy
        self._log(f"login: {handle}")
        u = self.find_first_visible([
            (AppiumBy.ID, f"{self.IG_PACKAGE}:id/login_username"),
            (AppiumBy.ACCESSIBILITY_ID, "Username, email or mobile number"),
            (AppiumBy.XPATH, "//android.widget.EditText[1]"),
        ], timeout=20)
        if not u:
            self._log("⚠️ login form not found", self.screenshot())
            raise RuntimeError("login form not found")
        u.click(); self._sleep(0.3, 0.8)
        self._human_type(u, handle); self._sleep(0.5, 1.2)

        p = self.find_first_visible([
            (AppiumBy.ID, f"{self.IG_PACKAGE}:id/password"),
            (AppiumBy.ACCESSIBILITY_ID, "Password"),
            (AppiumBy.XPATH, "//android.widget.EditText[2]"),
        ], timeout=10)
        if not p:
            self._log("⚠️ password field not found", self.screenshot())
            raise RuntimeError("password field not found")
        p.click(); self._sleep(0.3, 0.8)
        self._human_type(p, password); self._sleep(1.0, 2.0)

        btn = self.find_first_visible([
            (AppiumBy.ID, f"{self.IG_PACKAGE}:id/log_in_button"),
            (AppiumBy.ACCESSIBILITY_ID, "Log in"),
            (AppiumBy.XPATH, "//android.widget.Button[contains(@text, 'Log in') or contains(@text, 'Log In')]"),
        ], timeout=5)
        if btn: btn.click()
        else: self.driver.press_keycode(66)  # ENTER
        self._sleep(5, 8)
        self._log("post-login", self.screenshot())

        # 2FA: only if the field appears
        totp = self.find_first_visible([
            (AppiumBy.ID, f"{self.IG_PACKAGE}:id/two_fac_input"),
            (AppiumBy.XPATH, "//android.widget.EditText[contains(@hint, 'code') or contains(@hint, 'Code')]"),
        ], timeout=10)
        if totp:
            seed = (totp_seed or '').replace(' ', '').strip()
            if not seed:
                raise RuntimeError("TOTP prompt but no seed provided")
            import pyotp
            code = pyotp.TOTP(seed).now()
            self._log(f"TOTP: {code}")
            totp.click(); self._sleep(0.3, 0.6)
            self._human_type(totp, code); self._sleep(0.5, 1.2)
            confirm = self.find_first_visible([
                (AppiumBy.ID, f"{self.IG_PACKAGE}:id/confirm_button"),
                (AppiumBy.ACCESSIBILITY_ID, "Confirm"),
                (AppiumBy.XPATH, "//android.widget.Button[contains(@text, 'Confirm') or contains(@text, 'Continue') or contains(@text, 'Next')]"),
            ], timeout=5)
            if confirm: confirm.click()
            self._sleep(5, 8)
            self._log("TOTP submitted", self.screenshot())

        page = self.driver.page_source
        if any(kw in page for kw in ['challenge_required', 'Help us confirm', 'Suspicious Login',
                                     "Confirm you're human", 'I am not a robot',
                                     'Confirm your phone', "Confirm it's You"]):
            return 'challenge'
        return 'ok'

    def dismiss_save_login(self):
        from appium.webdriver.common.appiumby import AppiumBy
        for label in ['Not now', 'Not Now', 'Skip', 'Cancel']:
            btn = self.find_first_visible([
                (AppiumBy.XPATH, f"//*[@text='{label}']"),
            ], timeout=3)
            if btn:
                try: btn.click(); self._sleep(1, 2)
                except Exception: pass

    def go_to_profile(self):
        from appium.webdriver.common.appiumby import AppiumBy
        prof = self.find_first_visible([
            (AppiumBy.ACCESSIBILITY_ID, "Profile"),
            (AppiumBy.XPATH, "//android.widget.FrameLayout[contains(@content-desc, 'Profile')]"),
        ], timeout=5)
        if prof:
            prof.click()
        else:
            size = self.driver.get_window_size()
            self._tap(int(size['width'] * 0.92), int(size['height'] * 0.97))
        self._sleep(2, 3)

    def open_edit_profile(self):
        from appium.webdriver.common.appiumby import AppiumBy
        b = self.find_first_visible([
            (AppiumBy.XPATH, "//android.widget.Button[contains(@text, 'Edit profile') or contains(@text, 'Edit Profile')]"),
            (AppiumBy.ACCESSIBILITY_ID, "Edit profile"),
        ], timeout=10)
        if b: b.click()
        self._sleep(2, 3)

    def set_bio(self, bio_text):
        """Set IG bio. Targets the inner EditText inside the bio Button wrapper —
        the outer container's first EditText is the Name field, and tapping that
        triggers the 'Are you sure you want to change your name?' rate-limit
        dialog (only 2 name changes per 14 days)."""
        from appium.webdriver.common.appiumby import AppiumBy
        self.go_to_profile(); self.open_edit_profile()
        bi = self.find_first_visible([
            (AppiumBy.XPATH, "//android.widget.Button[@resource-id='com.instagram.android:id/bio']//android.widget.EditText[@long-clickable='true']"),
            (AppiumBy.XPATH, "//*[@resource-id='com.instagram.android:id/bio']//android.widget.EditText[2]"),
            (AppiumBy.ID, f"{self.IG_PACKAGE}:id/bio"),
        ], timeout=10)
        if not bi:
            self._log("⚠️ Bio editor not found", self.screenshot()); return False
        try: cur_text = bi.get_attribute('text') or ''
        except Exception: cur_text = ''
        bi.click(); self._sleep(0.6, 1.0)

        if 'change your name twice within' in (self.driver.page_source or '').lower():
            self._log("⚠️ landed on Name editor — cancelling (bio target failed)")
            cancel = self.find_first_visible([(AppiumBy.XPATH, "//*[@text='Cancel']")], timeout=3)
            if cancel: cancel.click(); self._sleep(1, 2)
            return False

        # Clear existing bio via keyevents (Selenium .clear() unreliable on IG)
        self.adb('shell', 'input', 'keyevent', '123')  # MOVE_END
        self._sleep(0.2, 0.4)
        for _ in range(len(cur_text) + 5):
            self.adb('shell', 'input', 'keyevent', '67')  # DEL
            self._sleep(0.02, 0.04)
        self._sleep(0.4, 0.8)
        self._human_type(bi, bio_text); self._sleep(0.6, 1.2)

        # Save: action_bar_button_action (top-right ✓)
        done = self.find_first_visible([
            (AppiumBy.ID, f"{self.IG_PACKAGE}:id/action_bar_button_action"),
            (AppiumBy.ACCESSIBILITY_ID, "Done"),
        ], timeout=4)
        if done:
            done.click(); self._sleep(2, 3)
        else:
            try: self.driver.hide_keyboard()
            except Exception: pass
            self._sleep(0.6, 1.0)
        # Defensive cancel of Name-change dialog if it appeared after Done
        if 'change your name' in (self.driver.page_source or '').lower():
            self._log("⚠️ Name dialog after Done — Cancel")
            c = self.find_first_visible([(AppiumBy.XPATH, "//*[@text='Cancel']")], timeout=3)
            if c: c.click(); self._sleep(1, 2)
        self._log("bio set", self.screenshot())
        return True

    def set_link(self, url):
        """Set the IG website/link in bio. Best-effort — IG's link UI varies by
        app version. Returns True on success, False if we couldn't find the link
        editor (bio still works; the user can set the link manually)."""
        from appium.webdriver.common.appiumby import AppiumBy
        if not url: return True
        self.go_to_profile(); self.open_edit_profile()
        # Modern UI: "Links" row → "Add external link"
        links_row = self.find_first_visible([
            (AppiumBy.XPATH, "//*[@text='Links']"),
            (AppiumBy.XPATH, "//*[contains(@text, 'Add link')]"),
            (AppiumBy.XPATH, "//*[contains(@text, 'Website')]"),
        ], timeout=6)
        if not links_row:
            self._log("⚠️ Links/Website row not found — skipping link (set manually)")
            return False
        links_row.click(); self._sleep(2, 3)
        # On Links sub-screen tap "Add external link"
        add = self.find_first_visible([
            (AppiumBy.XPATH, "//*[contains(@text, 'Add external link')]"),
            (AppiumBy.XPATH, "//*[contains(@text, 'Add a link')]"),
            (AppiumBy.XPATH, "//android.widget.Button[contains(@text, 'Add')]"),
        ], timeout=5)
        if add:
            add.click(); self._sleep(2, 3)
        url_field = self.find_first_visible([
            (AppiumBy.XPATH, "//android.widget.EditText[contains(@hint, 'URL') or contains(@hint, 'url') or contains(@hint, 'link')]"),
            (AppiumBy.XPATH, "//android.widget.EditText[1]"),
        ], timeout=6)
        if not url_field:
            self._log("⚠️ link URL field not found"); return False
        url_field.click(); self._sleep(0.4, 0.7)
        self._human_type(url_field, url); self._sleep(0.6, 1.0)
        # Save (Done arrow / ✓)
        done = self.find_first_visible([
            (AppiumBy.ID, f"{self.IG_PACKAGE}:id/action_bar_button_action"),
            (AppiumBy.ACCESSIBILITY_ID, "Done"),
            (AppiumBy.XPATH, "//*[@text='Done']"),
            (AppiumBy.XPATH, "//*[@text='Save']"),
        ], timeout=5)
        if done:
            done.click(); self._sleep(2, 3)
        self._log("link set", self.screenshot())
        return True

    def set_profile_picture(self, local_path):
        """Push local_path to /sdcard/Pictures, then walk Edit Profile →
        change picture → Choose from library → drag-up to hide face → Done.
        Same crop-pan flow as reel_bot.
        """
        from appium.webdriver.common.appiumby import AppiumBy
        remote = self.push_image(local_path)
        if not remote:
            return False
        # Wait a moment for the media scanner broadcast
        self._sleep(2, 3)
        self.go_to_profile(); self.open_edit_profile()
        pic = self.find_first_visible([
            (AppiumBy.ACCESSIBILITY_ID, "Edit profile picture"),
            (AppiumBy.ID, f"{self.IG_PACKAGE}:id/change_avatar_button"),
            (AppiumBy.XPATH, "//*[contains(@text, 'Edit picture or avatar') or contains(@text, 'Change profile photo')]"),
        ], timeout=10)
        if not pic:
            self._log("⚠️ Edit profile picture button not found", self.screenshot()); return False
        pic.click(); self._sleep(2.0, 3.0)
        choose = self.find_first_visible([
            (AppiumBy.XPATH, "//*[@text='Choose from library']"),
            (AppiumBy.XPATH, "//*[contains(@text, 'New profile picture')]"),
        ], timeout=6)
        if choose:
            choose.click(); self._sleep(3, 4)
        # Pan up to bias the crop downward (face-safe).
        size = self.driver.get_window_size()
        x = size['width'] // 2
        self._swipe(x, int(size['height'] * 0.40), x, int(size['height'] * 0.10), ms=700)
        self._sleep(1, 1.5)
        done = self.find_first_visible([
            (AppiumBy.ID, f"{self.IG_PACKAGE}:id/action_bar_button_action"),
            (AppiumBy.ACCESSIBILITY_ID, "Done"),
            (AppiumBy.XPATH, "//*[@text='Done']"),
        ], timeout=6)
        if done:
            done.click(); self._sleep(3, 5)
        self._log("profile picture set", self.screenshot())
        return True

    def switch_to_private(self):
        """Toggle account → Private. Current IG: Profile → Options hamburger →
        Account privacy → toggle Private."""
        from appium.webdriver.common.appiumby import AppiumBy
        self.go_to_profile()
        menu = self.find_first_visible([
            (AppiumBy.ACCESSIBILITY_ID, "Options"),
        ], timeout=8)
        if not menu:
            self._log("⚠️ Options hamburger not found", self.screenshot()); return False
        menu.click(); self._sleep(2, 3)
        priv = self.find_first_visible([
            (AppiumBy.XPATH, "//*[@text='Account privacy']"),
        ], timeout=6)
        if not priv:
            sa = self.find_first_visible([
                (AppiumBy.XPATH, "//*[@text='Settings and activity']"),
            ], timeout=4)
            if sa:
                sa.click(); self._sleep(2, 3)
                priv = self.find_first_visible([
                    (AppiumBy.XPATH, "//*[@text='Account privacy']"),
                ], timeout=5)
        if not priv:
            self._log("⚠️ Account privacy entry not found", self.screenshot()); return False
        priv.click(); self._sleep(2, 3)
        toggle = self.find_first_visible([
            (AppiumBy.XPATH, "//*[contains(@text, 'Private account')]/following::android.widget.Switch[1]"),
            (AppiumBy.XPATH, "//*[@text='Private account']/parent::*//android.widget.Switch"),
            (AppiumBy.XPATH, "//android.widget.Switch[1]"),
        ], timeout=6)
        if not toggle:
            self._log("⚠️ Private switch not found", self.screenshot()); return False
        try:
            is_on = toggle.get_attribute('checked') == 'true'
        except Exception:
            is_on = False
        if not is_on:
            toggle.click(); self._sleep(1.5, 2.5)
            conf = self.find_first_visible([
                (AppiumBy.XPATH, "//*[@text='Switch to private' or @text='OK' or @text='Confirm']"),
            ], timeout=5)
            if conf: conf.click(); self._sleep(2, 3)
        self._log("switched to private", self.screenshot())
        for _ in range(3):
            try: self.driver.back(); self._sleep(0.6, 1.0)
            except Exception: pass
        return True


# ─── Top-level orchestrator ─────────────────────────────────────────────────

def setup_ig_account(adb_ip, adb_port, glogin_pwd, username, password,
                     totp_secret, bio, link, local_pic_path, send_progress=None):
    """Full happy-path: adb_connect → grant perms → connect Appium → open IG
    → login → dismiss save-login → set profile pic → set bio → set link
    → switch to private → final screenshot. Returns (ok, msg).

    Caller is responsible for the GeeLark phone being already booted (use
    /geelark_profile_open or its underlying APIs first). At the end the
    Appium driver disconnects; the caller decides whether to stop the phone.
    """
    drv = IGAppiumDriver(adb_ip, adb_port, glogin_pwd=glogin_pwd,
                          send_progress=send_progress)
    try:
        drv.adb_connect()
        drv.grant_media_perms()
        drv.connect()
        drv.open_ig()
        status = drv.login(username, password, totp_secret)
        if status == 'challenge':
            ss = drv.screenshot()
            drv._log("⚠️ login hit a challenge — manual intervention needed", ss)
            return False, 'challenge'
        drv.dismiss_save_login()

        results = {}
        if local_pic_path:
            results['pic'] = drv.set_profile_picture(local_pic_path)
        if bio:
            results['bio'] = drv.set_bio(bio)
        if link:
            results['link'] = drv.set_link(link)
        results['private'] = drv.switch_to_private()

        ss = drv.screenshot()
        drv._log(f"final state — {results}", ss)
        ok = all(v is not False for v in results.values())
        return ok, ('OK' if ok else f"partial: {results}")
    except Exception as e:
        logger.error(f"[setup_ig_account] fatal: {type(e).__name__}: {e}", exc_info=True)
        return False, f"{type(e).__name__}: {e}"
    finally:
        try: drv.disconnect()
        except Exception: pass
