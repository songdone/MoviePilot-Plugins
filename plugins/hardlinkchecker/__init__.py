import os
import logging
from moviepilot.core.plugin import PluginBase
from moviepilot.core.event import eventmanager, EventType

class HardLinkCheckerPlugin(PluginBase):
    """
    媒体库硬链接检测插件，用于检查媒体库中的硬链接并忽略小文件。
    """
    # 插件基本信息
    plugin_name = "HardLinkChecker"  # 确保插件名称与 package.json 中的 displayName 一致
    plugin_version = "1.0.0"
    plugin_description = "检查指定媒体库目录中的硬链接，忽略小文件"
    plugin_author = "songdone"  # 替换为你的名字或GitHub用户名

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(self.plugin_name)  # 初始化日志记录器
        self.minimum_file_size = 10 * 1024 * 1024  # 10 MB，小文件的最大值
        self.media_directory = None  # 媒体库目录路径初始化为 None

    def load_config(self):
        """
        加载插件配置，例如媒体库目录路径。
        """
        # 从配置中获取媒体库路径，如果没有设置则使用默认路径
        self.media_directory = self.config.get('media_directory', '/path/to/default/media/library')
        if not self.media_directory or not os.path.exists(self.media_directory):
            self.logger.error(f"无效的媒体库目录: {self.media_directory}")

    def check_hardlinks_in_directory(self):
        """
        检查指定媒体库目录中的硬链接，忽略小文件。
        :return: 硬链接信息列表
        """
        if not self.media_directory:
            self.logger.error("媒体库目录未配置或无效")
            return None

        hardlink_info = []
        # 遍历目录
        for root, dirs, files in os.walk(self.media_directory):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    # 检查文件大小
                    if os.path.getsize(file_path) >= self.minimum_file_size:
                        stat_info = os.stat(file_path)
                        hardlink_count = stat_info.st_nlink
                        # 硬链接数量大于1
                        if hardlink_count > 1:
                            hardlink_info.append((file_path, hardlink_count))
                except OSError as e:
                    self.logger.error(f"检查文件 {file_path} 时发生错误: {e}")

        return hardlink_info

    def on_plugin_reload(self, event):
        """
        当插件重新加载时执行的操作。
        """
        self.logger.info("正在重新加载媒体库硬链接检测插件...")
        self.load_config()  # 加载配置
        # 执行硬链接检查
        hardlinks = self.check_hardlinks_in_directory()
        if hardlinks:
            self.logger.info(f"检测到以下硬链接: {hardlinks}")
        else:
            self.logger.info("未检测到硬链接或所有文件均为小文件。")

# 插件入口点
if __name__ == "__main__":
    # 实例化插件并注册事件
    plugin = HardLinkCheckerPlugin()
    eventmanager.register(EventType.PluginReload, plugin.on_plugin_reload)
