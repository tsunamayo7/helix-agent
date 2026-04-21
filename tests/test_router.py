"""Tests for model router and capability detection."""


from src.router import (
    Capability,
    ModelInfo,
    _detect_capabilities,
    _infer_capability,
)


class TestDetectCapabilities:
    def test_code_model(self):
        caps = _detect_capabilities("qwen-coder:7b")
        assert Capability.CODE in caps

    def test_vision_model_mistral(self):
        caps = _detect_capabilities("mistral-small3.2:latest")
        assert Capability.VISION in caps

    def test_vision_model_gemma3(self):
        caps = _detect_capabilities("gemma3:27b")
        assert Capability.VISION in caps

    def test_vision_model_moondream(self):
        caps = _detect_capabilities("moondream:latest")
        assert Capability.VISION in caps

    def test_embedding_model(self):
        caps = _detect_capabilities("qwen3-embedding:8b")
        assert Capability.EMBEDDING in caps

    def test_embedding_nomic(self):
        caps = _detect_capabilities("nomic-embed-text:latest")
        assert Capability.EMBEDDING in caps

    def test_reasoning_model(self):
        caps = _detect_capabilities("qwen3.5:122b")
        assert Capability.REASONING in caps

    def test_reasoning_nemotron(self):
        caps = _detect_capabilities("nemotron-cascade-2:latest")
        assert Capability.REASONING in caps

    def test_unknown_model_gets_general(self):
        caps = _detect_capabilities("some-random-model:latest")
        assert Capability.GENERAL in caps

    def test_multiple_capabilities(self):
        caps = _detect_capabilities("gemma3:4b")
        # gemma3 matches both vision and creative
        assert len(caps) >= 2

    def test_deepseek_coder(self):
        caps = _detect_capabilities("deepseek-coder-v2:latest")
        assert Capability.CODE in caps

    def test_llava(self):
        caps = _detect_capabilities("llava:13b")
        assert Capability.VISION in caps


class TestInferCapability:
    def test_code_task_english(self):
        assert _infer_capability("Write a function to sort an array") == Capability.CODE

    def test_code_task_japanese(self):
        assert _infer_capability("このバグを修正して") == Capability.CODE

    def test_vision_task(self):
        assert _infer_capability("Analyze this screenshot") == Capability.VISION

    def test_vision_task_japanese(self):
        assert _infer_capability("この画像のテキストを抽出して") == Capability.VISION

    def test_embedding_task(self):
        assert _infer_capability("Generate embeddings for this text") == Capability.EMBEDDING

    def test_creative_task(self):
        assert _infer_capability("Write a story about a robot") == Capability.CREATIVE

    def test_creative_task_japanese(self):
        assert _infer_capability("ブレストしてアイデアを出して") == Capability.CREATIVE

    def test_reasoning_fallback(self):
        assert _infer_capability("Explain quantum computing") == Capability.REASONING

    def test_debug_task(self):
        assert _infer_capability("Debug this error in the logs") == Capability.CODE

    def test_ocr_task(self):
        assert _infer_capability("OCR this document") == Capability.VISION

    def test_refactor_task(self):
        assert _infer_capability("Refactor this class") == Capability.CODE

    def test_search_task(self):
        assert _infer_capability("Semantic search for relevant docs") == Capability.EMBEDDING


class TestModelInfo:
    def test_size_gb_conversion(self):
        info = ModelInfo(name="test", size_bytes=5 * 1024**3)
        assert info.size_gb == 5.0

    def test_size_gb_zero(self):
        info = ModelInfo(name="test", size_bytes=0)
        assert info.size_gb == 0.0

    def test_param_billions_from_b(self):
        info = ModelInfo(name="test", parameter_size="27.4B")
        assert info.param_billions == 27.4

    def test_param_billions_from_m(self):
        info = ModelInfo(name="test", parameter_size="566.70M")
        assert abs(info.param_billions - 0.5667) < 0.001

    def test_param_billions_empty(self):
        info = ModelInfo(name="test", parameter_size="")
        assert info.param_billions == 0.0

    def test_context_length_default(self):
        info = ModelInfo(name="test")
        assert info.context_length == 0

    def test_devstral_is_code(self):
        caps = _detect_capabilities("devstral-2:123b")
        assert Capability.CODE in caps
