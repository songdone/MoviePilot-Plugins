import os
import logging
from moviepilot.core.plugin import PluginBase
from moviepilot.core.event import eventmanager, EventType

class HardLinkCheckerPlugin(PluginBase):
    """
    媒体库硬链接检测插件，用于检查媒体库中的硬链接并忽略小文件。
    """
    # 插件基本信息
    plugin_name = "HardLinkChecker"  # 确保插件名称与package.json中的displayName一致
    plugin_version = "1.0.0"
    plugin_description = "检查指定媒体库目录中的硬链接，忽略小文件"

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(__name__)  # 初始化日志记录器
        self.minimum_file_size = 10 * 1024 * 1024  # 10 MB, 小文件的最大值

    def check_hardlinks_in_directory(self, media_directory):
        """
        检查指定媒体库目录中的硬链接，忽略小文件。
        :param media_directory: 媒体库的根目录
        :return: 硬链接信息列表
        """
        # 检查目录是否存在
        if not os.path.exists(media_directory):
            self.logger.error(f"指定的媒体库目录不存在: {media_directory}")
            return None

        hardlink_info = []
        # 遍历目录
        for root, dirs, files in os.walk(media_directory):
            for file in files:
                file_path = os.path.join(root, file)
                # 检查文件大小
                if os.path.getsize(file_path) >= self.minimum_file_size:
                    try:
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
        # 从配置中获取媒体库路径
        media_directory = self.config.get('media_directory', '默认媒体库路径')
        # 执行硬链接检查
        hardlinks = self.check_hardlinks_in_directory(media_directory)
        if hardlinks:
            self.logger.info(f"检测到以下硬链接: {hardlinks}")
        else:
            self.logger.info("未检测到硬链接或所有文件均为小文件。")

# 插件入口点
if __name__ == "__main__":
    # 实例化插件并注册事件
    plugin = HardLinkCheckerPlugin()
    eventmanager.register(EventType.PluginReload, plugin.on_plugin_reload)
