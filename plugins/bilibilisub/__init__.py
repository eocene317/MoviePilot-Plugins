import time
from typing import Any, List, Dict, Tuple, Optional

from threading import Lock
import datetime

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


# from app.core.event import eventmanager, Event
from threading import Event
from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import WebhookEventInfo, ServiceInfo
from app.schemas.types import EventType, MediaType, MediaImageType, NotificationType
from app.utils.web import WebUtils
from dataclasses import dataclass


import xml.etree.cElementTree as ET
import requests
import subprocess as sp
import os
from glob import glob

lock = Lock()

@dataclass
class BilibiliDownloaderConfig:
    sublist: List[str]
    rsshub_baseurl: str
    base_save_dir: str
    remux_format: str
    cookies_file: str
    ytdlp_params: str

class BilibiliDownloader:
    def __init__(self, config: BilibiliDownloaderConfig, event: Event):
        self.sublist = config.sublist
        self.rsshub_baseurl = config.rsshub_baseurl
        self.base_save_dir = config.base_save_dir
        self.remux_format = config.remux_format
        self.cookies_file = config.cookies_file
        self.event = event
        self.ytdlp_params = config.ytdlp_params if config.ytdlp_params else ""
        self.check_params()
        self.video_list = self.get_exist_video()
    
    def check_params(self):
        if not isinstance(self.sublist, list):
            raise ValueError("sublist must be list")
        if self.rsshub_baseurl is None:
            raise ValueError("rsshub_baseurl must set")
        if self.base_save_dir is None:
            raise ValueError("base_save_dir must set")
        if self.remux_format is None:
            raise ValueError("remux_format must set")
        if self.cookies_file is None:
            raise ValueError("cookies_file must set")

    def get_exist_video(self) -> None:
        if not os.path.exists(self.base_save_dir):
            os.makedirs(self.base_save_dir)
        return glob(os.path.join(self.base_save_dir, f"**/*.{self.remux_format}"), recursive=True)

    def check_exist(self, video_id: str) -> bool:
        for video in self.video_list:
            if video_id in video and video.endswith(f'{self.remux_format}'):
                return True
        return False
    
    @staticmethod
    def run_process_with_event(cmd: str, stop_event: Event, check_interval=0.5):
        """运行命令，可被stop_event终止"""
        proc = sp.Popen(cmd, shell=True)
        
        try:
            while True:
                if stop_event.is_set():
                    proc.terminate()
                    break
                if proc.poll() is not None:  # 进程已结束
                    break
                time.sleep(check_interval)
        finally:
            # 确保进程被终止
            if proc.poll() is None:
                proc.kill()
        return proc.returncode
    
    def download(self, url, save_dir):
        cmd = f"yt-dlp -f bestvideo+bestaudio {self.ytdlp_params} {url} --cookies {self.cookies_file} -P {save_dir} -t {self.remux_format}"
        # sp.run(cmd, shell=True)
        BilibiliDownloader.run_process_with_event(cmd, self.event)

    def download_from_rss(self):
        logger.info(f"Start check {self.sublist}")
        for subid in self.sublist:
            if self.event and self.event.is_set():
                return
            url = f"{self.rsshub_baseurl}/bilibili/user/video/{subid}"
            resp = requests.get(url)
            root = ET.fromstring(resp.text)
            for video_item in root.find('channel').findall('item'):
                if self.event and self.event.is_set():
                    return
                title = video_item.find('title').text
                guid = video_item.find('guid').text
                pub_date = video_item.find('pubDate').text
                author = video_item.find('author').text
                video_id = guid.split('/')[-1]
                if self.check_exist(video_id=video_id):
                    logger.info(f"{title} exists continue")
                    continue
                self.download(guid, os.path.join(self.base_save_dir, author))

class BiliBiliSub(_PluginBase):
    # 插件名称
    plugin_name = "BiliBili订阅"
    # 插件描述
    plugin_desc = "订阅并下载UP主投稿视频"
    # 插件图标
    plugin_icon = "Bililive_recorder_A.png"
    # 插件版本
    plugin_version = "2.2"
    # 插件作者
    plugin_author = "zerowang"
    # 作者主页
    author_url = "https://github.com/eocene317"
    # 插件配置项ID前缀
    plugin_config_prefix = "bilibilisub_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler: Optional[BackgroundScheduler] = None
    _event = Event()

    _enabled = False
    _sublist = None
    _ytdlp_params = ""
    _rsshub_baseurl = None
    _base_save_dir = None
    _remux_format = None
    _cookies_file = None
    _run_once = False
    _cron = None

    def check_params(self):
        if not self._sublist\
        or not self._rsshub_baseurl\
        or not self._base_save_dir\
        or not self._remux_format\
        or not self._cookies_file:
            return False
        return True

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = config.get("enabled")
            self._sublist = config.get("sublist")
            self._rsshub_baseurl = config.get("rsshub_baseurl")
            self._base_save_dir = config.get("base_save_dir")
            self._remux_format = config.get("remux_format")
            self._cookies_file = config.get("cookies_file")
            self._run_once = config.get("run_once")
            self._cron = config.get("cron")
            self._ytdlp_params = config.get("ytdlp_params")

        if self._enabled and not self.check_params():
            logger.warning(f"[BilibiliSubscribe] check params error")
            self._enabled = False
            self.__update_config()
            return

        if self._run_once:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"Bilibili订阅服务启动，立即运行一次")
            self._scheduler.add_job(func=self.check, trigger='date',
                                    run_date=datetime.datetime.now(
                                        tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                    )

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

        # 重置
        if self._run_once:
            self._run_once = False
            self.__update_config()
    
    def __update_config(self):
        """
        更新设置
        """
        self.update_config({
            "enabled": self._enabled,
            "sublist": self._sublist,
            "rsshub_baseurl": self._rsshub_baseurl,
            "base_save_dir": self._base_save_dir,
            "remux_format": self._remux_format,
            "cookies_file": self._cookies_file,
            "run_once": self._run_once,
            "cron": self._cron,
            "ytdlp_params": self._ytdlp_params
        })

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "BilibiliSubscribe",
                "name": "Bilibili订阅服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.check,
                "kwargs": {}
            }]
        elif self._enabled:
            return [{
                "id": "BilibiliSubscribe",
                "name": "Bilibili订阅服务",
                "trigger": "interval",
                "func": self.check,
                "kwargs": {"minutes": 30}
            }]
        return []
    

    def check(self):
        if isinstance(self._sublist, str):
            sublist = self._sublist.strip().split("\n")
        else:
            sublist = None
        conf = BilibiliDownloaderConfig(
            sublist=sublist,
            rsshub_baseurl = self._rsshub_baseurl,
            base_save_dir = self._base_save_dir,
            remux_format = self._remux_format,
            cookies_file = self._cookies_file,
            ytdlp_params = self._ytdlp_params
        )
        try:
            bd = BilibiliDownloader(conf, self._event)
            logger.info("start download from rss")
            bd.download_from_rss()
        except Exception as e:
            logger.error(e)
        


    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'run_once',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'rsshub_baseurl',
                                            'label': 'rsshub',
                                            'rows': 1,
                                            'placeholder': 'http://192.168.1.5:1200'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'base_save_dir',
                                            'label': '保存路径',
                                            'rows': 1,
                                            'placeholder': '/Download'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'cookies_file',
                                            'label': 'cookie文件',
                                            'rows': 1,
                                            'placeholder': 'www.bilibili.com_cookies.txt'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 3,
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'remux_format',
                                            'label': '格式',
                                            'rows': 1,
                                            'placeholder': 'mkv'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12
                        },
                        'content': [
                            {
                                'component': 'VTextarea',
                                'props': {
                                    'model': 'ytdlp_params',
                                    'label': '额外yt-dlp参数',
                                    'rows': 1,
                                    'placeholder': ''
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'sublist',
                                            'label': '订阅UP主ID，每行一个',
                                            'rows': 10,
                                            'placeholder': '每行一个UP主ID'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "types": []
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown(wait=True)
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
