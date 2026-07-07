"""
QQ空间相册原图批量下载器 (GUI版 v2)
====================================
轻量可视化界面，支持：
- 原图下载（通过 floatview 接口获取 raw URL）
- Cookie 过期自动检测 & 弹窗输入新 Cookie 续传
- 断点续传（已下载自动跳过）
- 实时进度和日志

使用方法：
1. pip install requests
2. python qq_album_downloader_gui.py
3. 填入QQ号和Cookie → 获取相册 → 勾选 → 开始下载

获取Cookie方法：
  浏览器登录 https://user.qzone.qq.com → F12 → Network →
  点击页面任意操作 → 找 qzone.qq.com 请求 → 复制 Cookie 请求头
"""

import os
import re
import json
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests


# ============================================================
# 核心逻辑
# ============================================================

def calc_g_tk(p_skey: str) -> int:
    """根据 p_skey 或 skey 计算 g_tk"""
    h = 5381
    for ch in p_skey:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def parse_jsonp(text: str) -> dict:
    match = re.search(r'^\s*\w+\s*\(\s*({.*})\s*\)\s*;?\s*$', text, re.S)
    if match:
        return json.loads(match.group(1))
    return json.loads(text)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    return name.strip('. ')[:200]


def fix_url(url: str) -> str:
    """修复 URL：处理协议相对URL（//开头）和其他格式"""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return ""


class CookieExpiredError(Exception):
    """Cookie 过期异常"""
    pass


class QzoneAPI:
    """QQ空间API封装"""

    ALBUM_URL = "https://h5.qzone.qq.com/proxy/domain/photo.qzone.qq.com/fcgi-bin/fcg_list_album_v3"
    PHOTO_URL = "https://h5.qzone.qq.com/proxy/domain/photo.qzone.qq.com/fcgi-bin/cgi_list_photo"
    FLOATVIEW_URL = "https://user.qzone.qq.com/proxy/domain/photo.qzone.qq.com/fcgi-bin/cgi_floatview_photo_list_v2"

    # 已知的鉴权失败错误码
    AUTH_ERROR_CODES = {-3000, -3001, -4000, -4001, -10001, 100000, -100000}

    def __init__(self, qq: str, cookie: str):
        self.qq = qq
        self.cookie = cookie

        # 提取鉴权密钥（优先级：p_skey > skey）
        p_skey_match = re.search(r'(?<![a-zA-Z_])p_skey=([^;]+)', cookie)
        skey_match = re.search(r'(?<![a-zA-Z_])skey=([^;]+)', cookie)

        if p_skey_match:
            self.auth_key = p_skey_match.group(1)
            self.auth_type = "p_skey"
        elif skey_match:
            self.auth_key = skey_match.group(1)
            self.auth_type = "skey"
        else:
            raise ValueError(
                "Cookie 中未找到 p_skey 或 skey！\n\n"
                "请确认：\n"
                "1. 已在浏览器中登录 QQ 空间 (https://user.qzone.qq.com)\n"
                "2. F12 → Network → 找 qzone.qq.com 域名的请求\n"
                "3. 复制该请求的完整 Cookie 头"
            )

        self.g_tk = calc_g_tk(self.auth_key)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"https://user.qzone.qq.com/{qq}",
            "Origin": "https://user.qzone.qq.com",
            "Cookie": cookie,
        })

    def update_cookie(self, new_cookie: str):
        """更新 Cookie（用于过期续传）"""
        self.cookie = new_cookie

        p_skey_match = re.search(r'(?<![a-zA-Z_])p_skey=([^;]+)', new_cookie)
        skey_match = re.search(r'(?<![a-zA-Z_])skey=([^;]+)', new_cookie)

        if p_skey_match:
            self.auth_key = p_skey_match.group(1)
            self.auth_type = "p_skey"
        elif skey_match:
            self.auth_key = skey_match.group(1)
            self.auth_type = "skey"
        else:
            raise ValueError("新 Cookie 中未找到 p_skey 或 skey")

        self.g_tk = calc_g_tk(self.auth_key)
        self.session.headers["Cookie"] = new_cookie

    def _check_auth_error(self, data: dict):
        """检测是否是鉴权失败"""
        code = data.get("code", 0)
        if code in self.AUTH_ERROR_CODES:
            raise CookieExpiredError(
                f"Cookie 已过期 (错误码: {code})，请重新登录QQ空间获取新的Cookie")
        msg = str(data.get("message", "")).lower()
        if "login" in msg or "登录" in msg or "auth" in msg or "鉴权" in msg:
            raise CookieExpiredError(f"Cookie 已过期: {data.get('message')}")

    def get_albums(self) -> list:
        params = {
            "g_tk": self.g_tk,
            "hostUin": self.qq,
            "uin": self.qq,
            "appid": 4,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "source": "qzone",
            "plat": "qzone",
            "format": "jsonp",
            "notice": 0,
            "filter": 1,
            "handset": 4,
            "pageNumModeSort": 40,
            "pageNumModeClass": 15,
            "needUserInfo": 1,
            "idcNum": 4,
        }
        resp = self.session.get(self.ALBUM_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = parse_jsonp(resp.text)

        self._check_auth_error(data)

        if data.get("code") != 0:
            raise RuntimeError(f"获取相册失败: code={data.get('code')}, {data.get('message', '未知错误')}")

        albums = data.get("data", {}).get("albumListModeSort", [])
        if not albums:
            albums = data.get("data", {}).get("albumListModeClass", [])
        if not albums:
            albums = data.get("data", {}).get("albumList", [])
        return albums

    def get_photos(self, album_id: str, total: int, page_size=500) -> list:
        all_photos = []
        page_start = 0
        errors = []
        while page_start < total:
            params = {
                "g_tk": self.g_tk,
                "hostUin": self.qq,
                "uin": self.qq,
                "appid": 4,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "source": "qzone",
                "plat": "qzone",
                "format": "jsonp",
                "notice": 0,
                "topicId": album_id,
                "pageStart": page_start,
                "pageNum": page_size,
            }
            resp = self.session.get(self.PHOTO_URL, params=params, timeout=30)
            resp.raise_for_status()

            try:
                data = parse_jsonp(resp.text)
            except Exception as e:
                errors.append(f"解析响应失败: {e}, 内容前200字: {resp.text[:200]}")
                break

            self._check_auth_error(data)

            code = data.get("code", -1)
            if code != 0:
                msg = data.get("message", "未知错误")
                errors.append(f"API返回错误 code={code}: {msg}")
                break

            photos = data.get("data", {}).get("photoList", [])
            if not photos:
                break
            all_photos.extend(photos)
            page_start += len(photos)
            if len(photos) < page_size:
                break
            time.sleep(0.3)

        if not all_photos and errors:
            raise RuntimeError("; ".join(errors))

        return all_photos

    def enrich_with_floatview(self, photos: list, topic_id: str, log_func=None):
        """
        通过 cgi_floatview_photo_list_v2 接口批量获取照片的原图(raw) URL。
        返回成功获取到 raw URL 的数量。
        """
        batch_size = 18
        enriched_count = 0

        for i in range(0, len(photos), batch_size):
            pic_key = photos[i].get("picKey") or photos[i].get("lloc", "")
            if not pic_key:
                continue

            try:
                params = {
                    "g_tk": self.g_tk,
                    "topicId": topic_id,
                    "picKey": pic_key,
                    "cmtNum": 0,
                    "inCharset": "utf-8",
                    "outCharset": "utf-8",
                    "uin": self.qq,
                    "hostUin": self.qq,
                    "appid": 4,
                    "isFirst": 1 if i == 0 else 0,
                    "postNum": batch_size,
                }
                resp = self.session.get(self.FLOATVIEW_URL, params=params, timeout=20)
                resp.raise_for_status()
                data = parse_jsonp(resp.text)

                self._check_auth_error(data)

                if data.get("code") == 0 and data.get("data", {}).get("photos"):
                    for p in data["data"]["photos"]:
                        key = p.get("picKey", "")
                        raw_url = fix_url(p.get("raw", ""))
                        if key and raw_url:
                            # 写回到对应照片
                            for photo in photos:
                                pk = photo.get("picKey") or photo.get("lloc", "")
                                if pk == key and not photo.get("_raw_set"):
                                    photo["raw"] = raw_url
                                    photo["_raw_set"] = True
                                    enriched_count += 1
                                    break
            except CookieExpiredError:
                raise
            except Exception as e:
                if log_func:
                    log_func(f"    floatview批次失败(offset={i}): {type(e).__name__}")

            time.sleep(0.3)

        return enriched_count

    @staticmethod
    def get_original_url(photo: dict) -> str:
        """
        获取原图URL。
        优先级：raw > url > pre
        所有URL都经过 fix_url 处理（兼容 // 开头的协议相对URL）
        """
        for key in ["raw", "url", "pre"]:
            raw_val = photo.get(key, "")
            url = fix_url(raw_val)
            if url:
                return url
        return ""

    def download_photo(self, url: str, save_path: str) -> bool:
        """下载单张照片，返回 True/False"""
        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            return True

        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=30, stream=True)
                if resp.status_code == 403 or resp.status_code == 401:
                    raise CookieExpiredError("下载返回401/403，Cookie可能已过期")
                resp.raise_for_status()

                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                temp_path = save_path + ".tmp"
                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(8192):
                        if chunk:
                            f.write(chunk)
                if os.path.getsize(temp_path) > 0:
                    os.replace(temp_path, save_path)
                    return True
                else:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
            except CookieExpiredError:
                raise
            except Exception:
                if attempt < 2:
                    time.sleep(1)
        return False


# ============================================================
# GUI 界面
# ============================================================

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("QQ空间相册原图下载器 v2")
        self.root.geometry("750x720")
        self.root.resizable(True, True)

        self.api = None
        self.albums = []
        self.album_vars = []
        self.downloading = False
        self.stop_flag = False

        self._build_ui()

    def _build_ui(self):
        # ---------- 顶部：输入区域 ----------
        input_frame = ttk.LabelFrame(self.root, text="认证信息", padding=10)
        input_frame.pack(fill="x", padx=10, pady=(10, 5))

        # QQ号
        row1 = ttk.Frame(input_frame)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="QQ号:", width=8).pack(side="left")
        self.qq_entry = ttk.Entry(row1, width=20)
        self.qq_entry.pack(side="left", padx=(0, 20))

        # 保存目录
        ttk.Label(row1, text="保存到:").pack(side="left")
        self.dir_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "QQ空间相册"))
        self.dir_entry = ttk.Entry(row1, textvariable=self.dir_var, width=30)
        self.dir_entry.pack(side="left", padx=(0, 5))
        ttk.Button(row1, text="浏览", command=self._choose_dir, width=5).pack(side="left")

        # Cookie
        row2 = ttk.Frame(input_frame)
        row2.pack(fill="x", pady=(5, 2))
        ttk.Label(row2, text="Cookie:", width=8).pack(side="left", anchor="n")
        self.cookie_text = tk.Text(row2, height=3, wrap="none")
        self.cookie_text.pack(side="left", fill="x", expand=True)
        scrollbar = ttk.Scrollbar(row2, orient="horizontal", command=self.cookie_text.xview)
        self.cookie_text.configure(xscrollcommand=scrollbar.set)

        # 获取相册按钮
        btn_frame = ttk.Frame(input_frame)
        btn_frame.pack(fill="x", pady=(5, 0))
        self.fetch_btn = ttk.Button(btn_frame, text="获取相册列表", command=self._fetch_albums)
        self.fetch_btn.pack(side="left")
        self.status_label = ttk.Label(btn_frame, text="", foreground="gray")
        self.status_label.pack(side="left", padx=10)

        # ---------- 中间：相册列表 ----------
        album_frame = ttk.LabelFrame(self.root, text="相册列表（勾选要下载的相册）", padding=10)
        album_frame.pack(fill="both", expand=True, padx=10, pady=5)

        # 全选/取消按钮
        ctrl_frame = ttk.Frame(album_frame)
        ctrl_frame.pack(fill="x", pady=(0, 5))
        ttk.Button(ctrl_frame, text="全选", command=self._select_all, width=8).pack(side="left", padx=(0, 5))
        ttk.Button(ctrl_frame, text="取消全选", command=self._deselect_all, width=8).pack(side="left")
        self.selected_label = ttk.Label(ctrl_frame, text="")
        self.selected_label.pack(side="right")

        # 可滚动的相册列表
        list_container = ttk.Frame(album_frame)
        list_container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(list_container, borderwidth=0, highlightthickness=0)
        self.scrollbar_y = ttk.Scrollbar(list_container, orient="vertical", command=self.canvas.yview)
        self.album_inner = ttk.Frame(self.canvas)

        self.album_inner.bind("<Configure>",
                              lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.album_inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar_y.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar_y.pack(side="right", fill="y")

        # 鼠标滚轮支持
        self.canvas.bind_all("<MouseWheel>",
                             lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # ---------- 底部：下载控制 ----------
        download_frame = ttk.LabelFrame(self.root, text="下载", padding=10)
        download_frame.pack(fill="x", padx=10, pady=(5, 10))

        btn_row = ttk.Frame(download_frame)
        btn_row.pack(fill="x", pady=(0, 5))
        self.download_btn = ttk.Button(btn_row, text="开始下载原图", command=self._start_download)
        self.download_btn.pack(side="left")
        self.stop_btn = ttk.Button(btn_row, text="停止", command=self._stop_download, state="disabled")
        self.stop_btn.pack(side="left", padx=10)
        self.progress_label = ttk.Label(btn_row, text="")
        self.progress_label.pack(side="right")

        self.progress = ttk.Progressbar(download_frame, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 5))

        # 日志
        self.log_text = tk.Text(download_frame, height=8, state="disabled", wrap="word",
                                background="#f5f5f5", font=("Consolas", 9))
        self.log_text.pack(fill="x")

    def _choose_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.dir_var.set(d)

    def _log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _log_safe(self, msg: str):
        """线程安全的日志输出"""
        self.root.after(0, lambda: self._log(msg))

    def _set_status(self, text: str, color="gray"):
        self.status_label.configure(text=text, foreground=color)

    def _fetch_albums(self):
        qq = self.qq_entry.get().strip()
        cookie = self.cookie_text.get("1.0", "end").strip()

        if not qq:
            messagebox.showwarning("提示", "请输入QQ号")
            return
        if not cookie:
            messagebox.showwarning("提示", "请粘贴Cookie")
            return

        self.fetch_btn.configure(state="disabled")
        self._set_status("正在获取相册列表...", "blue")

        def worker():
            try:
                self.api = QzoneAPI(qq, cookie)
                self.root.after(0, lambda: self._log(
                    f"[认证] 使用 {self.api.auth_type} 计算 g_tk={self.api.g_tk}"))
                self.albums = self.api.get_albums()
                self.root.after(0, self._render_albums)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("获取失败", str(e)))
                self.root.after(0, lambda: self._set_status("获取失败", "red"))
            finally:
                self.root.after(0, lambda: self.fetch_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _render_albums(self):
        for widget in self.album_inner.winfo_children():
            widget.destroy()
        self.album_vars.clear()

        if not self.albums:
            self._set_status("未找到相册", "red")
            return

        total_photos = 0
        for album in self.albums:
            name = album.get("name", "未命名")
            count = album.get("total", 0)
            total_photos += count

            var = tk.BooleanVar(value=True)
            self.album_vars.append(var)

            cb = ttk.Checkbutton(
                self.album_inner,
                text=f"{name}  ({count} 张)",
                variable=var,
                command=self._update_selected_count
            )
            cb.pack(anchor="w", pady=1)

        self._set_status(f"找到 {len(self.albums)} 个相册，共约 {total_photos} 张照片", "green")
        self._update_selected_count()

    def _update_selected_count(self):
        selected = sum(1 for v in self.album_vars if v.get())
        total_photos = sum(
            self.albums[i].get("total", 0)
            for i, v in enumerate(self.album_vars) if v.get()
        )
        self.selected_label.configure(text=f"已选 {selected} 个相册，约 {total_photos} 张照片")

    def _select_all(self):
        for v in self.album_vars:
            v.set(True)
        self._update_selected_count()

    def _deselect_all(self):
        for v in self.album_vars:
            v.set(False)
        self._update_selected_count()

    def _ask_new_cookie(self) -> str:
        """弹窗询问用户输入新的Cookie（在主线程调用）"""
        result = [None]

        def ask():
            # 使用自定义对话框，因为 simpledialog 对长文本不友好
            dialog = tk.Toplevel(self.root)
            dialog.title("Cookie 已过期")
            dialog.geometry("500x200")
            dialog.transient(self.root)
            dialog.grab_set()

            ttk.Label(dialog,
                      text="Cookie 已过期！请重新登录 QQ 空间，\n复制新的 Cookie 粘贴到下方：",
                      font=("", 10)).pack(padx=10, pady=(10, 5))

            text_widget = tk.Text(dialog, height=4, wrap="none")
            text_widget.pack(fill="x", padx=10, pady=5)

            def on_ok():
                result[0] = text_widget.get("1.0", "end").strip()
                dialog.destroy()

            def on_cancel():
                result[0] = ""
                dialog.destroy()

            btn_frame = ttk.Frame(dialog)
            btn_frame.pack(pady=10)
            ttk.Button(btn_frame, text="确定继续下载", command=on_ok).pack(side="left", padx=10)
            ttk.Button(btn_frame, text="取消下载", command=on_cancel).pack(side="left", padx=10)

            dialog.protocol("WM_DELETE_WINDOW", on_cancel)
            dialog.wait_window()

        # 在主线程执行对话框
        event = threading.Event()

        def ask_and_signal():
            ask()
            event.set()

        self.root.after(0, ask_and_signal)
        event.wait()  # 等待用户输入完毕

        return result[0] or ""

    def _start_download(self):
        if not self.api or not self.albums:
            messagebox.showwarning("提示", "请先获取相册列表")
            return

        selected_indices = [i for i, v in enumerate(self.album_vars) if v.get()]
        if not selected_indices:
            messagebox.showwarning("提示", "请至少勾选一个相册")
            return

        save_dir = self.dir_var.get().strip()
        if not save_dir:
            messagebox.showwarning("提示", "请选择保存目录")
            return

        self.downloading = True
        self.stop_flag = False
        self.download_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.fetch_btn.configure(state="disabled")

        def worker():
            try:
                self._do_download(selected_indices, save_dir)
            finally:
                self.root.after(0, self._download_finished)

        threading.Thread(target=worker, daemon=True).start()

    def _do_download(self, indices: list, save_dir: str):
        total_expected = sum(self.albums[i].get("total", 0) for i in indices)
        self.root.after(0, lambda: self.progress.configure(maximum=total_expected, value=0))

        downloaded = 0
        skipped = 0
        failed = 0
        current = 0

        for idx in indices:
            if self.stop_flag:
                break

            album = self.albums[idx]
            album_name = album.get("name", "未命名")
            album_id = album.get("id", "")
            album_total = album.get("total", 0)

            if album_total == 0:
                continue

            safe_name = sanitize_filename(album_name)
            album_dir = os.path.join(save_dir, safe_name)

            self._log_safe(f"▶ 正在处理: {album_name} ({album_total} 张)")

            # 获取照片列表（带 Cookie 过期重试）
            photos = None
            while photos is None:
                try:
                    photos = self.api.get_photos(album_id, album_total)
                except CookieExpiredError as e:
                    self._log_safe(f"  ⚠ {e}")
                    if not self._handle_cookie_expired():
                        return  # 用户取消
                except Exception as e:
                    self._log_safe(f"  ✗ 获取照片列表失败: {e}")
                    current += album_total
                    self.root.after(0, lambda v=current: self.progress.configure(value=v))
                    photos = []  # 跳过此相册
                    break

            if not photos:
                continue

            self._log_safe(f"  获取到 {len(photos)} 张照片信息")
            if len(photos) < album_total:
                self._log_safe(
                    f"  ⚠ 注意: API返回 {len(photos)} 张，相册显示 {album_total} 张"
                    f" (可能有 {album_total - len(photos)} 张未获取到)")

            # 通过 floatview 接口获取原图 URL
            self._log_safe(f"  正在获取原图链接(floatview)...")
            try:
                raw_count = self.api.enrich_with_floatview(
                    photos, album_id, log_func=self._log_safe)
                self._log_safe(f"  原图链接获取完成: {raw_count}/{len(photos)} 张有raw原图")
            except CookieExpiredError as e:
                self._log_safe(f"  ⚠ {e}")
                if not self._handle_cookie_expired():
                    return
                # 重试 floatview
                try:
                    raw_count = self.api.enrich_with_floatview(
                        photos, album_id, log_func=self._log_safe)
                    self._log_safe(f"  原图链接获取完成: {raw_count}/{len(photos)} 张有raw原图")
                except Exception:
                    self._log_safe(f"  floatview重试失败，将使用备选URL下载")

            # 开始逐张下载
            self._log_safe(f"  开始下载...")
            for photo_idx, photo in enumerate(photos):
                if self.stop_flag:
                    break

                url = self.api.get_original_url(photo)
                if not url:
                    self._log_safe(f"    [{photo_idx+1}] 无有效URL，跳过")
                    failed += 1
                    current += 1
                    self.root.after(0, lambda v=current: self.progress.configure(value=v))
                    continue

                # 生成唯一文件名：序号_唯一标识.扩展名
                # 使用照片的 picKey/lloc 作为唯一标识，确保每张照片对应唯一文件
                pic_id = photo.get("picKey") or photo.get("lloc") or ""
                # 从URL或phototype推断扩展名
                ext = ""
                parsed_url = urlparse(url)
                url_basename = os.path.basename(parsed_url.path)
                if url_basename and "." in url_basename:
                    ext = os.path.splitext(url_basename)[1].lower()
                if not ext or len(ext) > 5:
                    ext_map = {"1": ".jpg", "2": ".gif", "3": ".png", "5": ".bmp"}
                    ext = ext_map.get(str(photo.get("phototype", "")), ".jpg")

                # 文件名格式: 0001_picKey.jpg（序号确保排序，picKey确保唯一）
                if pic_id:
                    safe_id = sanitize_filename(pic_id)[:30]
                    photo_name = f"{photo_idx + 1:04d}_{safe_id}{ext}"
                else:
                    photo_name = f"{photo_idx + 1:04d}{ext}"

                save_path = os.path.join(album_dir, photo_name)

                # 断点续传：如果此文件已存在且有内容，说明上次已下载过这张
                if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                    skipped += 1
                    current += 1
                    self.root.after(0, lambda v=current: self.progress.configure(value=v))
                    continue

                # 下载（带 Cookie 过期检测）
                try:
                    success = self.api.download_photo(url, save_path)
                    if success:
                        downloaded += 1
                    else:
                        failed += 1
                except CookieExpiredError as e:
                    self._log_safe(f"  ⚠ 下载时Cookie过期: {e}")
                    if self._handle_cookie_expired():
                        try:
                            success = self.api.download_photo(url, save_path)
                            if success:
                                downloaded += 1
                            else:
                                failed += 1
                        except Exception:
                            failed += 1
                    else:
                        return  # 用户取消

                current += 1
                self.root.after(0, lambda v=current: self.progress.configure(value=v))

                # 每 5 张更新一次进度显示
                if (photo_idx + 1) % 5 == 0 or photo_idx == len(photos) - 1:
                    self.root.after(0, lambda d=downloaded, s=skipped, f=failed, pi=photo_idx+1, pt=len(photos):
                                   self.progress_label.configure(
                                       text=f"[{pi}/{pt}] 下载:{d} 跳过:{s} 失败:{f}"))

                time.sleep(0.3)

            # 相册完成后校验：统计磁盘上实际文件数
            if os.path.isdir(album_dir):
                actual_files = len([f for f in os.listdir(album_dir)
                                    if os.path.isfile(os.path.join(album_dir, f))
                                    and not f.endswith('.tmp')])
                if actual_files < len(photos):
                    self._log_safe(
                        f"  ⚠ 校验: 磁盘 {actual_files} 张 / API {len(photos)} 张"
                        f" (差 {len(photos) - actual_files} 张)")
                else:
                    self._log_safe(f"  ✓ 校验通过: 磁盘 {actual_files} 张")

        # 完成
        summary = f"下载完成！成功:{downloaded}  跳过:{skipped}  失败:{failed}"
        self._log_safe(f"\n{'='*40}\n{summary}")
        self._log_safe(f"保存位置: {save_dir}")

        if not self.stop_flag:
            self.root.after(0, lambda: messagebox.showinfo("完成", summary))

    def _handle_cookie_expired(self) -> bool:
        """处理 Cookie 过期：弹窗让用户输入新 Cookie。返回 True=已更新，False=用户取消"""
        new_cookie = self._ask_new_cookie()
        if not new_cookie:
            self._log_safe("用户取消，停止下载")
            self.stop_flag = True
            return False

        try:
            self.api.update_cookie(new_cookie)
            self._log_safe(f"[认证] Cookie 已更新，使用 {self.api.auth_type} g_tk={self.api.g_tk}")
            # 同时更新界面上的 Cookie 输入框
            self.root.after(0, lambda: self._update_cookie_display(new_cookie))
            return True
        except ValueError as e:
            self._log_safe(f"✗ 新Cookie无效: {e}")
            return False

    def _update_cookie_display(self, cookie: str):
        self.cookie_text.delete("1.0", "end")
        self.cookie_text.insert("1.0", cookie)

    def _stop_download(self):
        self.stop_flag = True
        self._log("正在停止...")

    def _download_finished(self):
        self.downloading = False
        self.download_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.fetch_btn.configure(state="normal")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
