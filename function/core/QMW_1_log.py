import base64
import datetime

import cv2
import numpy
import numpy as np
from PyQt6 import QtWidgets, QtCore
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QApplication

from function.core.QMW_0_load_ui_file import QMainWindowLoadUI
from function.globals import SIGNAL
from function.globals.get_paths import PATHS
from function.globals.log import CUS_LOGGER


class QMainWindowLog(QMainWindowLoadUI):
    signal_dialog = pyqtSignal(str, str)  # 标题, 正文
    signal_print_to_ui = pyqtSignal(str, str, bool)
    signal_image_to_ui = pyqtSignal(numpy.ndarray)

    def __init__(self):
        # 继承父类构造方法
        super().__init__()

        # 链接防呆弹窗
        self.signal_dialog.connect(self.show_dialog)

        # 并不是直接输出, 其emit方法是 一个可以输入 缺省的颜色 或 时间参数 来生成文本 调用 signal_print_to_ui_1
        SIGNAL.PRINT_TO_UI = self.MidSignalPrint(signal_1=self.signal_print_to_ui, theme=self.theme)

        # 真正的 发送信息激活 print 的函数, 被链接到直接发送信息到ui的函数
        self.signal_print_to_ui.connect(self.print_to_ui)

        # 用于支持 输入 路径 或 numpy.ndarray
        SIGNAL.IMAGE_TO_UI = self.MidSignalImage(signal_1=self.signal_image_to_ui)

        # 真正的 发送图片到ui的函数, 被链接到直接发送图片到ui的函数
        self.signal_image_to_ui.connect(self.image_to_ui)

        # 储存在全局
        SIGNAL.DIALOG = self.signal_dialog

        # 打印默认输出提示
        self.start_print()

    class MidSignalPrint:
        """
        模仿信号的类, 但其实本身完全不是信号, 是为了可以接受缺省参数而模仿的中间类,
        该类的emit方法是 一个可以输入 缺省的颜色 或 时间参数 来生成文本的方法
        并调用信号发送真正的信息
        """

        def __init__(self, signal_1, theme):
            super().__init__()
            self.signal_1 = signal_1
            match theme:
                case 'light':
                    self.color_scheme = {
                        1: "C80000",  # 深红色
                        2: "E67800",  # 深橙色暗调
                        3: "006400",  # 深绿色
                        4: "009688",  # 深宝石绿
                        5: "0056A6",  # 深海蓝
                        6: "003153",  # 普鲁士蓝
                        7: "5E2D79",  # 深兰花紫
                        8: "4B0082",  # 靛蓝
                        9: "333333",  # 煤黑色
                    }
                case 'dark':
                    self.color_scheme = {
                        1: "FF4C4C",  # 鲜红色
                        2: "FFA500",  # 橙色
                        3: "00FF00",  # 亮绿色
                        4: "20B2AA",  # 浅海绿色
                        5: "1E90FF",  # 道奇蓝
                        6: "4682B4",  # 钢蓝色
                        7: "9370DB",  # 中兰花紫
                        8: "8A2BE2",  # 蓝紫色
                        9: "CCCCCC",  # 浅灰色
                    }

        def emit(self, text, color_level=9, color=None, time=True):
            if color_level in self.color_scheme:
                color = self.color_scheme[color_level]
            elif not color:
                color = self.color_scheme[9]

            # 处理缺省参数
            self.signal_1.emit(text, color, time)

    class MidSignalImage:
        """
        模仿信号的类, 但其实本身完全不是信号, 是为了可以接受缺省参数而模仿的中间类,
        该类的emit方法是 一个可以输入 numpy.ndarray 或 图片路径 并判断是否读取的方法
        并调用信号发送真正的图片
        """

        def __init__(self, signal_1):
            super().__init__()
            self.signal_1 = signal_1

        def emit(self, image):
            # 根据 路径 或者 numpy.ndarray 选择是否读取
            if type(image) is not np.ndarray:
                # 读取目标图像,中文路径兼容方案
                image_ndarray = cv2.imdecode(buf=np.fromfile(file=image, dtype=np.uint8), flags=-1)
            else:
                image_ndarray = image
            # 处理缺省参数
            self.signal_1.emit(image_ndarray)

    def start_print(self):
        """打印默认输出提示"""

        SIGNAL.PRINT_TO_UI.emit(
            text="嗷呜, 欢迎使用FAA-美食大战老鼠自动放卡作战小助手~",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="本软件 [开源][免费][绿色]",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="使用安全说明",
            color_level=1,
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[1] 务必有二级密码",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[2] 有一定的礼卷防翻牌异常",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[3] 高星或珍贵不绑卡放储藏室",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[4] 鼠标不在游戏内, 不玩有反外挂的游戏",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="使用疑难解决",
            color_level=1,
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[在线文档]  https://stareabyss.top/FAA-WebSite/",
            color_level=2,
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="新手入门 / 除错 / 深入使用 / 开发者, 都可以在线文档找到相关内容",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="鼠标在文字或按钮上悬停一会, 会显示部分有用的提示信息哦~",
            color_level=2,
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="任务或定时器开始运行后, 将锁定点击按钮时的配置文件, 不应用实时更改",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="FAA会向服务器发送战利品掉落Log以做掉落统计, 不传输<任何>其他内容",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="相关链接",
            color_level=1,
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[Github]  https://github.com/StareAbyss/FoodsVsMiceAutoAssistant",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[Github]  开源不易, 为我点个Star吧! 发送Issues是最有效的问题反馈渠道",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[B站][UP直视深淵][宣传]  https://www.bilibili.com/video/BV1owUFYHEPq/",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[B站]  速速一键三连辣!",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[交流QQ群]  1群: 786921130  2群: 142272678 (推荐, 但比较爆满)",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[交流QQ群]  欢迎加入, 交流游戏和自动化 & 获取使用帮助 & 参与开发!",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[腾讯频道]  https://pd.qq.com/s/a0h4rujt0",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[腾讯频道]  欢迎加入, 用以下载 / 公告 / 提交问题. (目前人少维护较差)",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="支持FAA",
            color_level=1,
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="应用程序的开发和维护不仅耗时, 还需投入资金, 但FAA免费开放供大家使用",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="如果使用满意, 并想要表达感激之情或支持后续版本完善",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text=" 那么您的鼓励就是是FAA 持(不)续(跑)开(路)发 的最大动力!",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="您可以选择以下任意一种方式进行捐赠. 赞助时, 可留下您的称呼以供致谢 ~",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[微信-赞赏码]  下方直接扫码即可. (推荐)",
            time=False)
        SIGNAL.PRINT_TO_UI.emit(
            text="[QQ-红包]  加入讨论QQ群后直接发送即可, 以防高仿.",
            time=False)

        SIGNAL.IMAGE_TO_UI.emit(
            image=PATHS["logo"] + "\\赞赏码.png")

    # 用于展示弹窗信息的方法
    @QtCore.pyqtSlot(str, str)
    def show_dialog(self, title, message):
        msg = QtWidgets.QMessageBox()
        msg.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        msg.setWindowTitle(title)
        msg.setText(message)
        msg.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
        msg.exec()

    def print_to_ui(self, text, color, time):
        """打印文本到输出框 """

        # 时间文本
        text_time = "[{}] ".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")) if time else ""

        # 颜色文本
        text_all = f'<span style="color:#{color};">{text_time}{text}</span>'

        # 输出到输出框
        self.TextBrowser.append(text_all)

        # 实时输出
        cursor = self.TextBrowser.textCursor()
        cursor.setPosition(cursor.position(), QTextCursor.MoveMode.MoveAnchor)
        cursor.setPosition(cursor.position() + 1, QTextCursor.MoveMode.KeepAnchor)  # 移动到末尾
        self.TextBrowser.setTextCursor(cursor)
        QApplication.processEvents()

        # 输出到日志和运行框
        CUS_LOGGER.info(text)

    def image_to_ui(self, image_ndarray: numpy.ndarray):
        """
        :param image_ndarray: 必须是 numpy.ndarray 对象
        :return:
        """

        # 編碼字節流
        _, img_encoded = cv2.imencode('.png', image_ndarray)

        # base64
        img_base64 = base64.b64encode(img_encoded).decode('utf-8')

        image_html = f"<img src='data:image/png;base64,{img_base64}'>"

        # self.TextBrowser.insertHtml(image_html)

        # 输出到输出框
        self.TextBrowser.append(image_html)

        # 实时输出
        cursor = self.TextBrowser.textCursor()
        cursor.setPosition(cursor.position(), QTextCursor.MoveMode.MoveAnchor)
        cursor.setPosition(cursor.position() + 1, QTextCursor.MoveMode.KeepAnchor)  # 移动到末尾
        self.TextBrowser.setTextCursor(cursor)
        QApplication.processEvents()
