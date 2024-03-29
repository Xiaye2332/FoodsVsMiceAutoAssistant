# 鼠标点击队列，包含处理队列和添加队列
import queue
import sys
from ctypes import windll
from string import printable

from PyQt5.QtCore import QTimer, QThread
from PyQt5.QtWidgets import QApplication, QMainWindow

from function.scattered.gat_handle import faa_get_handle


class ThreadActionQueueTimer(QThread):
    """
    线程类
    包含一个队列和定时器，队列用于储存需要进行的操作，定时器用于绑定点击函数执行队列中的操作
    """

    def __init__(self):
        super().__init__()
        self.action_timer = None
        self.zoom_rate = None
        self.action_queue = queue.Queue()
        self.PostMessageW = windll.user32.PostMessageW
        # 按键名称和虚拟键码对应表
        self.VkCode = {
            "l_button": 0x01,  # 鼠标左键
            "r_button": 0x02,  # 鼠标右键
            "backspace": 0x08,
            "tab": 0x09,
            "return": 0x0D,
            "shift": 0x10,
            "control": 0x11,  # ctrl
            "menu": 0x12,
            "pause": 0x13,
            "capital": 0x14,
            "enter": 0x0D,  # 回车键
            "escape": 0x1B,  # ESC
            "space": 0x20,
            "end": 0x23,
            "home": 0x24,
            "left": 0x25,
            "up": 0x26,
            "right": 0x27,
            "down": 0x28,
            "print": 0x2A,
            "snapshot": 0x2C,
            "insert": 0x2D,
            "delete": 0x2E,
            "0": 0x30,  # 主键盘0
            "1": 0x31,  # 主键盘1
            "2": 0x32,  # 主键盘2
            "3": 0x33,  # 主键盘3
            "4": 0x34,  # 主键盘4
            "5": 0x35,  # 主键盘5
            "6": 0x36,  # 主键盘6
            "7": 0x37,  # 主键盘7
            "8": 0x38,  # 主键盘8
            "9": 0x39,  # 主键盘9
            "left_win": 0x5B,
            "right_win": 0x5C,
            "num0": 0x60,  # 数字键盘0
            "num1": 0x61,  # 数字键盘1
            "num2": 0x62,  # 数字键盘2
            "num3": 0x63,  # 数字键盘3
            "num4": 0x64,  # 数字键盘4
            "num5": 0x65,  # 数字键盘5
            "num6": 0x66,  # 数字键盘6
            "num7": 0x67,  # 数字键盘7
            "num8": 0x68,  # 数字键盘8
            "num9": 0x69,  # 数字键盘9
            "multiply": 0x6A,  # 数字键盘乘键
            "add": 0x6B,
            "separator": 0x6C,
            "subtract": 0x6D,
            "decimal": 0x6E,
            "divide": 0x6F,
            "f1": 0x70,
            "f2": 0x71,
            "f3": 0x72,
            "f4": 0x73,
            "f5": 0x74,
            "f6": 0x75,
            "f7": 0x76,
            "f8": 0x77,
            "f9": 0x78,
            "f10": 0x79,
            "f11": 0x7A,
            "f12": 0x7B,
            "numlock": 0x90,
            "scroll": 0x91,
            "left_shift": 0xA0,
            "right_shift": 0xA1,
            "left_control": 0xA2,
            "right_control": 0xA3,
            "left_menu": 0xA4,
            "right_menu": 0XA5
        }

    def run(self):
        self.action_timer = QTimer()  # 不能放在init方法里，否则无效果
        self.action_timer.timeout.connect(self.execute_click_queue)
        self.action_timer.start(15)
        self.exec()  # 开始事件循环

    def stop(self):
        self.action_queue.queue.clear()
        self.action_timer.stop()
        self.quit()

    def execute_click_queue(self):
        if not self.action_queue.empty():
            # 获取任务
            d_type, handle, args = self.action_queue.get()
            # 执行任务
            self.do_something(d_type=d_type, handle=handle, args=args)
            # 标记任务已完成
            self.action_queue.task_done()

    def add_click_to_queue(self, handle, x, y):
        self.action_queue.put(("click", handle, [x, y]))

        # print("鼠标左键点击添加到队列")

    def add_move_to_queue(self, handle, x, y):
        self.action_queue.put(("move_to", handle, [x, y]))

        # print("鼠标移动添加到队列")

    def add_keyboard_up_down_to_queue(self, handle, key):
        self.action_queue.put(("keyboard_up_down", handle, [key]))

    def do_something(self, d_type, handle, args):
        """执行动作任务函数"""
        if d_type == "click":
            self.do_left_mouse_click(handle=handle, x=args[0], y=args[1])
        elif d_type == "move_to":
            self.do_left_mouse_move_to(handle=handle, x=args[0], y=args[1])
        elif d_type == "keyboard_up_down":
            self.do_keyboard_up_down(handle=handle, key=args[0])

    def do_left_mouse_click(self, handle, x, y):
        """执行动作函数 子函数"""
        x = int(x * self.zoom_rate)
        y = int(y * self.zoom_rate)
        self.PostMessageW(handle, 0x0201, 0, y << 16 | x)
        self.PostMessageW(handle, 0x0202, 0, y << 16 | x)

    def do_left_mouse_move_to(self, handle, x, y):
        """执行动作函数 子函数"""
        x = int(x * self.zoom_rate)
        y = int(y * self.zoom_rate)
        self.PostMessageW(handle, 0x0200, 0, y << 16 | x)

    def do_keyboard_up_down(self, handle, key):
        """执行动作函数 子函数"""

        # 根据按键名获取虚拟按键码
        if len(key) == 1 and key in printable:
            vk_code = windll.user32.VkKeyScanA(ord(key)) & 0xff
        else:
            vk_code = self.VkCode[key]

        scan_code = windll.user32.MapVirtualKeyW(vk_code, 0)

        # 按下
        self.PostMessageW(handle, 0x100, vk_code, (scan_code << 16) | 1)
        # 松开
        self.PostMessageW(handle, 0x101, vk_code, (scan_code << 16) | 0XC0000001)

    def set_zoom_rate(self, zoom_rate):
        if __name__ == '__main__':
            self.zoom_rate = 1.0
        else:
            self.zoom_rate = zoom_rate


# 实例化为全局线程
T_ACTION_QUEUE_TIMER = ThreadActionQueueTimer()

if __name__ == '__main__':
    class ActionThread(QThread):
        """模拟FAA类"""

        def __init__(self):
            super().__init__()
            global T_ACTION_QUEUE_TIMER

        def run(self):
            handle = faa_get_handle(channel="锑食")

            T_ACTION_QUEUE_TIMER.add_click_to_queue(handle=handle, x=100, y=100)
            QThread.msleep(2000)

            T_ACTION_QUEUE_TIMER.add_click_to_queue(handle=handle, x=100, y=100)
            T_ACTION_QUEUE_TIMER.add_click_to_queue(handle=handle, x=100, y=100)
            QThread.msleep(2000)

            T_ACTION_QUEUE_TIMER.add_click_to_queue(handle=handle, x=100, y=100)
            QThread.msleep(2000)

            T_ACTION_QUEUE_TIMER.add_click_to_queue(handle=handle, x=100, y=100)


    class MainWindow(QMainWindow):
        """模拟窗口主线程"""

        def __init__(self):
            super().__init__()
            self.initUI()
            global T_ACTION_QUEUE_TIMER
            self.t2 = ActionThread()

        def initUI(self):
            self.setWindowTitle('计时器示例')
            self.setGeometry(300, 300, 250, 150)

        def do_something(self):
            self.t2.start()
            T_ACTION_QUEUE_TIMER.set_zoom_rate(1.0)
            T_ACTION_QUEUE_TIMER.start()


    """模拟启动"""
    app = QApplication(sys.argv)

    main_win = MainWindow()
    main_win.show()
    main_win.do_something()

    sys.exit(app.exec_())

"""
外部调用示例
T_CLICK_QUEUE_TIMER.add_to_click_queue(handle=self.handle, x=920, y=422)
QThread.msleep(200)
"""