# -*- coding: utf-8 -*-
"""Windows GUI: 按电脑本地时间，在指定时刻向前台窗口发送一次 Enter。无需第三方库。"""
import ctypes
import os
import sys
import tkinter as tk
from ctypes import wintypes
from datetime import datetime, timedelta
from tkinter import messagebox, ttk

if os.name != "nt":
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("系统不支持", "本工具仅适用于 Windows 系统。")
    sys.exit(1)

user32 = ctypes.WinDLL("user32", use_last_error=True)
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# Windows SendInput 所需的完整结构体（兼容 32 位及 64 位 Python）。
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]

class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL

VK_RETURN = 0x0D
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9


def window_title(hwnd):
    size = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(max(2, size + 1))
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value or "（无标题窗口）"


def belongs_to_this_tool(hwnd):
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


def send_enter():
    events = (INPUT * 2)()
    events[0].type = INPUT_KEYBOARD
    events[0].ki = KEYBDINPUT(VK_RETURN, 0, 0, 0, 0)
    events[1].type = INPUT_KEYBOARD
    events[1].ki = KEYBDINPUT(VK_RETURN, 0, KEYEVENTF_KEYUP, 0, 0)
    count = user32.SendInput(2, events, ctypes.sizeof(INPUT))
    if count != 2:
        error = ctypes.get_last_error()
        raise OSError(f"SendInput 只发送了 {count}/2 个事件（系统错误代码 {error}）。请检查目标是否以管理员权限运行。")


class EnterTimerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("定时按回车 - Windows")
        self.root.geometry("480x465")
        self.root.minsize(460, 460)
        self.root.resizable(False, False)
        self.root.configure(background="#f5f7fb")

        self.target_hwnd = None
        self.deadline = None
        self.capture_pending = False

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(9, 7))

        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="到指定时间，自动按一次 Enter", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(frame, text="按电脑当前时间执行，不修改系统时间。").pack(anchor="w", pady=(5, 14))

        timerow = ttk.Frame(frame)
        timerow.pack(fill="x", pady=(0, 10))
        ttk.Label(timerow, text="执行时间（24小时制）").pack(side="left")
        self.time_var = tk.StringVar(value="02:00:00")
        self.time_entry = ttk.Entry(timerow, textvariable=self.time_var, width=13, justify="center", font=("Consolas", 14))
        self.time_entry.pack(side="right")

        self.now_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.now_var).pack(anchor="w", pady=(0, 7))

        targetrow = ttk.Frame(frame)
        targetrow.pack(fill="x", pady=(8, 5))
        self.capture_btn = ttk.Button(targetrow, text="5秒后锁定目标窗口", command=self.capture_window)
        self.capture_btn.pack(side="left")
        ttk.Button(targetrow, text="清除锁定", command=self.clear_target).pack(side="left", padx=(8, 0))

        self.target_var = tk.StringVar(value="目标：未锁定（届时对最前面的窗口按 Enter）")
        ttk.Label(frame, textvariable=self.target_var, wraplength=430).pack(anchor="w", pady=(3, 15))

        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        self.start_btn = ttk.Button(actions, text="开始定时", command=self.start_timer)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.cancel_btn = ttk.Button(actions, text="取消定时", command=self.cancel_timer, state="disabled")
        self.cancel_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))

        self.countdown_var = tk.StringVar(value="未开始")
        ttk.Label(frame, textvariable=self.countdown_var, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(16, 7))
        self.status_var = tk.StringVar(value="设置好时间后，点击“开始定时”。")
        ttk.Label(frame, textvariable=self.status_var, wraplength=425).pack(anchor="w")

        ttk.Separator(frame).pack(fill="x", pady=(17, 10))
        ttk.Label(frame, text="提示：保持电脑开机、未睡眠，目标软件要保持登录并可操作。", wraplength=430, foreground="#666666").pack(anchor="w")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tick()

    def capture_window(self):
        if self.capture_pending:
            return
        self.capture_pending = True
        self.capture_btn.configure(state="disabled")
        self.target_var.set("请在 5 秒内切换到需要接收回车键的软件窗口……")
        self.root.after(5000, self.finish_capture)

    def finish_capture(self):
        self.capture_pending = False
        self.capture_btn.configure(state="normal")
        hwnd = user32.GetForegroundWindow()
        if not hwnd or belongs_to_this_tool(hwnd):
            self.target_hwnd = None
            self.target_var.set("锁定失败：请点击锁定后，在 5 秒内切换到目标软件。")
            return
        self.target_hwnd = hwnd
        self.target_var.set("已锁定：" + window_title(hwnd))

    def clear_target(self):
        self.target_hwnd = None
        self.target_var.set("目标：未锁定（届时对最前面的窗口按 Enter）")

    def start_timer(self):
        raw = self.time_var.get().strip()
        try:
            datetime.strptime(raw, "%H:%M:%S")
            hour, minute, second = [int(item) for item in raw.split(":")]
        except (ValueError, TypeError):
            messagebox.showerror("时间格式不正确", "请输入 24 小时格式，例如 02:00:00 或 14:30:15。")
            return
        now = datetime.now()
        deadline = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if deadline <= now:
            deadline += timedelta(days=1)
        self.deadline = deadline
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.time_entry.configure(state="disabled")
        self.status_var.set("已安排：" + deadline.strftime("%Y-%m-%d %H:%M:%S") + "，仅执行一次。")
        self.update_countdown()

    def cancel_timer(self):
        self.deadline = None
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.time_entry.configure(state="normal")
        self.countdown_var.set("已取消")
        self.status_var.set("任务已取消，不会发送回车键。")

    def update_countdown(self):
        if self.deadline is None:
            return
        remaining = max(0, int((self.deadline - datetime.now()).total_seconds() + 0.999))
        h, rest = divmod(remaining, 3600)
        m, s = divmod(rest, 60)
        self.countdown_var.set(f"距离执行：{h:02}:{m:02}:{s:02}")

    def fire(self):
        # 首先停用任务，防止意外执行多次。
        self.deadline = None
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.time_entry.configure(state="normal")
        self.countdown_var.set("执行完成" )

        hwnd = self.target_hwnd
        if hwnd:
            if not user32.IsWindow(hwnd):
                self.status_var.set("失败：目标窗口已经关闭。请重新锁定窗口。")
                self.countdown_var.set("执行失败")
                return
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            if user32.GetForegroundWindow() != hwnd:
                user32.SetForegroundWindow(hwnd)
            if user32.GetForegroundWindow() != hwnd:
                self.status_var.set("失败：Windows 未允许切换到锁定窗口。请保持目标窗口在前台。")
                self.countdown_var.set("执行失败")
                return
        else:
            hwnd = user32.GetForegroundWindow()
            if not hwnd or belongs_to_this_tool(hwnd):
                self.status_var.set("失败：当前在工具窗口中，无法安全地将 Enter 发送给目标软件。")
                self.countdown_var.set("执行失败")
                return

        try:
            send_enter()
            self.status_var.set("已在 " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " 向窗口「" + window_title(hwnd) + "」发送一次 Enter。")
        except Exception as exc:
            self.countdown_var.set("执行失败")
            self.status_var.set("发送失败：" + str(exc))

    def tick(self):
        now = datetime.now()
        self.now_var.set("电脑当前时间：" + now.strftime("%Y-%m-%d %H:%M:%S"))
        if self.deadline is not None:
            time_left = (self.deadline - now).total_seconds()
            if time_left <= 0:
                self.fire()
            else:
                self.update_countdown()
        if self.deadline is None:
            interval = 200
        else:
            remain = (self.deadline - datetime.now()).total_seconds()
            interval = 10 if remain < 2 else (50 if remain < 60 else 200)
        self.root.after(interval, self.tick)

    def on_close(self):
        if self.deadline is not None:
            if not messagebox.askyesno("确认退出", "退出工具会取消尚未执行的定时任务，确定退出吗？"):
                return
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    EnterTimerApp(root)
    root.mainloop()
