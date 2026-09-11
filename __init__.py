"""点歌插件（github.cateye.music-request）。

本包的模块划分：
    plugin.py       入口、配置模型、生命周期、Command/Tool/Hook 组件
    music_api.py    网易云 / QQ音乐 / NapCat 的 HTTP 客户端
    url_parser.py   音乐链接与分享卡片的纯逻辑解析
    audio_cache.py  voice 模式的本地音频缓存（LRU + 过期清理）
"""
