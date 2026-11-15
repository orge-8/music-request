# -*- coding: utf-8 -*-
"""WebUI 配置 Schema 显示层回归测试：走插件实例的真实链路。

真机 Runner 调的就是 ``plugin.get_webui_config_schema``（异常被吞 →
空 Schema → 配置页整页空白），所以这里必须走插件实例，而不是直接调
SDK 生成器。
"""
import importlib
import pathlib
import sys

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    # 包式加载（与真机一致）：__init__.py 可能只是引导文件，需下沉到 .plugin 子模块
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")
_PLUGIN = _MOD.create_plugin()


def _webui_schema() -> dict:
    return _PLUGIN.get_webui_config_schema(
        plugin_id=getattr(_MOD, "__plugin_id__", _PLUGIN_DIR.name),
        plugin_name=_PLUGIN_DIR.name,
        plugin_version="",
        plugin_description="",
        plugin_author="",
    ) or {}


def _webui_sections() -> dict:
    sections = _webui_schema().get("sections") or {}
    assert sections, "schema 无 sections：Runner 会渲染出整页空白"
    return sections


def test_sections_have_visible_fields():
    sections = _webui_sections()
    assert any(
        not f.get("hidden")
        for s in sections.values()
        for f in (s.get("fields") or {}).values()
    ), "所有字段均被隐藏，配置页会空白"


def test_no_object_fields():
    for s in _webui_sections().values():
        for f in (s.get("fields") or {}).values():
            assert not (f.get("type") == "object" or f.get("ui_type") == "json"), (
                "嵌套对象落 default 文本框会显示 [object Object]"
            )


def test_description_also_visible_as_hint():
    for s in _webui_sections().values():
        for f in (s.get("fields") or {}).values():
            if f.get("hidden"):
                continue
            if (f.get("description") or "").strip():
                assert (f.get("hint") or "").strip(), (
                    f"字段 {f.get('name')} 的说明在可视化模式不可见（需抄进 hint）"
                )


def test_labels_not_raw_keys():
    for s in _webui_sections().values():
        for fname, f in (s.get("fields") or {}).items():
            if f.get("hidden"):
                continue
            label = (f.get("label") or "").strip()
            assert label and label != fname, f"字段 {fname} 仍以英文键名当 label"


def test_section_titles_not_raw_keys():
    for name, s in _webui_sections().items():
        title = (s.get("title") or "").strip()
        assert title and title != name, f"节 {name} 仍以键名当标题"


def test_config_version_hidden_if_present():
    for s in _webui_sections().values():
        for fname, f in (s.get("fields") or {}).items():
            if fname == "config_version":
                assert f.get("hidden") is True, "config_version 应只在源代码模式可见"


def test_labels_have_no_broken_truncation():
    """CJK 硬切容易留下半句话 label（如「uin——QQ音乐登录账」），一律视为坏 label。"""
    bad_tail = ("的", "是否", "和", "与", "「", "（", "(", "【", "，", ",", "、", "—", "-")
    for s in _webui_sections().values():
        for fname, f in (s.get("fields") or {}).items():
            if f.get("hidden"):
                continue
            label = (f.get("label") or "").strip()
            assert len(label) <= 20, f"字段 {fname} 的 label 过长（{len(label)} 字）会被前端截断: {label}"
            assert not label.endswith(bad_tail), f"字段 {fname} 的 label 断句不完整: {label}"


def test_tabs_cover_every_section():
    """tabs 布局下前端只渲染 tab.sections 里的节，漏一个就等于该节整块消失。"""
    schema = _webui_schema()
    layout = schema.get("layout") or {}
    assert layout.get("type") == "tabs", "8 个配置节平铺会淹没常用配置，应分组页签"
    tabs = layout.get("tabs") or []
    assert tabs, "layout 声明为 tabs 却没有任何页签"
    listed = [name for tab in tabs for name in (tab.get("sections") or [])]
    assert sorted(listed) == sorted(schema["sections"]), (
        f"页签未覆盖全部配置节：遗漏 {set(schema['sections']) - set(listed)}"
    )
    assert len(listed) == len(set(listed)), f"同一节被多个页签重复引用: {listed}"
    for tab in tabs:
        assert tab.get("id") and tab.get("title"), f"页签缺 id/title: {tab}"


def test_optional_sections_collapsed():
    """凭据/高级节默认收起；qq 是搜索必需项、music 是主配置，保持展开。"""
    sections = _webui_sections()
    assert sections["music"].get("collapsed") is not True
    assert sections["qq"].get("collapsed") is not True, (
        "uin + qqmusic_key 是 QQ音乐搜索必需项，折叠起来等于把第一个要配的开关藏起来"
    )
    for name in ("netease", "napcat", "cache", "library", "bilibili"):
        assert sections[name].get("collapsed") is True, f"可选节 {name} 应默认收起"


def test_credential_and_path_fields_have_placeholder():
    """凭据/路径/格式敏感字段必须给占位示例（灰字提示，不改校验语义）。"""
    required = {
        ("netease", "music_u"), ("netease", "csrf_token"),
        ("qq", "uin"), ("qq", "qqmusic_key"),
        ("napcat", "http_url"), ("napcat", "http_token"),
        ("cache", "storage_dir"), ("cache", "napcat_dir"),
        ("library", "music_dir"), ("library", "napcat_dir"),
        ("bilibili", "sessdata"), ("bilibili", "buvid3"),
        ("music", "command_prefix"),
    }
    sections = _webui_sections()
    for sec_name, fname in required:
        field = sections[sec_name]["fields"][fname]
        assert (field.get("placeholder") or "").strip(), f"{sec_name}.{fname} 缺 placeholder 示例"


def test_napcat_dir_labels_distinguished_by_section():
    """cache / library 各有一个 napcat_dir，label 必须能区分，否则用户填串目录。"""
    sections = _webui_sections()
    cache_label = sections["cache"]["fields"]["napcat_dir"]["label"]
    lib_label = sections["library"]["fields"]["napcat_dir"]["label"]
    assert cache_label != lib_label, "两个 napcat_dir 的 label 相同，用户无法区分"
    assert "缓存" in cache_label and "NapCat" in cache_label
    assert "歌曲库" in lib_label and "NapCat" in lib_label


def test_field_groups_render_subheadings():
    """大节内按 group 分块渲染小标题；未分组字段留在最前的无标题块。"""
    sections = _webui_sections()
    music_fields = sections["music"]["fields"]
    assert music_fields["search_limit"].get("group") == "搜索与选歌"
    assert music_fields["auto_parse_url"].get("group") == "链接与卡片解析"
    assert music_fields["play_mode"].get("group") == "发送形态"
    assert not music_fields["default_platform"].get("group"), "主开关应留在无标题首块"
    cache_fields = sections["cache"]["fields"]
    assert cache_fields["max_size_mb"].get("group") == "容量与清理"
    assert not cache_fields["storage_dir"].get("group")
    lib_fields = sections["library"]["fields"]
    assert lib_fields["music_dir"].get("group") == "目录"
    assert lib_fields["send_mode"].get("group") == "发送与检索"
    assert not lib_fields["enabled"].get("group"), "启用开关应留在无标题首块"


def test_relevance_floor_has_step():
    """相关度地板是 0~1 浮点，给步进提示；不碰 min/max 校验语义。"""
    field = _webui_sections()["music"]["fields"]["relevance_floor"]
    assert field.get("step") == 0.05
    assert field.get("min") == 0.0 and field.get("max") == 1.0, (
        "显示层不得改变 pydantic ge/le 带来的校验边界"
    )
