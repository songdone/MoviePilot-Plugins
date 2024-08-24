# 媒体库硬链接检测插件

## 简介

“媒体库硬链接检测”是一个为 MoviePilot 设计的插件，旨在帮助用户检测和统计媒体库中硬链接的成功和失败情况。该插件会忽略掉小文件（例如海报和字幕文件），专注于检测实际的媒体文件，以确保媒体库的完整性和节省磁盘空间。

## 功能

- 检测媒体库中的硬链接文件。
- 忽略小于 10MB 的文件，只统计媒体文件。
- 显示检测到的硬链接数量和文件总数。
- 生成检测日志，便于用户查看详细的硬链接信息。

## 使用方法

1. 在 MoviePilot 的插件管理界面中找到“媒体库硬链接检测”插件并启用。
2. 进入插件设置页面，输入你要检测的媒体库根目录路径。
3. 点击“开始检测”按钮，插件将自动扫描指定目录下的媒体文件，并输出硬链接检测报告。

## 日志查看

检测完成后，插件会在日志中记录检测到的硬链接和文件数量信息。可以通过 MoviePilot 的日志查看功能来查看详细的检测结果。

## 注意事项

- 确保输入的媒体库路径是正确的，并且有访问权限。
- 检测过程中不要关闭 MoviePilot 应用程序，以避免检测被中断。
- 插件的检测结果只针对媒体文件，小于 10MB 的文件将被忽略。

## 贡献

欢迎对该插件进行改进和优化！请在 GitHub 上提交 pull request 或 issue。

## 许可

此插件遵循 MIT 许可证进行开源发布。请查看 LICENSE 文件以了解更多信息。
