"""pytest 前置：把插件目录加入 sys.path。

测试要同时覆盖「纯逻辑模块」与「plugin.py 本身」：
    - url_parser / music_api / audio_cache 是平铺模块，加路径即可导入
    - plugin.py 优先尝试相对导入，平铺失败后回退绝对导入，因此也需要这条路径
"""

from __future__ import annotations

import pathlib
import sys

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
