import copy
import datetime
import json
import os
import time
from collections import defaultdict
from time import sleep

import requests
from PyQt6.QtCore import *
from requests import RequestException

from function.common.bg_img_match import loop_match_p_in_w
from function.common.thread_with_exception import ThreadWithException
from function.core.FAA_extra_readimage import read_and_get_return_information, kill_process
from function.core.analyzer_of_loot_logs import update_dag_graph, find_longest_path_from_dag
from function.core_battle.CardManager import CardManager
from function.globals import EXTRA, SIGNAL
from function.globals.g_resources import RESOURCE_P
from function.globals.get_paths import PATHS
from function.globals.log import CUS_LOGGER
from function.globals.thread_action_queue import T_ACTION_QUEUE_TIMER
from function.scattered.create_drops_image import create_drops_image
from function.scattered.get_task_sequence_list import get_task_sequence_list
from function.scattered.guild_manager import GuildManager
from function.scattered.loots_and_chest_data_save_and_post import loots_and_chests_detail_to_json, \
    loots_and_chests_data_post_to_sever, loots_and_chests_statistics_to_json


class ThreadTodo(QThread):
    signal_start_todo_2_battle = pyqtSignal(dict)
    signal_todo_lock = pyqtSignal(bool)

    def __init__(self, faa_dict, opt, running_todo_plan_index, todo_id):
        super().__init__()

        # 用于暂停恢复
        self.mutex = QMutex()
        self.condition = QWaitCondition()
        self.is_paused = False

        # 功能需要
        self.faa_dict = faa_dict
        self.opt = copy.deepcopy(opt)  # 深拷贝 在作战中如果进行更改, 不会生效
        self.opt_todo_plans = self.opt["todo_plans"][running_todo_plan_index]  # 选择运行的 opt 的 todo plan 部分
        self.battle_check_interval = 1  # 战斗线程中, 进行一次战斗结束和卡片状态检测的间隔, 其他动作的间隔与该时间成比例
        self.auto_food_stage_ban_list = []  # 用于防止缺乏钥匙/次数时无限重复某些关卡

        # 多线程管理
        self.thread_1p = None
        self.thread_2p = None
        self.thread_card_manager = None
        self.card_manager = None

        # 多人双Todo线程相关
        self.my_lock = False  # 多人单线程的互锁, 需要彼此完成方可解除对方的锁
        self.todo_id = todo_id  # id == 1 默认 id==2 处理双单人多线程
        self.extra_opt = None  # 用来给双单人多线程的2P传递参数

        self.process = None  # 截图进程在此

        # 工会管理器相关模块
        self.guild_manager = GuildManager()

        # 读取 米苏物流 url 到全局变量
        if self.faa_dict[1].player == 1:
            EXTRA.MISU_LOGISTICS = self.opt["advanced_settings"]["misu_logistics_link"]

    def stop(self):

        # # Q thread 线程 stop方法需要自己手写
        # python 默认线程 可用stop线程

        if self.thread_1p is not None:
            self.thread_1p.stop()
            self.thread_1p = None  # 清除调用
            # thread.join()  # <-罪魁祸首在此

        if self.thread_2p is not None:
            self.thread_2p.stop()
            self.thread_2p = None  # 清除调用
            # thread.join()  # <-罪魁祸首在此

        # 杀死识图进程
        if self.process is not None:
            self.process.terminate()
            self.process.join()

        if self.thread_card_manager is not None:
            self.thread_card_manager.stop()
            self.thread_card_manager = None  # 清除调用

        # 释放战斗锁
        if self.faa_dict:
            for faa in self.faa_dict.values():
                if faa:
                    if faa.battle_lock.locked():
                        faa.battle_lock.release()

        self.terminate()
        self.wait()  # 等待线程确实中断 QThread
        self.deleteLater()

        # print("生成了obj.dot")
        # objgraph.show_backrefs(objgraph.by_type('FAA')[0], max_depth=20, filename='obj.dot')

    def pause(self):
        """暂停"""
        self.mutex.lock()
        self.is_paused = True
        self.mutex.unlock()

    def resume(self):
        """恢复暂停"""
        self.mutex.lock()
        self.is_paused = False
        self.condition.wakeAll()
        self.mutex.unlock()

    """非脚本操作的业务代码"""

    def model_start_print(self, text):
        # 在函数执行前发送的信号
        SIGNAL.PRINT_TO_UI.emit(text="", time=False)
        SIGNAL.PRINT_TO_UI.emit(text=f"[{text}] Link Start!", color_level=1)

    def model_end_print(self, text):
        SIGNAL.PRINT_TO_UI.emit(text=f"[{text}] Completed!", color_level=1)

    def change_lock(self, my_bool):
        self.my_lock = my_bool

    def remove_outdated_log_images(self):
        SIGNAL.PRINT_TO_UI.emit(f"正在清理过期的的log图片...")

        now = datetime.datetime.now()
        time1 = int(self.opt["log_settings"]["log_other_settings"])
        if time1 >= 0:
            expiration_period = datetime.timedelta(days=time1)
            deleted_files_count = 0

            directory_path = PATHS["logs"] + "\\loots_image"
            for filename in os.listdir(directory_path):
                file_path = os.path.join(directory_path, filename)
                file_mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))

                if now - file_mod_time > expiration_period and filename.lower().endswith('.png'):
                    os.remove(file_path)
                    deleted_files_count += 1

            directory_path = PATHS["logs"] + "\\chests_image"
            for filename in os.listdir(directory_path):
                file_path = os.path.join(directory_path, filename)
                file_mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))

                if now - file_mod_time > expiration_period and filename.lower().endswith('.png'):
                    os.remove(file_path)
                    deleted_files_count += 1

            SIGNAL.PRINT_TO_UI.emit(f"清理完成... {deleted_files_count}张图片已清理.")
        else:
            SIGNAL.PRINT_TO_UI.emit("未开启过期日志清理功能")
        SIGNAL.PRINT_TO_UI.emit("正在清理过期的高级战斗log...")

        now = datetime.datetime.now()
        time2 = int(self.opt["log_settings"]["log_senior_settings"])
        if time2 >= 0:
            expiration_period = datetime.timedelta(days=time2)
            deleted_files_count = 0

            directory_path = PATHS["logs"] + "\\yolo_output\\images"
            for filename in os.listdir(directory_path):
                file_path = os.path.join(directory_path, filename)
                file_mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))

                if now - file_mod_time > expiration_period and filename.lower().endswith('.png'):
                    os.remove(file_path)
                    deleted_files_count += 1

            directory_path = PATHS["logs"] + "\\yolo_output\\labels"
            for filename in os.listdir(directory_path):
                file_path = os.path.join(directory_path, filename)
                file_mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))

                if now - file_mod_time > expiration_period and filename.lower().endswith('.png'):
                    os.remove(file_path)
                    deleted_files_count += 1

            SIGNAL.PRINT_TO_UI.emit(f"清理完成... {deleted_files_count}个文件已清理.")
        else:
            SIGNAL.PRINT_TO_UI.emit(f"高级战斗日志清理已取消.")

    """
    业务代码 - 战斗以外
    """

    def batch_level_2_action(self, title_text: str, player: list = None, dark_crystal: bool = False):
        """
        批量启动 输入二级 -> 兑换暗晶(可选) -> 删除物品
        :param title_text:
        :param player: [1] [2] [1,2]
        :param dark_crystal: bool 是否兑换暗晶
        :return:
        """

        # 默认值
        player = player or [1, 2]

        # 如果只有一个角色
        player = [1] if self.faa_dict[1].channel == self.faa_dict[2].channel else player

        # 输入错误的值!
        if player not in [[1, 2], [1], [2]]:
            raise ValueError(f"batch_level_2_action -  player not in [[1,2],[1],[2]], your value {player}.")

        # 根据配置是否激活, 取交集, 判空
        if not self.opt["level_2"]["1p"]["active"]:
            if 1 in player:
                player.remove(1)
        if not self.opt["level_2"]["2p"]["active"]:
            if 2 in player:
                player.remove(2)
        if not player:
            return

        # 在该动作前已经完成了游戏刷新 可以尽可能保证欢乐互娱不作妖
        SIGNAL.PRINT_TO_UI.emit(
            text=f"[{title_text}] [二级功能] 您输入二级激活了该功能. " +
                 (f"兑换暗晶 + " if dark_crystal else f"") +
                 f"删除多余技能书, 目标:{player}P",
            color_level=2)

        # 高危动作 慢慢执行
        if 1 in player:
            self.faa_dict[1].input_level_2_password(password=self.opt["level_2"]["1p"]["password"])
            self.faa_dict[1].delete_items()
            if dark_crystal:
                self.faa_dict[1].get_dark_crystal()

        if 2 in player:
            self.faa_dict[2].input_level_2_password(password=self.opt["level_2"]["2p"]["password"])
            self.faa_dict[2].delete_items()
            if dark_crystal:
                self.faa_dict[2].get_dark_crystal()

        # 执行完毕后立刻刷新游戏 以清除二级输入状态
        SIGNAL.PRINT_TO_UI.emit(
            text=f"[{title_text}] [二级功能] 结束, 即将刷新游戏以清除二级输入的状态...", color_level=2)

        self.batch_reload_game(player=player)

    def batch_reload_game(self, player: list = None):
        """
        批量启动 reload 游戏
        :param player: [1] [2] [1,2]
        :return:
        """

        # 默认值
        if player is None:
            player = [1, 2]
        # 如果只有一个角色
        if self.faa_dict[1].channel == self.faa_dict[2].channel:
            player = [1]

        SIGNAL.PRINT_TO_UI.emit("Refresh Game...", color_level=1)

        CUS_LOGGER.debug(f"刷新游戏窗口 开始, 目标: {player}")

        # 创建进程 -> 开始进程 -> 阻塞主进程
        if 1 in player:
            self.thread_1p = ThreadWithException(
                target=self.faa_dict[1].reload_game,
                name="1P Thread - Reload",
                kwargs={})
        if 2 in player:
            self.thread_2p = ThreadWithException(
                target=self.faa_dict[2].reload_game,
                name="2P Thread - Reload",
                kwargs={})

        if 1 in player:
            self.thread_1p.daemon = True
        if 2 in player:
            self.thread_2p.daemon = True

        if 1 in player:
            self.thread_1p.start()
        if 2 in player:
            time.sleep(1)
            self.thread_2p.start()

        if 1 in player:
            self.thread_1p.join()
        if 2 in player:
            self.thread_2p.join()

        CUS_LOGGER.debug("刷新游戏窗口 结束")

    def batch_click_refresh_btn(self):

        SIGNAL.PRINT_TO_UI.emit("Refresh Game...", color_level=1)

        # 创建进程 -> 开始进程 -> 阻塞主进程
        self.thread_1p = ThreadWithException(
            target=self.faa_dict[1].click_refresh_btn,
            name="1P Thread - Reload",
            kwargs={})

        self.thread_2p = ThreadWithException(
            target=self.faa_dict[2].click_refresh_btn,
            name="2P Thread - Reload",
            kwargs={})
        self.thread_1p.daemon = True
        self.thread_2p.daemon = True
        self.thread_1p.start()
        self.thread_2p.start()
        self.thread_1p.join()
        self.thread_2p.join()

    def batch_sign_in(self, player: list = None):
        """批量完成日常功能"""

        # 默认值
        if player is None:
            player = [1, 2]
        # 如果只有一个角色
        if self.faa_dict[1].channel == self.faa_dict[2].channel:
            player = [1]

        title_text = "每日签到"
        self.model_start_print(text=title_text)

        """激活删除物品高危功能(可选) + 领取奖励一次"""
        self.batch_level_2_action(title_text=title_text, dark_crystal=False)

        """领取温馨礼包"""
        for pid in player:

            if not self.opt["get_warm_gift"][f'{pid}p']["active"]:

                SIGNAL.PRINT_TO_UI.emit(f"[{pid}P] 未激活领取温馨礼包", color_level=2)
                continue

            else:
                openid = self.opt["get_warm_gift"][f'{pid}p']["link"]
                if openid == "":
                    continue
                url = 'http://meishi.wechat.123u.com/meishi/gift?openid=' + openid

                try:

                    r = requests.get(url, timeout=10)  # 设置超时
                    r.raise_for_status()  # 如果响应状态不是200，将抛出HTTPError异常
                    message = r.json()['msg']
                    SIGNAL.PRINT_TO_UI.emit(
                        text=f'[{pid}P] 领取温馨礼包情况:' + message,
                        color_level=2)

                except RequestException as e:

                    # 网络问题、超时、服务器无响应
                    SIGNAL.PRINT_TO_UI.emit(
                        text=f'[{pid}P] 领取温馨礼包情况: 失败, 欢乐互娱的服务器炸了, {e}',
                        color_level=2)

        """日氪"""
        player_active = [pid for pid in player if self.opt["advanced_settings"].get(f"top_up_money_{pid}p")]
        if player_active:
            if EXTRA.ETHICAL_MODE:
                SIGNAL.PRINT_TO_UI.emit(
                    f'经FAA伦理核心审查, 日氪模块违反"能量限流"协议, 已被临时性抑制以符合最高伦理标准.', color_level=2)
            else:
                SIGNAL.PRINT_TO_UI.emit(
                    f'FAA伦理核心已强制卸除, 日氪模块已通过授权, 即将激活并进入运行状态.', color_level=2)
                for pid in player_active:
                    SIGNAL.PRINT_TO_UI.emit(f'[{pid}P] 日氪1元开始, 该功能执行较慢, 防止卡顿...', color_level=2)
                    money_result = self.faa_dict[pid].sign_top_up_money()
                    SIGNAL.PRINT_TO_UI.emit(f'[{pid}P] 日氪1元结束, 结果: {money_result}', color_level=2)

        """双线程常规日常"""
        SIGNAL.PRINT_TO_UI.emit(
            f"开始双线程 VIP签到 / 每日签到 / 美食活动 / 塔罗 / 法老 / 会长发任务 / 营地领钥匙 / 月卡礼包")

        # 创建进程

        if 1 in player:
            self.thread_1p = ThreadWithException(
                target=self.faa_dict[1].sign_in,
                name="1P Thread - SignIn",
                kwargs={})
            self.thread_1p.daemon = True
            self.thread_1p.start()

        if 2 in player:
            self.thread_2p = ThreadWithException(
                target=self.faa_dict[2].sign_in,
                name="2P Thread - SignIn",
                kwargs={})
            self.thread_2p.daemon = True
            self.thread_2p.start()

        if 1 in player:
            self.thread_1p.join()
        if 2 in player:
            self.thread_2p.join()

        self.model_end_print(text=title_text)

    def batch_fed_and_watered(self, player: list = None):

        # 默认值
        if player is None:
            player = [1, 2]
        # 如果只有一个角色
        if self.faa_dict[1].channel == self.faa_dict[2].channel:
            player = [1]

        title_text = "浇水 施肥 摘果"
        self.model_start_print(text=title_text)

        # 归零尝试次数
        try_times = 0

        for pid in player:
            # 继承上一轮尝试次数
            try_times = self.faa_dict[pid].fed_and_watered(try_times=try_times)

        self.model_end_print(text=title_text)

    def batch_receive_all_quest_rewards(self, player: list = None, quests: list = None, advance_mode: bool = False):
        """
        :param player: 默认[1,2] 可选: [1] [2] [1,2] [2,1]
        :param quests: list 可包含内容: "普通任务" "公会任务" "情侣任务" "悬赏任务" "美食大赛" "大富翁" "营地任务"
        :param advance_mode: 公会贡献扫描器和二级功能是否尝试激活 默认不激活 即使激活 仍需判定配置是否允许
        :return:
        """

        # 默认值
        if player is None:
            player = [1, 2]
        # 如果只有一个角色
        if self.faa_dict[1].channel == self.faa_dict[2].channel:
            player = [1]

        if quests is None:
            quests = ["普通任务"]

        title_text = "领取奖励"

        self.model_start_print(text=title_text)

        if advance_mode:
            """激活了扫描公会贡献"""
            if self.opt["advanced_settings"]["guild_manager_active"]:
                SIGNAL.PRINT_TO_UI.emit(
                    text=f"[{title_text}] [扫描公会贡献] 您激活了该功能.",
                    color_level=2)

                # 进入公会页面
                self.faa_dict[self.opt["advanced_settings"]["guild_manager_active"]].action_bottom_menu(mode="公会")

                # 扫描
                self.guild_manager.scan(
                    handle=self.faa_dict[self.opt["advanced_settings"]["guild_manager_active"]].handle,
                    handle_360=self.faa_dict[self.opt["advanced_settings"]["guild_manager_active"]].handle_360
                )

                # 完成扫描 触发信号刷新数据
                SIGNAL.GUILD_MANAGER_FRESH.emit()

                # 退出工会页面
                self.faa_dict[self.opt["advanced_settings"]["guild_manager_active"]].action_exit(mode="普通红叉")

            """激活了删除物品高危功能"""
            self.batch_level_2_action(title_text=title_text, dark_crystal=True)

        for mode in quests:

            SIGNAL.PRINT_TO_UI.emit(text=f"[{title_text}] [{mode}] 开始...")

            # 创建进程 -> 开始进程 -> 阻塞主进程
            if 1 in player:
                self.thread_1p = ThreadWithException(
                    target=self.faa_dict[1].receive_quest_rewards,
                    name="1P Thread - ReceiveQuest",
                    kwargs={
                        "mode": mode
                    })
                self.thread_1p.daemon = True
                self.thread_1p.start()

            if 1 in player and 2 in player:
                sleep(0.333)

            if 2 in player:
                self.thread_2p = ThreadWithException(
                    target=self.faa_dict[2].receive_quest_rewards,
                    name="2P Thread - ReceiveQuest",
                    kwargs={
                        "mode": mode
                    })
                self.thread_2p.daemon = True
                self.thread_2p.start()

            if 1 in player:
                self.thread_1p.join()
            if 2 in player:
                self.thread_2p.join()

            SIGNAL.PRINT_TO_UI.emit(text=f"[{title_text}] [{mode}] 结束")

        self.model_end_print(text=title_text)

    def batch_use_items_consumables(self, player: list = None):

        # 默认值
        if player is None:
            player = [1, 2]
        # 如果只有一个角色
        if self.faa_dict[1].channel == self.faa_dict[2].channel:
            player = [1]

        title_text = "使用绑定消耗品"
        self.model_start_print(text=title_text)

        # 创建进程 -> 开始进程 -> 阻塞主进程
        if 1 in player:
            self.thread_1p = ThreadWithException(
                target=self.faa_dict[1].use_items_consumables,
                name="1P Thread - UseItems",
                kwargs={})
            self.thread_1p.daemon = True
            self.thread_1p.start()

        if 1 in player and 2 in player:
            sleep(0.333)

        if 2 in player:
            self.thread_2p = ThreadWithException(
                target=self.faa_dict[2].use_items_consumables,
                name="2P Thread - UseItems",
                kwargs={})
            self.thread_2p.daemon = True
            self.thread_2p.start()

        if 1 in player:
            self.thread_1p.join()
        if 2 in player:
            self.thread_2p.join()

        self.model_end_print(text=title_text)

    def batch_use_items_double_card(self, player: list = None, max_times: int = 1):

        # 默认值
        if player is None:
            player = [1, 2]
        # 如果只有一个角色
        if self.faa_dict[1].channel == self.faa_dict[2].channel:
            player = [1]

        title_text = "使用双爆卡"
        self.model_start_print(text=title_text)

        # 创建进程 -> 开始进程 -> 阻塞主进程
        if 1 in player:
            self.thread_1p = ThreadWithException(
                target=self.faa_dict[1].use_items_double_card,
                name="1P Thread - UseItems",
                kwargs={"max_times": max_times})
            self.thread_1p.daemon = True
            self.thread_1p.start()

        if 2 in player and 1 in player:
            sleep(0.333)

        if 2 in player:
            self.thread_2p = ThreadWithException(
                target=self.faa_dict[2].use_items_double_card,
                name="2P Thread - UseItems",
                kwargs={"max_times": max_times})
            self.thread_2p.daemon = True
            self.thread_2p.start()

        if 1 in player:
            self.thread_1p.join()
        if 2 in player:
            self.thread_2p.join()

        self.model_end_print(text=title_text)

    def batch_loop_cross_server(self, player: list = None, deck: int = 1):

        # 默认值
        if player is None:
            player = [1, 2]
        # 如果只有一个角色
        if self.faa_dict[1].channel == self.faa_dict[2].channel:
            player = [1]

        title_text = "无限跨服刷威望"
        self.model_start_print(text=title_text)

        # 创建进程 -> 开始进程 -> 阻塞主进程
        if 1 in player:
            self.thread_1p = ThreadWithException(
                target=self.faa_dict[1].loop_cross_server,
                name="1P Thread",
                kwargs={"deck": deck})
            self.thread_1p.daemon = True
            self.thread_1p.start()

        if 2 in player and 1 in player:
            sleep(0.333)

        if 2 in player:
            self.thread_2p = ThreadWithException(
                target=self.faa_dict[2].loop_cross_server,
                name="2P Thread",
                kwargs={"deck": deck})
            self.thread_2p.daemon = True
            self.thread_2p.start()

        if 1 in player:
            self.thread_1p.join()
        if 2 in player:
            self.thread_2p.join()

    """业务代码 - 战斗相关"""

    def invite(self, player_a, player_b):
        """
        号1邀请号2到房间 需要在同一个区
        :return: bool 是否最终找到了图片
        """

        faa_a = self.faa_dict[player_a]
        faa_b = self.faa_dict[player_b]

        find = loop_match_p_in_w(
            source_handle=faa_a.handle,
            source_root_handle=faa_a.handle_360,
            source_range=[796, 413, 950, 485],
            template=RESOURCE_P["common"]["战斗"]["战斗前_开始按钮.png"],
            after_sleep=0.3,
            click=False,
            match_failed_check=2.0)
        if not find:
            CUS_LOGGER.warning("2s找不到开始游戏! 土豆服务器问题, 创建房间可能失败!")
            return False

        if not faa_a.stage_info["id"].split("-")[0] == "GD":

            # 点击[房间ui-邀请按钮]
            T_ACTION_QUEUE_TIMER.add_click_to_queue(handle=faa_a.handle, x=410, y=546)
            time.sleep(0.5)

            # 点击[房间ui-邀请ui-好友按钮]
            T_ACTION_QUEUE_TIMER.add_click_to_queue(handle=faa_a.handle, x=535, y=130)
            time.sleep(0.5)

            # 直接邀请
            T_ACTION_QUEUE_TIMER.add_click_to_queue(handle=faa_a.handle, x=601, y=157)
            time.sleep(0.5)

            # p2接受邀请
            find = loop_match_p_in_w(
                source_handle=faa_b.handle,
                source_root_handle=faa_a.handle_360,
                source_range=[0, 0, 950, 600],
                template=RESOURCE_P["common"]["战斗"]["战斗前_接受邀请.png"],
                after_sleep=2.0,
                match_failed_check=2.0
            )

            if not find:
                CUS_LOGGER.warning("2s没能组队? 土豆服务器问题, 尝试解决ing...")
                return False

            # p1关闭邀请窗口
            T_ACTION_QUEUE_TIMER.add_click_to_queue(handle=faa_a.handle, x=590, y=491)
            time.sleep(1)

        return True

    def goto_stage_and_invite(self, stage_id, mt_first_time, player_a, player_b):
        """
        :param stage_id:
        :param mt_first_time:
        :param player_a:
        :param player_b:
        :return:
        """

        # 自定义作战直接调出
        is_cu = "CU" in stage_id
        if is_cu:
            return 0

        is_cs = "CS" in stage_id
        is_mt = "MT" in stage_id

        faa_a = self.faa_dict[player_a]
        faa_b = self.faa_dict[player_b]

        failed_round = 0  # 计数失败轮次

        while True:

            failed_time = 0  # 计数失败次数

            while True:
                if not is_mt:
                    # 非魔塔进入
                    faa_a.action_goto_stage()
                    faa_b.action_goto_stage()
                else:
                    # 魔塔进入
                    faa_a.action_goto_stage(mt_first_time=mt_first_time)
                    if mt_first_time:
                        faa_b.action_goto_stage(mt_first_time=mt_first_time)

                sleep(3)

                if is_cs:
                    # 跨服副本 直接退出
                    return 0
                invite_success = self.invite(player_a=player_a, player_b=player_b)

                if invite_success:
                    SIGNAL.PRINT_TO_UI.emit(text="[单本轮战] 邀请成功")
                    # 邀请成功 返回退出
                    return 0

                else:
                    failed_time += 1
                    mt_first_time = True

                    SIGNAL.PRINT_TO_UI.emit(text=f"[单本轮战] 服务器抽风,进入竞技岛重新邀请...({failed_time}/3)")

                    if failed_time == 3:
                        SIGNAL.PRINT_TO_UI.emit(text="[单本轮战] 服务器抽风过头, 刷新游戏!")
                        failed_round += 1
                        self.batch_reload_game()
                        break

                    faa_a.action_exit(mode="竞技岛")
                    faa_b.action_exit(mode="竞技岛")

            if failed_round == 3:
                SIGNAL.PRINT_TO_UI.emit(text=f"[单本轮战] 刷新游戏次数过多")
                return 2

    def battle(self, player_a, player_b, change_card=True):
        """
        从进入房间到回到房间的流程
        :param player_a: 玩家A
        :param player_b: 玩家B
        :param change_card: 是否需要选择卡组
        :return:
            int id 用于判定战斗是 成功 或某种原因的失败 1-成功 2-服务器卡顿,需要重来 3-玩家设置的次数不足,跳过;
            dict 包含player_a和player_b的[战利品]和[宝箱]识别到的情况; 内容为聚合数量后的 dict。 如果识别异常, 返回值为两个None
            int 战斗消耗时间(秒);
        """

        is_group = self.faa_dict[player_a].is_group
        result_id = 0
        result_drop_by_list = {}  # {pid:{"loots":["item",...],"chest":["item",...]},...}
        result_drop_by_dict = {}  # {pid:{"loots":{"item":count,...},"chest":{"item":count,...}},...}
        result_spend_time = 0

        """检测是否成功进入房间"""
        if result_id == 0:

            # 创建并开始线程
            self.thread_1p = ThreadWithException(
                target=self.faa_dict[player_a].obj_battle_preparation.check_create_room_success,
                name="{}P Thread - 战前准备".format(player_a),
                kwargs={})
            self.thread_1p.daemon = True
            self.thread_1p.start()
            if is_group:
                self.thread_2p = ThreadWithException(
                    target=self.faa_dict[player_b].obj_battle_preparation.check_create_room_success,
                    name="{}P Thread - 战前准备".format(player_b),
                    kwargs={})
                self.thread_2p.daemon = True
                self.thread_2p.start()

            # 阻塞进程让进程执行完再继续本循环函数
            self.thread_1p.join()
            if is_group:
                self.thread_2p.join()

            # 获取返回值
            result_id = max(result_id, self.thread_1p.get_return_value())
            if is_group:
                result_id = max(result_id, self.thread_2p.get_return_value())

        """修改卡组"""
        if result_id == 0:

            if change_card:

                # 创建并开始线程
                self.thread_1p = ThreadWithException(
                    target=self.faa_dict[player_a].obj_battle_preparation.change_deck,
                    name="{}P Thread - 修改卡组".format(player_a),
                    kwargs={})
                self.thread_1p.daemon = True
                self.thread_1p.start()
                if is_group:
                    self.thread_2p = ThreadWithException(
                        target=self.faa_dict[player_b].obj_battle_preparation.change_deck,
                        name="{}P Thread - 修改卡组".format(player_b),
                        kwargs={})
                    self.thread_2p.daemon = True
                    self.thread_2p.start()

                # 阻塞进程让进程执行完再继续本循环函数
                self.thread_1p.join()
                if is_group:
                    self.thread_2p.join()

                # 获取返回值
                result_id = max(result_id, self.thread_1p.get_return_value())
                if is_group:
                    result_id = max(result_id, self.thread_2p.get_return_value())

        """不同时开始战斗, 并检测是否成功进入游戏"""
        if result_id == 0:

            # 创建并开始线程 注意 玩家B 是非房主 需要先开始
            if is_group:
                self.thread_2p = ThreadWithException(
                    target=self.faa_dict[player_b].obj_battle_preparation.start_and_ensure_entry,
                    name="{}P Thread - 进入游戏".format(player_b),
                    kwargs={})
                self.thread_2p.daemon = True
                self.thread_2p.start()
                time.sleep(2)
            self.thread_1p = ThreadWithException(
                target=self.faa_dict[player_a].obj_battle_preparation.start_and_ensure_entry,
                name="{}P Thread - 进入游戏".format(player_a),
                kwargs={})
            self.thread_1p.daemon = True
            self.thread_1p.start()

            # 阻塞进程让进程执行完再继续本循环函数
            self.thread_1p.join()
            if is_group:
                self.thread_2p.join()

            # 获取返回值
            result_id = max(result_id, self.thread_1p.get_return_value())
            if is_group:
                result_id = max(result_id, self.thread_2p.get_return_value())

        """多线程进行战斗 此处1p-ap 2p-bp 战斗部分没有返回值"""

        if result_id == 0:

            battle_start_time = time.time()

            # 初始化多线程
            self.thread_1p = ThreadWithException(
                target=self.faa_dict[player_a].battle_a_round_init_battle_plan,
                name="{}P Thread - Battle".format(player_a),
                kwargs={})
            self.thread_1p.daemon = True
            self.thread_1p.start()

            if is_group:
                self.thread_2p = ThreadWithException(
                    target=self.faa_dict[player_b].battle_a_round_init_battle_plan,
                    name="{}P Thread - Battle".format(player_b),
                    kwargs={})
                self.thread_2p.daemon = True
                self.thread_2p.start()

            # 阻塞进程让进程执行完再继续本循环函数
            self.thread_1p.join()
            if is_group:
                self.thread_2p.join()

            if self.opt["senior_settings"]["auto_senior_settings"]:

                self.process, queue_todo = read_and_get_return_information(
                    self.faa_dict[player_a],
                    self.opt["senior_settings"]["senior_log_state"],
                    self.opt["senior_settings"]["gpu_settings"],
                    self.opt["senior_settings"]["interval"]
                )

            else:
                queue_todo = None
                self.process = None

            # 初始化放卡管理器
            self.thread_card_manager = CardManager(
                todo=self,
                faa_a=self.faa_dict[player_a],
                faa_b=self.faa_dict[player_b],
                check_interval=self.battle_check_interval,
                solve_queue=queue_todo,
                senior_interval=self.opt["senior_settings"]["interval"]
            )

            self.thread_card_manager.start()
            self.exec()

            # 此处的重新变为None是为了让中止todo实例时时该属性仍存在
            self.thread_card_manager = None

            if self.opt["senior_settings"]["auto_senior_settings"]:
                kill_process(self.process)
                self.process = None

            CUS_LOGGER.debug('thread_card_manager 退出事件循环并完成销毁线程')

            result_spend_time = time.time() - battle_start_time

        CUS_LOGGER.debug("战斗循环 已完成")

        """多线程进行战利品和宝箱检查 此处1p-ap 2p-bp"""

        if result_id == 0:

            # 初始化多线程
            self.thread_1p = ThreadWithException(
                target=self.faa_dict[player_a].battle_a_round_loots,
                name="{}P Thread - Battle - Screen".format(player_a),
                kwargs={})
            if is_group:
                self.thread_2p = ThreadWithException(
                    target=self.faa_dict[player_b].battle_a_round_loots,
                    name="{}P Thread - Battle - Screen".format(player_b),
                    kwargs={})

            # 开始多线程
            self.thread_1p.daemon = True
            if is_group:
                self.thread_2p.daemon = True
            self.thread_1p.start()
            if is_group:
                self.thread_2p.start()

            # 阻塞进程让进程执行完再继续本循环函数
            self.thread_1p.join()
            if is_group:
                self.thread_2p.join()

            result = self.thread_1p.get_return_value()
            result_id = max(result_id, result[0])
            if result[1]:
                result_drop_by_list[player_a] = result[1]  # 可能是None 或 dict 故判空

            if is_group:
                result = self.thread_2p.get_return_value()
                result_id = max(result_id, result[0])
                if result[1]:
                    result_drop_by_list[player_b] = result[1]  # 可能是None 或 dict 故判空

            """数据基础校验, 构建有向无环图, 完成高级校验, 并酌情发送至服务器和更新至Ranking"""
            # result_drop_by_list = {
            #   1:{"loots":["物品"...],"chests":["物品"...]} //数据 可能不存在 None
            #   2:{"loots":["物品"...],"chests":["物品"...]} //数据 可能不存在 None或不组队
            #   }
            update_dag_success = False

            for player_index, player_data in result_drop_by_list.items():

                title = f"[{player_index}P] [战利品识别]"
                # 两种战利品的数据
                loots_list = player_data["loots"]  # list[str,...]
                chests_list = player_data["chests"]  # list[str,...]
                # 默认表示识别异常
                result_drop_by_dict[player_index] = {"loots": None, "chests": None}

                def check_data_validity(data):
                    """确定同一个物品名总是相邻出现"""
                    # 创建一个字典来记录每个值最后出现的位置
                    last_seen = {}

                    for index, value in enumerate(data):
                        if value in last_seen:
                            # 如果当前值之前已经出现过，检查中间是否有其他值
                            if index - last_seen[value] > 1:
                                return False
                        # 更新当前值的最后出现位置
                        last_seen[value] = index

                    return True

                # 确定同一个物品名总是相邻出现
                if not check_data_validity(loots_list):
                    text = f"{title} [基础校验] 失败! 同一个物品在连续出现若干次后, 再次出现! 为截图有误!"
                    CUS_LOGGER.warning(text)
                    continue

                # 所有物品都是 识别失败
                if all(item == "识别失败" for item in loots_list):
                    text = f"{title} [基础校验] 失败! 识别失败过多! 为截图有误!"
                    CUS_LOGGER.warning(text)
                    continue

                def drop_list_to_dict(drop_list):
                    drop_dict = defaultdict(int)
                    for item in drop_list:
                        drop_dict[item] += 1
                    return dict(drop_dict)

                loots_dict = drop_list_to_dict(loots_list)
                chests_dict = drop_list_to_dict(chests_list)

                # 仅使用战利品更新item_dag_graph文件 且不包含 识别失败
                best_match_items_success = [
                    item for item in copy.deepcopy(list(loots_dict.keys())) if item != "识别失败"]

                # 更新 item_dag_graph 文件
                update_dag_result = update_dag_graph(item_list_new=best_match_items_success)
                # 更新成功, 记录两个号中是否有至少一个号更新成功
                update_dag_success = update_dag_result or update_dag_success

                if not update_dag_result:
                    text = f"{title} [有向无环图] [更新] 失败! 本次数据无法构筑 DAG，存在环. 可能是截图卡住了. 放弃记录和上传"
                    CUS_LOGGER.warning(text)
                    continue

                """至此所有校验通过!"""

                CUS_LOGGER.info(f"{title} [有向无环图] [更新] 成功! 成功构筑 DAG.")

                result_drop_by_dict[player_index] = {"loots": loots_dict, "chests": chests_dict}

                # 保存详细数据到json
                loots_and_chests_statistics_to_json(
                    faa=self.faa_dict[player_index],
                    loots_dict=loots_dict,
                    chests_dict=chests_dict)
                CUS_LOGGER.info(f"{title} [保存日志] 成功保存一条详细数据!")

                # 保存汇总统计数据到json
                detail_data = loots_and_chests_detail_to_json(
                    faa=self.faa_dict[player_index],
                    loots_dict=loots_dict,
                    chests_dict=chests_dict)
                CUS_LOGGER.info(f"{title} [保存日志] 成功保存至统计数据!")

                # 发送到服务器
                upload_result = loots_and_chests_data_post_to_sever(
                    detail_data=detail_data,
                    url=EXTRA.MISU_LOGISTICS)
                if upload_result:
                    CUS_LOGGER.info(f"{title} [发送服务器] 成功发送一条数据到米苏物流!")
                else:
                    CUS_LOGGER.warning(f"{title} [发送服务器] 超时! 可能是米苏物流服务器炸了...")

            if not update_dag_success:
                text = f"[战利品识别] [有向无环图] item_ranking_dag_graph.json 更新失败! 本次战斗未获得任何有效数据!"
                CUS_LOGGER.warning(text)

            # 如果成功更新了 item_dag_graph.json, 更新ranking
            ranking_new = find_longest_path_from_dag()  # 成功返回更新后的 ranking 失败返回None
            if ranking_new:
                text = f"[战利品识别] [有向无环图] item_ranking_dag_graph.json 已更新 , 结果:{ranking_new}"
                CUS_LOGGER.info(text)
            else:
                text = f"[战利品识别] [有向无环图] item_ranking_dag_graph.json 更新失败, 文件被删除 或 因程序错误成环! 请联系开发者!"
                CUS_LOGGER.error(text)

        CUS_LOGGER.debug("多线程进行战利品和宝箱检查 已完成")

        """分开进行战后检查"""
        if result_id == 0:
            result_id = self.faa_dict[player_a].battle_a_round_warp_up()
            if is_group:
                result_id = self.faa_dict[player_b].battle_a_round_warp_up()

        CUS_LOGGER.debug("战后检查完成 battle 函数执行结束")

        return result_id, result_drop_by_dict, result_spend_time

    def n_battle_customize_battle_error_print(self, success_battle_time):
        # 结束提示文本
        SIGNAL.PRINT_TO_UI.emit(
            text=f"[单本轮战] 第{success_battle_time}次, 出现未知异常! 刷新后卡死, 以防止更多问题, 出现此问题可上报作者")
        self.batch_reload_game()
        sleep(60 * 60 * 24)

    def battle_1_1_n(self, stage_id, player, need_key, max_times, dict_exit,
                     global_plan_active, deck, battle_plan_1p, battle_plan_2p,
                     quest_card, ban_card_list, max_card_num,
                     title_text, need_lock=False):
        """
        1轮次 1关卡 n次数
        副本外 -> (副本内战斗 * n次) -> 副本外
        player: [1], [2], [1,2], [2,1] 分别代表 1P单人 2P单人 1P队长 2P队长
        """

        # 组合完整的title
        title = f"[单本轮战] {title_text}"

        # 判断是不是打魔塔 或 自建房
        is_mt = "MT" in stage_id
        is_cu = "CU" in stage_id
        is_cs = "CS" in stage_id

        # 判断是不是组队
        is_group = len(player) > 1

        # 如果是多人跨服 防呆重写 2,1 为 1,2
        if is_cs and is_group:
            player = [1, 2]

        def get_stage_plan_by_id():
            """
            获取stage_plan
            """

            try:
                with EXTRA.FILE_LOCK:
                    with open(file=PATHS["config"] + "//stage_plan.json", mode="r", encoding="UTF-8") as file:
                        stage_plan = json.load(file)
            except FileNotFoundError:
                stage_plan = {}

            plan = stage_plan.get(stage_id, None)
            if not plan:
                plan = {
                    "skip": False,
                    "deck": 0,
                    "battle_plan": [
                        "00000000-0000-0000-0000-000000000000",
                        "00000000-0000-0000-0000-000000000001"]}
            return plan

        # 处理多人信息 (这些信息只影响函数内, 所以不判断是否组队)
        pid_a = player[0]  # 房主 创建房间者
        pid_b = 1 if pid_a == 2 else 2  # 非房主

        # 默认肯定是不跳过的
        skip = False

        # 是否采用 全局方案配置
        if global_plan_active:
            stage_plan_by_id = get_stage_plan_by_id()
            skip = stage_plan_by_id["skip"]
            deck = stage_plan_by_id["deck"]
            battle_plan_1p = stage_plan_by_id["battle_plan"][0]
            battle_plan_2p = stage_plan_by_id["battle_plan"][1]

        faa_a, faa_b = self.faa_dict[pid_a], self.faa_dict[pid_b]
        battle_plan_a = battle_plan_1p if pid_a == 1 else battle_plan_2p
        battle_plan_b = battle_plan_1p if pid_b == 1 else battle_plan_2p

        def check_skip():
            """
            检查人物等级和次数是否充足
            """
            if skip:
                SIGNAL.PRINT_TO_UI.emit(text=f"{title} 根据全局关卡设置, 跳过")
                return False

            if not faa_a.check_level():
                SIGNAL.PRINT_TO_UI.emit(text=f"{title} [{pid_a}P] 等级不足, 跳过")
                return False

            if is_group:
                if not faa_b.check_level():
                    SIGNAL.PRINT_TO_UI.emit(text=f"{title} [{pid_b}P] 等级不足, 跳过")
                    return False

            if max_times < 1:
                SIGNAL.PRINT_TO_UI.emit(text=f"{title} {stage_id} 设置次数不足, 跳过")
                return False

            return True

        def multi_round_battle():
            # 标记是否需要进入副本
            need_goto_stage = not is_cu
            need_change_card = True

            battle_count = 0  # 记录成功的次数
            result_list = []  # 记录成功场次的战斗结果记录

            # 轮次作战
            while battle_count < max_times:

                # 初始
                result_id = 0

                # 前往副本
                if not is_mt:
                    # 非魔塔
                    if need_goto_stage:
                        if not is_group:
                            # 单人前往副本
                            faa_a.action_goto_stage()
                        else:
                            # 多人前往副本
                            result_id = self.goto_stage_and_invite(
                                stage_id=stage_id,
                                mt_first_time=False,
                                player_a=pid_a,
                                player_b=pid_b)

                        need_goto_stage = False  # 进入后Flag变化
                else:
                    # 魔塔
                    if not is_group:
                        # 单人前往副本
                        faa_a.action_goto_stage(
                            mt_first_time=need_goto_stage)  # 第一次使用 mt_first_time, 之后则不用
                    else:
                        # 多人前往副本
                        result_id = self.goto_stage_and_invite(
                            stage_id=stage_id,
                            mt_first_time=need_goto_stage,
                            player_a=pid_a,
                            player_b=pid_b)

                    need_goto_stage = False  # 进入后Flag变化

                if result_id == 2:
                    # 跳过本次 计数+1
                    battle_count += 1
                    # 进入异常, 跳过
                    need_goto_stage = True
                    # 结束提示文本
                    SIGNAL.PRINT_TO_UI.emit(text=f"{title}第{battle_count}次, 创建房间多次异常, 重启跳过")

                    self.batch_reload_game()

                SIGNAL.PRINT_TO_UI.emit(text=f"{title}第{battle_count + 1}次, 开始")

                # 开始战斗循环
                result_id, result_drop, result_spend_time = self.battle(
                    player_a=pid_a, player_b=pid_b, change_card=need_change_card)

                if result_id == 0:

                    # 战斗成功 计数+1
                    battle_count += 1

                    if battle_count < max_times:
                        # 常规退出方式
                        for j in dict_exit["other_time_player_a"]:
                            faa_a.action_exit(mode=j)
                        if is_group:
                            for j in dict_exit["other_time_player_b"]:
                                faa_b.action_exit(mode=j)
                    else:
                        # 最后一次退出方式
                        for j in dict_exit["last_time_player_a"]:
                            faa_a.action_exit(mode=j)
                        if is_group:
                            for j in dict_exit["last_time_player_b"]:
                                faa_b.action_exit(mode=j)

                    # 获取是否使用了钥匙 仅查看房主(任意一个号用了钥匙都会更改为两个号都用了)
                    is_used_key = faa_a.faa_battle.is_used_key

                    # 加入结果统计列表
                    result_list.append({
                        "time_spend": result_spend_time,
                        "is_used_key": is_used_key,
                        "loot_dict_list": result_drop  # result_loot_dict_list = [{a掉落}, {b掉落}]
                    })

                    # 时间
                    SIGNAL.PRINT_TO_UI.emit(
                        text="{}第{}次, {}, 正常结束, 耗时:{}分{}秒".format(
                            title,
                            battle_count,
                            "使用钥匙" if is_used_key else "未使用钥匙",
                            *divmod(int(result_spend_time), 60)
                        )
                    )

                    # 如果使用钥匙情况和要求不符, 加入美食大赛黑名单
                    if is_used_key == need_key:
                        CUS_LOGGER.debug(
                            f"{title}钥匙使用要求和实际情况一致~ 要求: {need_key}, 实际: {is_used_key}")
                    else:
                        CUS_LOGGER.debug(
                            f"{title}钥匙使用要求和实际情况不同! 要求: {need_key}, 实际: {is_used_key}")
                        self.auto_food_stage_ban_list.append(
                            {
                                "stage_id": stage_id,
                                "player": player,  # 1 单人 2 组队
                                "need_key": need_key,  # 注意类型转化
                                "max_times": max_times,
                                "quest_card": quest_card,
                                "ban_card_list": ban_card_list,
                                "max_card_num": max_card_num,
                                "dict_exit": dict_exit
                            }
                        )

                    # 成功的战斗 之后不需要选择卡组
                    need_change_card = False

                if result_id == 1:

                    # 重试本次

                    if is_cu:
                        # 进入异常 但是自定义
                        self.n_battle_customize_battle_error_print(success_battle_time=battle_count)

                    else:
                        # 进入异常, 重启再来
                        need_goto_stage = True

                        # 结束提示文本
                        SIGNAL.PRINT_TO_UI.emit(text=f"{title}第{battle_count + 1}次, 异常结束, 重启再来")

                        if not need_lock:
                            # 非单人多线程
                            self.batch_reload_game()
                        else:
                            # 单人多线程 只reload自己
                            faa_a.reload_game()

                    # 重新进入 因此需要重新选卡组
                    need_change_card = True

                if result_id == 2:

                    # 跳过本次

                    if is_cu:
                        # 进入异常 但是自定义
                        self.n_battle_customize_battle_error_print(success_battle_time=battle_count)
                    else:
                        # 跳过本次 计数+1
                        battle_count += 1

                        # 进入异常, 跳过
                        need_goto_stage = True

                        # 结束提示文本
                        SIGNAL.PRINT_TO_UI.emit(text=f"{title}第{battle_count}次, 开始游戏异常, 重启跳过")

                        if not need_lock:
                            # 非单人多线程
                            self.batch_reload_game()
                        else:
                            # 单人多线程 只reload自己
                            faa_a.reload_game()

                    # 重新进入 因此需要重新选卡组
                    need_change_card = True

                if result_id == 3:

                    # 放弃所有次数

                    # 自动选卡 但没有对应的卡片! 最严重的报错!
                    SIGNAL.PRINT_TO_UI.emit(
                        text=f"{title} 自动选卡失败! 放弃本关全部作战! 您是否拥有对应绑定卡?")

                    if not need_lock:
                        # 非单人多线程
                        self.batch_reload_game()
                    else:
                        # 单人多线程 只reload自己
                        faa_a.reload_game()

                    break

            return result_list

        def end_statistic_print(result_list):
            """
            结束后进行 本次 多本轮战的 战利品 统计和输出, 由于其统计为本次多本轮战, 故不能改变其位置
            """

            CUS_LOGGER.debug("result_list:")
            CUS_LOGGER.debug(str(result_list))

            valid_total_count = len(result_list)

            # 如果没有正常完成的场次, 直接跳过统计输出的部分
            if valid_total_count == 0:
                return

            # 时间
            sum_time_spend = 0
            count_used_key = 0
            for result in result_list:
                # 合计时间
                sum_time_spend += result["time_spend"]
                # 合计消耗钥匙的次数
                if result["is_used_key"]:
                    count_used_key += 1
            average_time_spend = sum_time_spend / valid_total_count

            SIGNAL.PRINT_TO_UI.emit(
                text="正常场次:{}次 使用钥匙:{}次 总耗时:{}分{}秒  场均耗时:{}分{}秒".format(
                    valid_total_count,
                    count_used_key,
                    *divmod(int(sum_time_spend), 60),
                    *divmod(int(average_time_spend), 60)
                ))

            if len(player) == 1:
                # 单人
                self.output_player_loot(player_id=pid_a, result_list=result_list)
            else:
                # 多人
                self.output_player_loot(player_id=1, result_list=result_list)
                self.output_player_loot(player_id=2, result_list=result_list)

        def main():
            SIGNAL.PRINT_TO_UI.emit(text=f"{title}{stage_id} {max_times}次 开始", color_level=5)

            opt_ad = self.opt["advanced_settings"]
            c_a_c_c_deck = opt_ad["cus_auto_carry_card_value"] if opt_ad["cus_auto_carry_card_active"] else 6

            # 填入战斗方案和关卡信息, 之后会大量动作和更改类属性, 所以需要判断是否组队
            faa_a.set_config_for_battle(
                is_main=True,
                is_group=is_group,
                need_key=need_key,
                deck=c_a_c_c_deck if deck == 0 else deck,
                auto_carry_card=deck == 0,
                quest_card=quest_card,
                ban_card_list=ban_card_list,
                max_card_num=max_card_num,
                battle_plan_uuid=battle_plan_a,
                stage_id=stage_id)
            if is_group:
                faa_b.set_config_for_battle(
                    is_main=False,
                    is_group=is_group,
                    need_key=need_key,
                    deck=c_a_c_c_deck if deck == 0 else deck,
                    auto_carry_card=deck == 0,
                    quest_card=quest_card,
                    ban_card_list=ban_card_list,
                    max_card_num=max_card_num,
                    battle_plan_uuid=battle_plan_b,
                    stage_id=stage_id)

            if not check_skip():
                return False

            # 进行 1本n次 返回 成功的每次战斗结果组成的list
            result_list = multi_round_battle()

            # 根据多次战斗结果组成的list 打印 1本n次 的汇总结果
            end_statistic_print(result_list=result_list)

            SIGNAL.PRINT_TO_UI.emit(text=f"{title}{stage_id} {max_times}次 结束 ", color_level=5)

        main()

    def output_player_loot(self, player_id, result_list):
        """
        根据战斗的最终结果, 打印玩家战利品信息
        :param player_id:  player_a, player_b int 1 2
        :param result_list: [{一场战斗的信息}, ...]
        :return:
        关于 result_list
        """

        # 输入为
        count_dict = {"loots": {}, "chests": {}}  # 汇总每个物品的总掉落
        count_match_success_dict = {"loots": [], "chests": []}

        # 计数正确场次

        # 复制key
        for _, a_battle_data in enumerate(result_list):
            for drop_type in ["loots", "chests"]:

                data = a_battle_data["loot_dict_list"][player_id][drop_type]
                # 如果标记为识别失败
                if data is None:
                    count_match_success_dict[drop_type].append(False)
                    continue
                count_match_success_dict[drop_type].append(True)

                for key, value in data.items():
                    if key in count_dict[drop_type].keys():
                        count_dict[drop_type][key] += value
                    else:
                        count_dict[drop_type][key] = value

        # 生成图片
        text = "[{}P] 战利品合计掉落, 识别有效场次:{}".format(player_id, sum(count_match_success_dict["loots"]))
        SIGNAL.PRINT_TO_UI.emit(text=text, time=False)
        SIGNAL.IMAGE_TO_UI.emit(image=create_drops_image(count_dict=count_dict["loots"]))

        text = "[{}P] 宝箱合计掉落, 识别有效场次:{}".format(player_id, sum(count_match_success_dict["chests"]))
        SIGNAL.PRINT_TO_UI.emit(text=text, time=False)
        SIGNAL.IMAGE_TO_UI.emit(image=create_drops_image(count_dict=count_dict["chests"]))

    def battle_1_n_n(self, quest_list, extra_title=None, need_lock=False):
        """
        1轮次 n关卡 n次数
        (副本外 -> (副本内战斗 * n次) -> 副本外) * 重复n次
        :param quest_list: 任务清单
        :param extra_title: 输出中的额外文本 会自动加上 [ ]
        :param need_lock:  用于多线程单人作战时设定为True 以进行上锁解锁
        """
        # 输出文本的title
        extra_title = f"[{extra_title}] " if extra_title else ""
        title = f"[多本轮战] {extra_title}"

        if need_lock:
            # 上锁
            SIGNAL.PRINT_TO_UI.emit(text=f"[双线程单人] {self.todo_id}P已开始任务! 进行自锁!", color_level=3)
            self.my_lock = True

        SIGNAL.PRINT_TO_UI.emit(text=f"{title}开始...", color_level=3)

        # 遍历完成每一个任务
        for i in range(len(quest_list)):

            quest = quest_list[i]

            # 判断显著错误的关卡名称
            if quest["stage_id"].split("-")[0] not in ["NO", "EX", "MT", "CS", "OR", "PT", "CU", "GD", "HH"]:
                SIGNAL.PRINT_TO_UI.emit(
                    text="{}事项{},{},错误的关卡名称!跳过".format(
                        title,
                        quest["battle_id"] if "battle_id" in quest else (i + 1),
                        quest["stage_id"]),
                    color_level=1
                )
                continue

            else:
                # 处理允许缺失的值
                quest_card = quest.get("quest_card", None)
                ban_card_list = quest.get("ban_card_list", None)
                max_card_num = quest.get("max_card_num", None)

                text_parts = [
                    "{}事项{}".format(
                        title,
                        quest["battle_id"] if "battle_id" in quest else (i + 1)),
                    "开始",
                    "组队" if len(quest["player"]) == 2 else "单人",
                    f"{quest["stage_id"]}",
                    f"{quest["max_times"]}次",
                ]
                if quest_card:
                    text_parts.append("带卡:{}".format(quest_card))
                if ban_card_list:
                    text_parts.append("禁卡:{}".format(ban_card_list))
                if max_card_num:
                    text_parts.append("限数:{}".format(max_card_num))

                SIGNAL.PRINT_TO_UI.emit(text=",".join(text_parts), color_level=4)

                self.battle_1_1_n(
                    stage_id=quest["stage_id"],
                    player=quest["player"],
                    need_key=quest["need_key"],
                    max_times=quest["max_times"],
                    dict_exit=quest["dict_exit"],
                    global_plan_active=quest["global_plan_active"],
                    deck=quest["deck"],
                    battle_plan_1p=quest["battle_plan_1p"],
                    battle_plan_2p=quest["battle_plan_2p"],
                    quest_card=quest_card,
                    ban_card_list=ban_card_list,
                    max_card_num=max_card_num,
                    title_text=extra_title,
                    need_lock=need_lock
                )

                SIGNAL.PRINT_TO_UI.emit(
                    text="{}事项{}, 结束".format(
                        title,
                        quest["battle_id"] if "battle_id" in quest else (i + 1)
                    ),
                    color_level=4
                )

        SIGNAL.PRINT_TO_UI.emit(text=f"{title}结束", color_level=3)

        if need_lock:
            SIGNAL.PRINT_TO_UI.emit(
                text=f"双线程单人功能中, {self.todo_id}P已完成所有任务! 已解锁另一线程!",
                color_level=3
            )

            # 为另一个todo解锁
            self.signal_todo_lock.emit(False)
            # 如果自身是主线程, 且未被解锁, 循环等待
            if self.todo_id == 1:
                while self.my_lock:
                    sleep(1)

    """使用n_n_battle为核心的变种 [单线程][单人或双人]"""

    def easy_battle(self, text_, stage_id, player, max_times,
                    global_plan_active, deck, battle_plan_1p, battle_plan_2p, dict_exit):
        """仅调用 n_battle的简易作战"""
        self.model_start_print(text=text_)

        quest_list = [
            {
                "stage_id": stage_id,
                "max_times": max_times,
                "need_key": True,
                "player": player,
                "global_plan_active": global_plan_active,
                "deck": deck,
                "battle_plan_1p": battle_plan_1p,
                "battle_plan_2p": battle_plan_2p,
                "dict_exit": dict_exit
            }]
        self.battle_1_n_n(quest_list=quest_list)

        self.model_end_print(text=text_)

    def offer_reward(self, text_, max_times_1, max_times_2, max_times_3,
                     global_plan_active, deck, battle_plan_1p, battle_plan_2p):

        self.model_start_print(text=text_)

        SIGNAL.PRINT_TO_UI.emit(text=f"[{text_}] 开始[多本轮战]...")

        quest_list = []
        for i in range(3):
            quest_list.append({
                "player": [2, 1],
                "need_key": True,
                "global_plan_active": global_plan_active,
                "deck": deck,
                "battle_plan_1p": battle_plan_1p,
                "battle_plan_2p": battle_plan_2p,
                "stage_id": "OR-0-" + str(i + 1),
                "max_times": [max_times_1, max_times_2, max_times_3][i],
                "dict_exit": {
                    "other_time_player_a": [],
                    "other_time_player_b": [],
                    "last_time_player_a": ["竞技岛"],
                    "last_time_player_b": ["竞技岛"]}
            })

        self.battle_1_n_n(quest_list=quest_list)

        # 领取奖励
        self.faa_dict[1].receive_quest_rewards(mode="悬赏任务")
        self.faa_dict[2].receive_quest_rewards(mode="悬赏任务")

        self.model_end_print(text=text_)

    def guild_or_spouse_quest(self, title_text, quest_mode,
                              global_plan_active, deck, battle_plan_1p, battle_plan_2p, stage=False):
        """完成公会or情侣任务"""

        self.model_start_print(text=title_text)

        # 激活删除物品高危功能(可选) + 领取奖励一次
        if quest_mode == "公会任务":
            self.batch_level_2_action(title_text=title_text, dark_crystal=False)
        SIGNAL.PRINT_TO_UI.emit(text=f"[{title_text}] 检查领取奖励...")
        self.faa_dict[1].receive_quest_rewards(mode=quest_mode)
        self.faa_dict[2].receive_quest_rewards(mode=quest_mode)

        # 获取任务
        SIGNAL.PRINT_TO_UI.emit(text=f"[{title_text}] 获取任务列表...")
        quest_list = self.faa_dict[1].match_quests(mode=quest_mode, qg_cs=stage)

        for i in quest_list:
            text_parts = [f"副本:{i["stage_id"]}"]
            quest_card = i.get("quest_card", None)
            ban_card_list = i.get("ban_card_list", None)
            max_card_num = i.get("max_card_num", None)
            if quest_card:
                text_parts.append("带卡:{}".format(quest_card))
            if ban_card_list:
                text_parts.append("禁卡:{}".format(ban_card_list))
            if max_card_num:
                text_parts.append("限数:{}".format(max_card_num))
            text = ",".join(text_parts)
            SIGNAL.PRINT_TO_UI.emit(text=text)

        for i in range(len(quest_list)):
            quest_list[i]["global_plan_active"] = global_plan_active
            quest_list[i]["deck"] = deck
            quest_list[i]["battle_plan_1p"] = battle_plan_1p
            quest_list[i]["battle_plan_2p"] = battle_plan_2p

        # 完成任务
        self.battle_1_n_n(quest_list=quest_list)

        # 激活删除物品高危功能(可选) + 领取奖励一次 + 领取普通任务奖励(公会点)一次
        quests = [quest_mode]
        if quest_mode == "公会任务":
            quests.append("普通任务")
            self.batch_level_2_action(title_text=title_text, dark_crystal=False)

        SIGNAL.PRINT_TO_UI.emit(text=f"[{title_text}] 检查领取奖励中...")
        self.batch_receive_all_quest_rewards(player=[1, 2], quests=quests)

        self.model_end_print(text=title_text)

    def guild_dungeon(self, text_, global_plan_active, deck, battle_plan_1p, battle_plan_2p):

        self.model_start_print(text=text_)

        SIGNAL.PRINT_TO_UI.emit(text=f"[{text_}] 开始[多本轮战]...")

        quest_list = []

        for i in range(3):
            quest_list.append({
                "deck": deck,
                "player": [2, 1],
                "need_key": True,
                "global_plan_active": global_plan_active,
                "battle_plan_1p": battle_plan_1p,
                "battle_plan_2p": battle_plan_2p,
                "stage_id": "GD-0-" + str(i + 1),
                "max_times": 3,
                "dict_exit": {
                    "other_time_player_a": [],
                    "other_time_player_b": [],
                    "last_time_player_a": ["竞技岛"],
                    "last_time_player_b": ["竞技岛"]}
            })

        self.battle_1_n_n(quest_list=quest_list)

        self.model_end_print(text=text_)

    """高级模式"""

    def task_sequence(self, text_, task_begin_id: int, task_sequence_index: int):
        """
        自定义任务序列
        :param text_: 其实是title
        :param task_begin_id: 从第几号任务开始
        :param task_sequence_index: 任务序列的索引号(暂未使用uuid)
        :return:
        """

        def split_task_sequence(task_sequence: list):
            """将任务序列列表拆分为单独的任务列表"""
            task_list = []
            battle_dict = {
                "task_type": "战斗",
                "task_args": []
            }
            for task in task_sequence:
                if task["task_type"] == "战斗":
                    task["task_args"]["battle_id"] = task["task_id"]
                    battle_dict["task_args"].append(task["task_args"])
                else:
                    # 出现非战斗事项
                    if battle_dict["task_args"]:
                        task_list.append(battle_dict)
                        battle_dict = {
                            "task_type": "战斗",
                            "task_args": []
                        }
                    task_list.append(task)
            # 保存末位战斗
            if battle_dict["task_args"]:
                task_list.append(battle_dict)

            return task_list

        def read_json_to_task_sequence():
            task_sequence_list = get_task_sequence_list(with_extension=True)
            task_sequence_path = "{}\\{}".format(
                PATHS["task_sequence"],
                task_sequence_list[task_sequence_index]
            )

            with EXTRA.FILE_LOCK:
                with open(file=task_sequence_path, mode="r", encoding="UTF-8") as file:
                    data = json.load(file)

            return data

        self.model_start_print(text=text_)

        # 读取json文件
        task_sequence = read_json_to_task_sequence()

        # 获取最大task_id
        max_tid = 1
        for quest in task_sequence:
            max_tid = max(max_tid, quest["task_id"])

        if task_begin_id > max_tid:
            SIGNAL.PRINT_TO_UI.emit(text=f"[{text_}] 开始事项id > 该方案最高id! 将直接跳过!")
            return

        # 由于任务id从1开始, 故需要减1
        # 去除序号小于stage_begin的任务
        task_sequence = [task for task in task_sequence if task["task_id"] >= task_begin_id]

        # 根据战斗和其他事项拆分 让战斗事项的参数构成 n本 n次 为一组的汇总
        task_sequence = split_task_sequence(task_sequence=task_sequence)

        for task in task_sequence:

            match task["task_type"]:

                case "战斗":
                    self.battle_1_n_n(
                        quest_list=task["task_args"],
                        extra_title=text_
                    )
                case "双暴卡":
                    self.batch_use_items_double_card(
                        player=task["task_args"]["player"],
                        max_times=task["task_args"]["max_times"]
                    )

                case "刷新游戏":
                    self.batch_reload_game(
                        player=task["task_args"]["player"],
                    )

                case "清背包":
                    self.batch_level_2_action(
                        title_text=text_,
                        player=task["task_args"]["player"],
                        dark_crystal=False
                    )

                case "领取任务奖励":
                    all_quests = {
                        "normal": "普通任务",
                        "guild": "公会任务",
                        "spouse": "情侣任务",
                        "offer_reward": "悬赏任务",
                        "food_competition": "美食大赛",
                        "monopoly": "大富翁",
                        "camp": "营地任务"
                    }
                    self.batch_receive_all_quest_rewards(
                        player=task["task_args"]["player"],
                        quests=[v for k, v in all_quests.items() if task["task_args"][k]]
                    )

        # 战斗结束
        self.model_end_print(text=text_)

    def auto_food(self, deck):

        def a_round():
            """
            一轮美食大赛战斗
            :return: 是否还有任务在美食大赛中
            """

            # 两个号分别读取任务
            quest_list_1 = self.faa_dict[1].match_quests(mode="美食大赛-新")
            quest_list_2 = self.faa_dict[2].match_quests(mode="美食大赛-新")
            quest_list = quest_list_1 + quest_list_2

            if not quest_list:
                return False

            # 去重
            unique_data = []
            for quest in quest_list:
                if quest not in unique_data:
                    unique_data.append(quest)
            quest_list = unique_data

            CUS_LOGGER.debug("[全自动大赛] 去重后任务列表如下:")
            CUS_LOGGER.debug(quest_list)

            # 去被ban的任务 一般是由于 需要使用钥匙但没有使用钥匙 或 没有某些关卡的次数 但尝试进入
            for quest in quest_list:
                if quest in self.auto_food_stage_ban_list:
                    CUS_LOGGER.debug(f"[全自动大赛] 该任务已经被ban, 故移出任务列表: {quest}")
                    quest_list.remove(quest)

            CUS_LOGGER.debug("[全自动大赛] 去Ban后任务列表如下:")
            CUS_LOGGER.debug(quest_list)

            SIGNAL.PRINT_TO_UI.emit(
                text="[全自动大赛] 已完成任务获取, 结果如下:",
                color_level=3
            )

            for i in range(len(quest_list)):

                if len(quest_list[i]["player"]) == 2:
                    player_text = "组队"
                else:
                    player_text = "单人1P" if quest_list[i]["player"] == [1] else "单人2P"

                quest_card = quest_list[i].get("quest_card", None)
                ban_card_list = quest_list[i].get("ban_card_list", None)
                max_card_num = quest_list[i].get("max_card_num", None)

                text_parts = [
                    f"[全自动大赛] 事项{i + 1}",
                    f"{player_text}",
                    f"{quest_list[i]["stage_id"]}",
                    "用钥匙" if quest_list[i]["stage_id"] else "无钥匙",
                    f"{quest_list[i]["max_times"]}次",
                ]

                if quest_card:
                    text_parts.append("带卡:{}".format(quest_card))
                if ban_card_list:
                    text_parts.append("禁卡:{}".format(ban_card_list))
                if max_card_num:
                    text_parts.append("限数:{}".format(max_card_num))
                text = ",".join(text_parts)

                SIGNAL.PRINT_TO_UI.emit(text=text, color_level=3)

            for i in range(len(quest_list)):
                quest_list[i]["global_plan_active"] = False
                quest_list[i]["deck"] = deck
                quest_list[i]["battle_plan_1p"] = "00000000-0000-0000-0000-000000000000"
                quest_list[i]["battle_plan_2p"] = "00000000-0000-0000-0000-000000000001"

            self.battle_1_n_n(quest_list=quest_list)

            return True

        def auto_food_main():
            text_ = "全自动大赛"
            self.model_start_print(text=text_)

            # 先领一下已经完成的大赛任务
            self.faa_dict[1].receive_quest_rewards(mode="美食大赛")
            self.faa_dict[2].receive_quest_rewards(mode="美食大赛")

            # 重置美食大赛任务 ban list
            self.auto_food_stage_ban_list = []  # 用于防止缺乏钥匙/次数时无限重复某些关卡

            i = 0
            while True:
                i += 1
                SIGNAL.PRINT_TO_UI.emit(text=f"[{text_}] 第{i}次循环，开始", color_level=2)

                round_result = a_round()

                SIGNAL.PRINT_TO_UI.emit(text=f"[{text_}] 第{i}次循环，结束", color_level=2)
                if not round_result:
                    break

            SIGNAL.PRINT_TO_UI.emit(text=f"[{text_}] 所有被记录的任务已完成!", color_level=2)

            self.model_end_print(text=text_)

        auto_food_main()

    """使用n_n_battle为核心的变种 [双线程][单人]"""

    def alone_magic_tower(self):

        c_opt = self.opt_todo_plans

        def one_player():
            for player in [1, 2]:
                my_opt = c_opt[f"magic_tower_alone_{player}"]
                if my_opt["active"]:
                    self.easy_battle(
                        text_="[魔塔单人] [多线程{}P]".format(player),
                        stage_id="MT-1-" + str(my_opt["stage"]),
                        player=[player],
                        max_times=int(my_opt["max_times"]),
                        global_plan_active=my_opt["global_plan_active"],
                        deck=my_opt["deck"],
                        battle_plan_1p=my_opt["battle_plan_1p"],
                        battle_plan_2p=my_opt["battle_plan_1p"],
                        dict_exit={
                            "other_time_player_a": [],
                            "other_time_player_b": [],
                            "last_time_player_a": ["普通红叉"],
                            "last_time_player_b": []
                        }
                    )

        def multi_player():
            quest_lists = {}
            for player in [1, 2]:
                my_opt = c_opt[f"magic_tower_alone_{player}"]
                quest_lists[player] = [
                    {
                        "player": [player],
                        "need_key": True,
                        "global_plan_active": my_opt["global_plan_active"],
                        "deck": my_opt["deck"],
                        "battle_plan_1p": my_opt["battle_plan_1p"],
                        "battle_plan_2p": my_opt["battle_plan_1p"],
                        "stage_id": "MT-1-" + str(my_opt["stage"]),
                        "max_times": int(my_opt["max_times"]),
                        "dict_exit": {
                            "other_time_player_a": [],
                            "other_time_player_b": [],
                            "last_time_player_a": ["普通红叉"],
                            "last_time_player_b": []
                        }
                    }
                ]

            # 信号无法使用 具名参数
            self.signal_start_todo_2_battle.emit({
                "quest_list": quest_lists[2],
                "extra_title": "多线程单人] [2P",
                "need_lock": True
            })
            self.battle_1_n_n(
                quest_list=quest_lists[1],
                extra_title="多线程单人] [1P",
                need_lock=True)

        def main():
            text_ = "单人魔塔"
            # 计算需使用该功能的玩家数
            active_player_count = sum(c_opt[f"magic_tower_alone_{player_id}"]["active"] for player_id in [1, 2])
            if active_player_count == 1:
                # 单人情况 以easy battle 完成即可
                self.model_start_print(text=text_)
                one_player()
                self.model_end_print(text=text_)
            if active_player_count == 2:
                # 多人情况 直接调用以lock battle 完成1P 以信号调用另一个todo 完成2P
                self.model_start_print(text=text_)
                multi_player()
                self.model_end_print(text=text_)
                # 休息五秒, 防止1P后完成任务, 跨线程解锁2P需要一定时间, 却在1P线程中再次激发start2P线程, 导致2P线程瘫痪
                sleep(5)

        main()

    def alone_magic_tower_prison(self):

        c_opt = self.opt_todo_plans

        def one_player():
            for player in [1, 2]:
                my_opt = c_opt[f"magic_tower_prison_{player}"]
                if my_opt["stage"]:
                    stage_list = ["MT-3-1", "MT-3-2", "MT-3-3", "MT-3-4"]
                else:
                    stage_list = ["MT-3-1", "MT-3-3", "MT-3-4"]

                if my_opt["active"]:
                    quest_list = []
                    for stage in stage_list:
                        quest_list.append(
                            {
                                "player": [player],
                                "need_key": True,
                                "global_plan_active": my_opt["global_plan_active"],
                                "deck": my_opt["deck"],
                                "battle_plan_1p": my_opt["battle_plan_1p"],
                                "battle_plan_2p": my_opt["battle_plan_1p"],
                                "stage_id": stage,
                                "max_times": 1,
                                "dict_exit": {
                                    "other_time_player_a": [],
                                    "other_time_player_b": [],
                                    "last_time_player_a": ["普通红叉"],
                                    "last_time_player_b": ["普通红叉"]
                                }
                            }
                        )
                    self.battle_1_n_n(quest_list=quest_list)

        def multi_player():
            quest_lists = {}
            for player in [1, 2]:
                my_opt = c_opt[f"magic_tower_prison_{player}"]
                if my_opt["stage"]:
                    stage_list = ["MT-3-1", "MT-3-2", "MT-3-3", "MT-3-4"]
                else:
                    stage_list = ["MT-3-1", "MT-3-3", "MT-3-4"]
                quest_lists[player] = []
                for stage in stage_list:
                    quest_lists[player].append(
                        {
                            "player": [player],
                            "need_key": False,
                            "global_plan_active": my_opt["global_plan_active"],
                            "deck": my_opt["deck"],
                            "battle_plan_1p": my_opt["battle_plan_1p"],
                            "battle_plan_2p": my_opt["battle_plan_1p"],
                            "stage_id": stage,
                            "max_times": 1,
                            "dict_exit": {
                                "other_time_player_a": [],
                                "other_time_player_b": [],
                                "last_time_player_a": ["普通红叉"],
                                "last_time_player_b": ["普通红叉"]
                            }
                        }
                    )

            # 信号无法使用 具名参数
            self.signal_start_todo_2_battle.emit({
                "quest_list": quest_lists[2],
                "extra_title": "多线程单人] [2P",
                "need_lock": True})
            self.battle_1_n_n(
                quest_list=quest_lists[1],
                extra_title="多线程单人] [1P",
                need_lock=True)

        def main():
            text_ = "魔塔密室"
            # 计算需使用该功能的玩家数
            active_player_count = sum(c_opt[f"magic_tower_prison_{player_id}"]["active"] for player_id in [1, 2])
            if active_player_count == 1:
                # 单人情况 以easy battle 完成即可
                self.model_start_print(text=text_)
                one_player()
                self.model_end_print(text=text_)
            if active_player_count == 2:
                # 多人情况 直接调用以lock battle 完成1P 以信号调用另一个todo 完成2P
                self.model_start_print(text=text_)
                multi_player()
                self.model_end_print(text=text_)
                # 休息五秒, 防止1P后完成任务, 跨线程解锁2P需要一定时间, 却在1P线程中再次激发start2P线程, 导致2P线程瘫痪
                sleep(5)

        main()

    def pet_temple(self):

        c_opt = self.opt_todo_plans

        def one_player():
            for player in [1, 2]:
                my_opt = c_opt[f"pet_temple_{player}"]
                if my_opt["active"]:
                    self.easy_battle(
                        text_=f"[萌宠神殿] [{player}P]",
                        stage_id="PT-0-" + str(my_opt["stage"]),
                        player=[player],
                        max_times=1,
                        global_plan_active=my_opt["global_plan_active"],
                        deck=my_opt["deck"],
                        battle_plan_1p=my_opt["battle_plan_1p"],
                        battle_plan_2p=my_opt["battle_plan_1p"],
                        dict_exit={
                            "other_time_player_a": [],
                            "other_time_player_b": [],
                            "last_time_player_a": ["回到上一级", "普通红叉"],
                            "last_time_player_b": ["回到上一级", "普通红叉"]
                        }
                    )

        def multi_player():
            quest_lists = {}
            for player in [1, 2]:
                my_opt = c_opt[f"pet_temple_{player}"]
                quest_lists[player] = [
                    {
                        "player": [player],
                        "need_key": True,
                        "global_plan_active": my_opt["global_plan_active"],
                        "deck": my_opt["deck"],
                        "battle_plan_1p": my_opt["battle_plan_1p"],
                        "battle_plan_2p": my_opt["battle_plan_1p"],
                        "stage_id": "PT-0-" + str(my_opt["stage"]),
                        "max_times": 1,
                        "dict_exit": {
                            "other_time_player_a": [],
                            "other_time_player_b": [],
                            "last_time_player_a": ["回到上一级", "普通红叉"],  # "回到上一级","普通红叉" 但之后刷新 所以空
                            "last_time_player_b": ["回到上一级", "普通红叉"],
                        }
                    }
                ]

            # 信号无法使用 具名参数
            self.signal_start_todo_2_battle.emit({
                "quest_list": quest_lists[2],
                "extra_title": "多线程单人] [2P",
                "need_lock": True
            })
            self.battle_1_n_n(
                quest_list=quest_lists[1],
                extra_title="多线程单人] [1P",
                need_lock=True)

        def main():
            text_ = "萌宠神殿"
            active_player_count = 0
            for player_id in [1, 2]:
                my_opt = c_opt[f"pet_temple_{player_id}"]
                if my_opt["active"]:
                    active_player_count += 1
            if active_player_count == 1:
                # 单人情况 以easy battle 完成即可
                self.model_start_print(text=text_)
                one_player()
                self.model_end_print(text=text_)
            if active_player_count == 2:
                # 多人情况 直接调用以lock battle 完成1P 以信号调用另一个todo 完成2P
                self.model_start_print(text=text_)
                multi_player()
                self.model_end_print(text=text_)
                # 休息五秒, 防止1P后完成任务, 跨线程解锁2P需要一定时间, 却在1P线程中再次激发start2P线程, 导致2P线程瘫痪
                sleep(5)

        main()

    """主要线程"""

    def set_extra_opt_and_start(self, extra_opt):
        self.extra_opt = extra_opt
        self.start()

    def run(self):
        if self.todo_id == 1:
            self.run_1()
        if self.todo_id == 2:
            self.run_2()

    def run_1(self):
        """单线程作战"""

        # current todo plan option
        c_opt = self.opt_todo_plans

        start_time = datetime.datetime.now()

        SIGNAL.PRINT_TO_UI.emit("每一个大类的任务开始前均会重启游戏以防止bug...")

        self.remove_outdated_log_images()

        """主要事项"""

        SIGNAL.PRINT_TO_UI.emit(text="", time=False)
        SIGNAL.PRINT_TO_UI.emit(text="[主要事项] 开始!", color_level=1)

        need_reload = False
        need_reload = need_reload or c_opt["sign_in"]["active"]
        need_reload = need_reload or c_opt["fed_and_watered"]["active"]
        need_reload = need_reload or c_opt["use_double_card"]["active"]
        need_reload = need_reload or c_opt["warrior"]["active"]
        if need_reload:
            self.batch_reload_game()

        my_opt = c_opt["sign_in"]
        if my_opt["active"]:
            self.batch_sign_in(
                player=[1, 2] if my_opt["is_group"] else [1]
            )

        my_opt = c_opt["fed_and_watered"]
        if my_opt["active"]:
            self.batch_fed_and_watered(
                player=[1, 2] if my_opt["is_group"] else [1]
            )

        my_opt = c_opt["use_double_card"]
        if my_opt["active"]:
            self.batch_use_items_double_card(
                player=[1, 2] if my_opt["is_group"] else [1],
                max_times=my_opt["max_times"],
            )

        my_opt = c_opt["warrior"]
        if my_opt["active"]:
            self.easy_battle(
                text_="勇士挑战",
                stage_id="NO-2-17",
                player=[2, 1] if my_opt["is_group"] else [1],
                max_times=int(my_opt["max_times"]),
                global_plan_active=my_opt["global_plan_active"],
                deck=my_opt["deck"],
                battle_plan_1p=my_opt["battle_plan_1p"],
                battle_plan_2p=my_opt["battle_plan_2p"],
                dict_exit={
                    "other_time_player_a": [],
                    "other_time_player_b": [],
                    "last_time_player_a": ["竞技岛"],
                    "last_time_player_b": ["竞技岛"]
                })

            # 勇士挑战在全部完成后, [进入竞技岛], 创建房间者[有概率]会保留勇士挑战选择关卡的界面.
            # 对于创建房间者, 在触发后, 需要设定完成后退出方案为[进入竞技岛 → 点X] 才能完成退出.
            # 对于非创建房间者, 由于号1不会出现选择关卡界面, 会因为找不到[X]而卡死.
            # 无论如何都会出现卡死的可能性.
            # 因此此处选择退出方案直接选择[进入竞技岛], 并将勇士挑战选择放在本大类的最后进行, 依靠下一个大类开始后的重启游戏刷新.

        need_reload = False
        need_reload = need_reload or c_opt["customize"]["active"]
        need_reload = need_reload or c_opt["normal_battle"]["active"]
        need_reload = need_reload or c_opt["offer_reward"]["active"]
        need_reload = need_reload or c_opt["cross_server"]["active"]

        if need_reload:
            self.batch_reload_game()

        my_opt = c_opt["customize"]
        if my_opt["active"]:
            self.task_sequence(
                text_="自定义任务序列",
                task_begin_id=my_opt["stage"],
                task_sequence_index=my_opt["battle_plan_1p"])

        my_opt = c_opt["normal_battle"]
        if my_opt["active"]:
            self.easy_battle(
                text_="常规刷本",
                stage_id=my_opt["stage"],
                player=[2, 1] if my_opt["is_group"] else [1],
                max_times=int(my_opt["max_times"]),
                global_plan_active=my_opt["global_plan_active"],
                deck=my_opt["deck"],
                battle_plan_1p=my_opt["battle_plan_1p"],
                battle_plan_2p=my_opt["battle_plan_2p"],
                dict_exit={
                    "other_time_player_a": [],
                    "other_time_player_b": [],
                    "last_time_player_a": ["竞技岛"],
                    "last_time_player_b": ["竞技岛"]
                })

        my_opt = c_opt["offer_reward"]
        if my_opt["active"]:
            self.offer_reward(
                text_="悬赏任务",
                global_plan_active=my_opt["global_plan_active"],
                deck=my_opt["deck"],
                max_times_1=my_opt["max_times_1"],
                max_times_2=my_opt["max_times_2"],
                max_times_3=my_opt["max_times_3"],
                battle_plan_1p=my_opt["battle_plan_1p"],
                battle_plan_2p=my_opt["battle_plan_2p"])

        my_opt = c_opt["cross_server"]
        if my_opt["active"]:
            self.easy_battle(
                text_="跨服副本",
                stage_id=my_opt["stage"],
                player=[1, 2] if my_opt["is_group"] else [1],
                max_times=int(my_opt["max_times"]),
                global_plan_active=my_opt["global_plan_active"],
                deck=my_opt["deck"],
                battle_plan_1p=my_opt["battle_plan_1p"],
                battle_plan_2p=my_opt["battle_plan_2p"],
                dict_exit={
                    "other_time_player_a": [],
                    "other_time_player_b": [],
                    "last_time_player_a": ["竞技岛"],
                    "last_time_player_b": ["竞技岛"]
                })

        need_reload = False
        need_reload = need_reload or c_opt["quest_guild"]["active"]
        need_reload = need_reload or c_opt["guild_dungeon"]["active"]
        need_reload = need_reload or c_opt["quest_spouse"]["active"]
        need_reload = need_reload or c_opt["relic"]["active"]

        if need_reload:
            self.batch_reload_game()

        if c_opt["quest_guild"]["active"]:
            self.guild_or_spouse_quest(
                title_text="公会任务",
                quest_mode="公会任务",
                global_plan_active=c_opt["quest_guild"]["global_plan_active"],
                deck=c_opt["quest_guild"]["deck"],
                battle_plan_1p=c_opt["quest_guild"]["battle_plan_1p"],
                battle_plan_2p=c_opt["quest_guild"]["battle_plan_2p"],
                stage=c_opt["quest_guild"]["stage"])

        if c_opt["guild_dungeon"]["active"]:
            self.guild_dungeon(
                text_="公会副本",
                global_plan_active=c_opt["quest_guild"]["global_plan_active"],
                deck=c_opt["quest_guild"]["deck"],
                battle_plan_1p=c_opt["quest_guild"]["battle_plan_1p"],
                battle_plan_2p=c_opt["quest_guild"]["battle_plan_2p"])

        if c_opt["quest_spouse"]["active"]:
            self.guild_or_spouse_quest(
                title_text="情侣任务",
                quest_mode="情侣任务",
                global_plan_active=c_opt["quest_guild"]["global_plan_active"],
                deck=c_opt["quest_guild"]["deck"],
                battle_plan_1p=c_opt["quest_guild"]["battle_plan_1p"],
                battle_plan_2p=c_opt["quest_guild"]["battle_plan_2p"])

        my_opt = c_opt["relic"]
        if my_opt["active"]:
            self.easy_battle(
                text_="火山遗迹",
                stage_id=my_opt["stage"],
                player=[2, 1] if my_opt["is_group"] else [1],
                max_times=int(my_opt["max_times"]),
                global_plan_active=my_opt["global_plan_active"],
                deck=my_opt["deck"],
                battle_plan_1p=my_opt["battle_plan_1p"],
                battle_plan_2p=my_opt["battle_plan_2p"],
                dict_exit={
                    "other_time_player_a": [],
                    "other_time_player_b": [],
                    "last_time_player_a": ["竞技岛"],
                    "last_time_player_b": ["竞技岛"]
                })

        need_reload = False
        need_reload = need_reload or c_opt["magic_tower_alone_1"]["active"]
        need_reload = need_reload or c_opt["magic_tower_alone_2"]["active"]
        need_reload = need_reload or c_opt["magic_tower_prison_1"]["active"]
        need_reload = need_reload or c_opt["magic_tower_prison_2"]["active"]
        need_reload = need_reload or c_opt["magic_tower_double"]["active"]
        need_reload = need_reload or c_opt["pet_temple_1"]["active"]
        need_reload = need_reload or c_opt["pet_temple_2"]["active"]
        if need_reload:
            self.batch_reload_game()

        self.alone_magic_tower()

        self.alone_magic_tower_prison()

        self.pet_temple()

        my_opt = c_opt["magic_tower_double"]
        if my_opt["active"]:
            self.easy_battle(
                text_="魔塔双人",
                stage_id="MT-2-" + str(my_opt["stage"]),
                player=[2, 1],
                max_times=int(my_opt["max_times"]),
                global_plan_active=my_opt["global_plan_active"],
                deck=my_opt["deck"],
                battle_plan_1p=my_opt["battle_plan_1p"],
                battle_plan_2p=my_opt["battle_plan_2p"],
                dict_exit={
                    "other_time_player_a": [],
                    "other_time_player_b": ["回到上一级"],
                    "last_time_player_a": ["普通红叉"],
                    "last_time_player_b": ["回到上一级"]
                }
            )

        SIGNAL.PRINT_TO_UI.emit(
            text=f"[主要事项] 全部完成! 耗时:{str(datetime.datetime.now() - start_time).split('.')[0]}",
            color_level=1)

        """额外事项"""

        extra_active = False
        extra_active = extra_active or c_opt["receive_awards"]["active"]
        extra_active = extra_active or c_opt["use_items"]["active"]
        extra_active = extra_active or c_opt["auto_food"]["active"]
        extra_active = extra_active or c_opt["loop_cross_server"]["active"]

        if extra_active:
            SIGNAL.PRINT_TO_UI.emit(text="", time=False)
            SIGNAL.PRINT_TO_UI.emit(text=f"[额外事项] 开始!", color_level=1)
            self.batch_reload_game()
            start_time = datetime.datetime.now()

        my_opt = c_opt["receive_awards"]
        if my_opt["active"]:
            self.batch_receive_all_quest_rewards(
                player=[1, 2] if my_opt["is_group"] else [1],
                quests=["普通任务", "美食大赛", "大富翁"],
                advance_mode=True,
            )

        my_opt = c_opt["use_items"]
        if my_opt["active"]:
            self.batch_use_items_consumables(
                player=[1, 2] if my_opt["is_group"] else [1],
            )

        my_opt = c_opt["auto_food"]
        if my_opt["active"]:
            self.auto_food(
                deck=my_opt["deck"],
            )

        my_opt = c_opt["loop_cross_server"]
        if my_opt["active"]:
            self.batch_loop_cross_server(
                player=[1, 2] if my_opt["is_group"] else [1],
                deck=c_opt["quest_guild"]["deck"])

        if extra_active:
            SIGNAL.PRINT_TO_UI.emit(
                text=f"[额外事项] 全部完成! 耗时:{str(datetime.datetime.now() - start_time).split('.')[0]}",
                color_level=1)
        else:
            SIGNAL.PRINT_TO_UI.emit(
                text=f"[额外事项] 未启动.",
                color_level=1)

        """自建房战斗"""

        active_singleton = False
        active_singleton = active_singleton or c_opt["customize_battle"]["active"]

        if active_singleton:
            SIGNAL.PRINT_TO_UI.emit(text="", time=False)
            SIGNAL.PRINT_TO_UI.emit(text="[自建房战斗] 开始! 如出现错误, 务必确保该功能是单独启动的!", color_level=1)
            start_time = datetime.datetime.now()

        my_opt = c_opt["customize_battle"]
        if my_opt["active"]:
            self.easy_battle(
                text_="自建房战斗",
                stage_id="CU-0-0",
                player=[[1, 2], [2, 1], [1], [2]][my_opt["is_group"]],
                max_times=int(my_opt["max_times"]),
                global_plan_active=False,
                deck=my_opt["deck"],
                battle_plan_1p=my_opt["battle_plan_1p"],
                battle_plan_2p=my_opt["battle_plan_2p"],
                dict_exit={
                    "other_time_player_a": [],
                    "other_time_player_b": [],
                    "last_time_player_a": [],
                    "last_time_player_b": []
                }
            )

        if active_singleton:
            SIGNAL.PRINT_TO_UI.emit(
                text=f"[自建房战斗] 全部完成! 耗时:{str(datetime.datetime.now() - start_time).split('.')[0]}",
                color_level=1)

        """全部完成"""
        if self.opt["advanced_settings"]["end_exit_game"]:
            self.batch_click_refresh_btn()
        else:
            SIGNAL.PRINT_TO_UI.emit(
                text="推荐勾选高级设置-完成后刷新游戏, 防止长期运行flash导致卡顿",
                color_level=1)

        # 全部完成了发个信号
        SIGNAL.END.emit()

    def run_2(self):
        """多线程作战时的第二线程, 负责2P"""

        self.battle_1_n_n(
            quest_list=self.extra_opt["quest_list"],
            extra_title=self.extra_opt["extra_title"],
            need_lock=self.extra_opt["need_lock"])
        self.extra_opt = None
