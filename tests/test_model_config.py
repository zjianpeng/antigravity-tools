"""一键配置模型功能 —— 单元测试

覆盖需求：
1. 所有模型支持图片输入（vision: true）
2. 新增模型 Hy3 / Kimi-K2.7-Code
3. 用户选择式配置（多选对话框存在且可用）
4. 增量替换逻辑（按 id+name 匹配：存在则合并替换，不存在则追加；未选中保留不变）
5. models.json 两种格式（WorkBuddy 裸数组 / CodeBuddy {"models": [...]}）读写正确

运行：
    cd F:/自制工具/antigravity-tools
    python -m unittest tests.test_model_config -v
"""

import json
import os
import sys
import tempfile
import unittest

# 确保项目根目录在 sys.path 中，便于直接运行
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.ui.pages.api_proxy import (  # noqa: E402
    ApiProxyPage,
    ModelSelectDialog,
    _incremental_merge_models,
    _read_existing_models,
    _write_models_json,
)
from src.modules.proxy_server import (  # noqa: E402
    SUPPORTED_MODELS,
    MODEL_CONTEXT_LENGTHS,
    MODEL_MAX_OUTPUT_TOKENS,
    MODEL_SUPPORTS_IMAGES,
)


class TestNewModelsAndImageSupport(unittest.TestCase):
    """需求 1 & 2：新增模型 + 全模型支持图片输入。"""

    def test_new_models_in_config_list(self):
        """Hy3 与 Kimi-K2.7-Code 出现在可配置模型列表中。"""
        self.assertIn("hy3", ApiProxyPage.SUPPORTED_CONFIG_MODELS)
        self.assertIn("kimi-k2.7-code", ApiProxyPage.SUPPORTED_CONFIG_MODELS)
        # 原有模型仍存在
        self.assertIn("glm-5.2", ApiProxyPage.SUPPORTED_CONFIG_MODELS)
        self.assertIn("glm-5.1", ApiProxyPage.SUPPORTED_CONFIG_MODELS)

    def test_all_models_support_images_in_capabilities(self):
        """MODEL_CAPABILITIES 中所有模型的 images 字段均为 True。"""
        for model_id, (_tool, images, _reason) in ApiProxyPage.MODEL_CAPABILITIES.items():
            self.assertTrue(images, f"模型 {model_id} 应支持图片输入，但 images=False")

    def test_model_display_names_match_screenshot(self):
        """显示名按截图大小写处理，id 保持小写（与图片文件名一致）。"""
        self.assertEqual(ApiProxyPage.MODEL_DISPLAY_NAMES.get("hy3"), "Hy3")
        self.assertEqual(ApiProxyPage.MODEL_DISPLAY_NAMES.get("kimi-k2.7-code"), "Kimi-K2.7-Code")
        # 未列出的模型显示名等于 id（默认）
        self.assertEqual(ApiProxyPage.MODEL_DISPLAY_NAMES.get("glm-5.2", "glm-5.2"), "glm-5.2")

    def test_proxy_server_new_models_registered(self):
        """新增模型已在 proxy_server 的各注册表中登记。"""
        for m in ("hy3", "kimi-k2.7-code"):
            self.assertIn(m, SUPPORTED_MODELS, f"{m} 未在 SUPPORTED_MODELS 中")
            self.assertIn(m, MODEL_CONTEXT_LENGTHS, f"{m} 未在 MODEL_CONTEXT_LENGTHS 中")
            self.assertIn(m, MODEL_MAX_OUTPUT_TOKENS, f"{m} 未在 MODEL_MAX_OUTPUT_TOKENS 中")
            self.assertIn(m, MODEL_SUPPORTS_IMAGES, f"{m} 未在 MODEL_SUPPORTS_IMAGES 中")

    def test_proxy_server_all_images_true(self):
        """proxy_server 的 MODEL_SUPPORTS_IMAGES 全部为 True（含原先为 False 的 glm-5.0 等）。"""
        for m in ("glm-5.0", "glm-5.0-turbo", "glm-4.7", "glm-4.6", "glm-5.1", "glm-5.2"):
            self.assertTrue(MODEL_SUPPORTS_IMAGES.get(m), f"{m} 应支持图片输入")


class TestModelSelectDialog(unittest.TestCase):
    """需求 3：用户选择式配置对话框。"""

    def test_dialog_is_importable_and_constructable(self):
        """ModelSelectDialog 可被构造（需 QApplication）。"""
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        dlg = ModelSelectDialog(
            "测试", "http://127.0.0.1:8080/v1", ApiProxyPage.SUPPORTED_CONFIG_MODELS
        )
        # 默认全部勾选
        self.assertEqual(dlg.selected_models(), list(ApiProxyPage.SUPPORTED_CONFIG_MODELS))
        # 全不选
        dlg._set_all(False)
        self.assertEqual(dlg.selected_models(), [])
        # 重新全选
        dlg._set_all(True)
        self.assertEqual(dlg.selected_models(), list(ApiProxyPage.SUPPORTED_CONFIG_MODELS))


class TestReadExistingModels(unittest.TestCase):
    """models.json 两种格式的读取兼容性。"""

    def test_read_bare_array(self):
        """WorkBuddy 格式：裸数组。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "models.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump([{"id": "a", "name": "a"}], f)
            self.assertEqual(_read_existing_models(p), [{"id": "a", "name": "a"}])

    def test_read_wrapped_object(self):
        """CodeBuddy 格式：{"models": [...]}。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "models.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"models": [{"id": "b", "name": "b"}]}, f)
            self.assertEqual(_read_existing_models(p), [{"id": "b", "name": "b"}])

    def test_read_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "models.json")
            self.assertEqual(_read_existing_models(p), [])

    def test_read_corrupt_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "models.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not valid json")
            self.assertEqual(_read_existing_models(p), [])


class TestIncrementalMerge(unittest.TestCase):
    """需求 4：增量替换逻辑。"""

    def test_replace_by_id_and_name_with_field_merge(self):
        """id 与 name 都相同 → 替换，并保留旧条目独有字段、用新字段覆盖。"""
        existing = [
            {"id": "glm-5.2", "name": "glm-5.2", "url": "http://old/v1", "favorite": True},
        ]
        new_entries = [
            {"id": "glm-5.2", "name": "glm-5.2", "url": "http://new/v1", "apiKey": "sk-new"},
        ]
        merged, replaced, added = _incremental_merge_models(existing, new_entries)
        self.assertEqual(replaced, 1)
        self.assertEqual(added, 0)
        self.assertEqual(len(merged), 1)
        entry = merged[0]
        self.assertEqual(entry["url"], "http://new/v1")     # 新字段覆盖
        self.assertEqual(entry["apiKey"], "sk-new")          # 新字段写入
        self.assertTrue(entry["favorite"])                  # 旧字段保留

    def test_append_when_not_exist(self):
        """不存在 → 追加到末尾。"""
        existing = [{"id": "a", "name": "a"}]
        new_entries = [{"id": "b", "name": "b"}]
        merged, replaced, added = _incremental_merge_models(existing, new_entries)
        self.assertEqual(replaced, 0)
        self.assertEqual(added, 1)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[1], {"id": "b", "name": "b"})

    def test_preserves_unselected_models(self):
        """未选中的模型（不在 new_entries 中）原样保留。"""
        existing = [
            {"id": "glm-5.2", "name": "glm-5.2"},
            {"id": "user-custom", "name": "我的自定义模型", "url": "http://x"},
        ]
        new_entries = [{"id": "glm-5.2", "name": "glm-5.2", "apiKey": "sk"}]
        merged, replaced, added = _incremental_merge_models(existing, new_entries)
        self.assertEqual(replaced, 1)
        self.assertEqual(added, 0)
        self.assertEqual(len(merged), 2)
        # 自定义模型完整保留
        custom = [m for m in merged if m["id"] == "user-custom"]
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["name"], "我的自定义模型")
        self.assertEqual(custom[0]["url"], "http://x")

    def test_same_id_different_name_appends(self):
        """id 相同但 name 不同 → 视为不同模型，追加而非替换（匹配规则：id AND name）。"""
        existing = [{"id": "glm-5.2", "name": "glm-5.2"}]
        new_entries = [{"id": "glm-5.2", "name": "GLM-5.2"}]
        merged, replaced, added = _incremental_merge_models(existing, new_entries)
        self.assertEqual(replaced, 0)
        self.assertEqual(added, 1)
        self.assertEqual(len(merged), 2)

    def test_empty_existing_all_appended(self):
        """现有为空时全部追加。"""
        new_entries = [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}]
        merged, replaced, added = _incremental_merge_models([], new_entries)
        self.assertEqual(replaced, 0)
        self.assertEqual(added, 2)
        self.assertEqual(len(merged), 2)


class TestWriteModelsJson(unittest.TestCase):
    """models.json 两种格式写入。"""

    def test_write_array_format(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "wb", "models.json")
            _write_models_json(p, [{"id": "a"}], wrapper="array")
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIsInstance(data, list)
            self.assertEqual(data, [{"id": "a"}])

    def test_write_object_format(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cb", "models.json")
            _write_models_json(p, [{"id": "a"}], wrapper="object")
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIsInstance(data, dict)
            self.assertEqual(data, {"models": [{"id": "a"}]})


class TestBuildModelEntries(unittest.TestCase):
    """构建条目字段正确性（以类对象作为 self 调用，访问类属性）。"""

    def test_workbuddy_entries(self):
        selected = ["hy3", "kimi-k2.7-code", "glm-5.2"]
        entries = ApiProxyPage._build_model_entries(
            ApiProxyPage, selected, "http://127.0.0.1:8080/v1", "sk-test",
            include_custom_protocol=True,
        )
        self.assertEqual(len(entries), 3)
        by_id = {e["id"]: e for e in entries}
        # 显示名按截图大小写
        self.assertEqual(by_id["hy3"]["name"], "Hy3")
        self.assertEqual(by_id["kimi-k2.7-code"]["name"], "Kimi-K2.7-Code")
        self.assertEqual(by_id["glm-5.2"]["name"], "glm-5.2")
        # 全部支持图片
        for e in entries:
            self.assertTrue(e["supportsImages"], f"{e['id']} supportsImages 应为 True")
            self.assertFalse(e["disabledMultimodal"])
            self.assertIn("image", e["input"])
        # WorkBuddy 需要 useCustomProtocol
        for e in entries:
            self.assertIn("useCustomProtocol", e)
            self.assertFalse(e["useCustomProtocol"])
        # 上下文字段写入
        self.assertEqual(by_id["hy3"]["maxInputTokens"], MODEL_CONTEXT_LENGTHS["hy3"])

    def test_codebuddy_entries_no_custom_protocol(self):
        entries = ApiProxyPage._build_model_entries(
            ApiProxyPage, ["glm-5.1"], "http://127.0.0.1:8080/v1", "sk-test",
            include_custom_protocol=False,
        )
        self.assertEqual(len(entries), 1)
        self.assertNotIn("useCustomProtocol", entries[0])
        self.assertTrue(entries[0]["supportsImages"])


class TestEndToEndIncrementalConfig(unittest.TestCase):
    """端到端：模拟一键配置的增量写入流程。"""

    def _simulate_config(self, wrapper, selected, work_dir):
        """模拟一次配置：读取现有 → 合并 → 写回，返回最终文件路径。"""
        target = os.path.join(work_dir, "models.json")
        base_url = "http://127.0.0.1:8080/v1"
        # 预置现有配置（含一个会被替换的模型 + 一个用户自定义模型）
        existing = [
            {"id": "glm-5.2", "name": "glm-5.2", "url": "http://old/v1", "note": "keep"},
            {"id": "my-model", "name": "我的模型", "url": "http://x"},
        ]
        _write_models_json(target, existing, wrapper=wrapper)
        # 构建新条目并增量合并
        entries = ApiProxyPage._build_model_entries(
            ApiProxyPage, selected, base_url, "sk-new", include_custom_protocol=(wrapper == "array")
        )
        current = _read_existing_models(target)
        merged, replaced, added = _incremental_merge_models(current, entries)
        _write_models_json(target, merged, wrapper=wrapper)
        return target, replaced, added, len(merged)

    def test_workbuddy_end_to_end(self):
        work_dir = tempfile.mkdtemp()
        try:
            target, replaced, added, total = self._simulate_config("array", ["glm-5.2", "hy3"], work_dir)
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIsInstance(data, list)  # WorkBuddy 裸数组
            ids = {m["id"] for m in data}
            self.assertIn("glm-5.2", ids)      # 已存在 → 替换
            self.assertIn("hy3", ids)          # 新增 → 追加
            self.assertIn("my-model", ids)     # 未选中 → 保留
            self.assertEqual(replaced, 1)
            self.assertEqual(added, 1)
            # 替换后旧字段保留 + 新字段覆盖
            g = [m for m in data if m["id"] == "glm-5.2"][0]
            self.assertEqual(g["url"], "http://127.0.0.1:8080/v1")
            self.assertEqual(g["note"], "keep")
            self.assertTrue(g["supportsImages"])
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_codebuddy_end_to_end(self):
        work_dir = tempfile.mkdtemp()
        try:
            target, replaced, added, total = self._simulate_config("object", ["kimi-k2.7-code"], work_dir)
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIsInstance(data, dict)  # CodeBuddy 包裹对象
            models = data["models"]
            ids = {m["id"] for m in models}
            self.assertIn("kimi-k2.7-code", ids)
            self.assertIn("glm-5.2", ids)      # 未选中 → 保留
            self.assertIn("my-model", ids)     # 未选中 → 保留
            self.assertEqual(replaced, 0)
            self.assertEqual(added, 1)
            k = [m for m in models if m["id"] == "kimi-k2.7-code"][0]
            self.assertEqual(k["name"], "Kimi-K2.7-Code")
            self.assertTrue(k["supportsImages"])
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
