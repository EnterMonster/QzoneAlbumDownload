# QQ空间相册原图批量下载器 (GUI版)

轻量可视化工具，支持 QQ 空间相册原图批量下载。

## 功能

- 原图下载（通过 floatview 接口获取 raw URL）
- Cookie 过期自动检测 & 弹窗输入新 Cookie 续传
- 断点续传（已下载自动跳过）
- 实时进度和日志

## 使用方法

1. 安装依赖

```bash
pip install -r requirements.txt
```

2. 运行程序

```bash
python qq_album_downloader_gui.py
```

3. 填入 QQ 号和 Cookie → 获取相册 → 勾选 → 开始下载

## 获取 Cookie

浏览器登录 https://user.qzone.qq.com → F12 → Network → 点击页面任意操作 → 找 `qzone.qq.com` 请求 → 复制 Cookie 请求头
