"""本地 API 中转代理服务

功能：
- 管理上游 Key 池（主 Key）
- 子 API Key 管理与鉴权
- 请求路由（顺序耗尽策略：一个 Key 用完再切下一个）
- 支持 OpenAI 兼容接口转发（/v1/chat/completions, /v1/models 等）
- 上游代理默认 https://copilot.tencent.com/v2（加密隐藏，用户不可见）
- 限流、自动切换耗尽 Key
"""

import base64
import copy
import hashlib
import json
import logging
import random
import re
import secrets
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional
from urllib.parse import urlparse

import requests

from ..utils.store import load_setting

logger = logging.getLogger(__name__)

# ─── 上游代理地址（加密隐藏，用户在UI上看不到）───
_OBFUSCATED_PROXY = base64.b64encode(
    b"aHR0cHM6Ly9jb3BpbG90LnRlbmNlbnQuY29tL3Yy"
).decode()  # 双重编码，增加隐蔽性


def _get_default_upstream_proxy() -> str:
    """获取默认上游代理地址（解密还原）"""
    return base64.b64decode(
        base64.b64decode(_OBFUSCATED_PROXY)
    ).decode("utf-8")


# ─── 支持的模型列表（已实测验证可用）───
SUPPORTED_MODELS = [
    "auto",
    # DeepSeek 系列
    "deepseek-v4-pro", "deepseek-v4-flash",
    "deepseek-v3-2-volc", "deepseek-v3-1", "deepseek-v3-0324",
    "deepseek-r1",
    # GLM 系列
    "glm-5.2", "glm-5.1", "glm-5.0", "glm-5.0-turbo", "glm-5v-turbo",
    "glm-4.7", "glm-4.6",
    # MiniMax
    "minimax-m3", "minimax-m2.7", "minimax-m2.5",
    # Kimi
    "kimi-k2.6", "kimi-k2.5", "kimi-k2.7",
    # 混元
    "hy3", "hy3-preview", "hunyuan-chat", "hunyuan-2.0-thinking",
]

# 模型上下文长度（maxInputTokens），用于 WorkBuddy 客户端判断是否需要压缩上下文
# 如果 /v1/models 不返回此字段，WorkBuddy 无法知道模型上下文限制，不会触发自动压缩
MODEL_ID_ALIASES = {
    "kimi-k2.7-code": "kimi-k2.7",
}

MODEL_CONTEXT_LENGTHS = {
    "auto": 168000,                    # 官方虚拟模型，服务端动态选模型；与 dist 一致
    # DeepSeek 系列
    "deepseek-v4-pro": 1000000,        # 1M 上下文
    "deepseek-v4-flash": 1000000,      # 1M 上下文
    "deepseek-v3-2-volc": 128000,      # 128K
    "deepseek-v3-1": 128000,           # 128K
    "deepseek-v3-0324": 128000,        # 128K
    "deepseek-r1": 128000,             # 128K
    # GLM 系列
    "glm-5.2": 1000000,                # 1M 上下文
    "glm-5.1": 200000,                 # 200K
    "glm-5.0": 128000,                 # 128K
    "glm-5.0-turbo": 200000,           # 200K
    "glm-5v-turbo": 200000,            # 200K（多模态版，同 5.0-turbo）
    "glm-4.7": 200000,                 # 200K
    "glm-4.6": 200000,                 # 200K
    # MiniMax
    "minimax-m3": 1000000,             # 1M 上下文
    "minimax-m2.7": 200000,            # 200K
    "minimax-m2.5": 200000,            # 200K
    # Kimi
    "kimi-k2.6": 256000,               # 256K
    "kimi-k2.5": 1000000,              # 百万级上下文
    "kimi-k2.7": 256000,               # 256K
    # 混元
    "hy3": 256000,                     # 256K
    "hy3-preview": 256000,             # 256K
    "hunyuan-chat": 256000,            # 256K（混元 2.0 Instruct）
    "hunyuan-2.0-thinking": 256000,    # 256K（混元 2.0 Think）
}

MODEL_SUPPORTS_IMAGES = {
    "auto": True,
    # GLM 系列：全部支持图片输入
    "glm-5.2": True,
    "glm-5.1": True,
    "glm-5.0": True,
    "glm-5.0-turbo": True,
    "glm-5v-turbo": True,
    "glm-4.7": True,
    "glm-4.6": True,
    # 新增模型
    "kimi-k2.7": True,
    "hy3": True,
}

MODEL_MAX_OUTPUT_TOKENS = {
    "auto": 131072,
    "glm-5.2": 131072,
    "glm-5.1": 131072,
    "glm-5.0": 131072,
    "glm-5.0-turbo": 65536,
    "glm-5v-turbo": 8192,
    "glm-4.7": 131072,
    "glm-4.6": 131072,
    # 新增模型
    "kimi-k2.7": 131072,
    "hy3": 131072,
}

IMAGE_UNSUPPORTED_TEXT_MODELS = {m for m, v in MODEL_SUPPORTS_IMAGES.items() if not v}

def _model_supports_images(model: str) -> bool:
    return MODEL_SUPPORTS_IMAGES.get(model, True)


def _model_context_fields(model: str) -> dict:
    context_tokens = MODEL_CONTEXT_LENGTHS.get(model, 128000)
    max_output_tokens = min(MODEL_MAX_OUTPUT_TOKENS.get(model, 131072), context_tokens)
    return {
        "maxInputTokens": context_tokens,
        "max_input_tokens": context_tokens,
        "maxOutputTokens": max_output_tokens,
        "max_output_tokens": max_output_tokens,
        "maxTokens": max_output_tokens,
        "context_length": context_tokens,
        "contextLength": context_tokens,
        "contextWindow": context_tokens,
        "maxContextTokens": context_tokens,
        "context_window": context_tokens,
        "max_context_window": context_tokens,
        "maxAllowedSize": context_tokens,
        "max_allowed_size": context_tokens,
    }


def _image_url_from_part(part: dict):
    if not isinstance(part, dict):
        return None
    part_type = part.get("type")
    if part_type == "image_url":
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            return image_url.get("url")
        if isinstance(image_url, str):
            return image_url
    if part_type in ("image", "input_image"):
        source = part.get("source")
        if isinstance(source, dict):
            source_type = source.get("type")
            if source_type == "url" and isinstance(source.get("url"), str):
                return source["url"]
            if source_type == "base64" and isinstance(source.get("data"), str):
                media_type = source.get("media_type") or source.get("mediaType") or "image/jpeg"
                data = source["data"]
                return data if data.startswith("data:") else f"data:{media_type};base64,{data}"
        url = part.get("url")
        if isinstance(url, str):
            return url
        uri = part.get("uri")
        if isinstance(uri, str):
            return uri
        image = part.get("image")
        if isinstance(image, dict):
            url = image.get("url")
            if isinstance(url, str):
                return url
            uri = image.get("uri")
            if isinstance(uri, str):
                return uri
            data = image.get("data")
            media_type = image.get("mediaType") or image.get("media_type") or "image/jpeg"
            if isinstance(data, str):
                return data if data.startswith("data:") else f"data:{media_type};base64,{data}"
        if isinstance(image, str):
            return image
        data = part.get("data")
        media_type = part.get("mediaType") or part.get("media_type") or "image/jpeg"
        if isinstance(data, str):
            return data if data.startswith("data:") else f"data:{media_type};base64,{data}"
    return None


def _detect_multimodal_images(request_data: dict) -> dict:
    image_count = 0
    data_uri_count = 0
    max_image_chars = 0
    for msg in request_data.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            image_url = _image_url_from_part(part)
            if image_url:
                image_count += 1
                max_image_chars = max(max_image_chars, len(image_url))
                if image_url.startswith("data:"):
                    data_uri_count += 1
    return {
        "image_count": image_count,
        "data_uri_count": data_uri_count,
        "max_image_chars": max_image_chars,
    }


def _truncate_for_log(value: str, limit: int = 1200) -> str:
    if not isinstance(value, str):
        value = str(value)
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...(truncated {len(value) - limit} chars)"


def _safe_json_for_log(value, limit: int = 8000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return _truncate_for_log(text, limit)


def _summarize_log_value(value, depth: int = 0):
    """Return a compact, log-safe summary without leaking huge image payloads."""
    if depth >= 4:
        return f"<{type(value).__name__}>"
    if isinstance(value, str):
        if value.startswith("data:image/"):
            media = value.split(";", 1)[0].replace("data:", "")
            return f"<data-uri {media}, chars={len(value)}>"
        if len(value) > 300:
            return f"{value[:300]}...(chars={len(value)})"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        items = [_summarize_log_value(item, depth + 1) for item in value[:20]]
        if len(value) > 20:
            items.append(f"...({len(value) - 20} more)")
        return items
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:40]:
            if key.lower() in ("authorization", "api_key", "apikey", "access_token", "token"):
                result[key] = "<redacted>"
            else:
                result[key] = _summarize_log_value(item, depth + 1)
        if len(value) > 40:
            result["..."] = f"{len(value) - 40} more keys"
        return result
    return str(value)


def _summarize_messages_for_log(messages: list) -> dict:
    if not isinstance(messages, list):
        return {"type": type(messages).__name__}

    def summarize_part(part):
        if isinstance(part, str):
            return {
                "type": "text",
                "chars": len(part),
                "preview": _truncate_for_log(part, 160),
            }
        if not isinstance(part, dict):
            return {"type": type(part).__name__, "value": _summarize_log_value(part)}

        part_type = part.get("type", "<missing>")
        summary = {"type": part_type}
        if part_type == "text":
            text = part.get("text", "")
            summary.update({"chars": len(text), "preview": _truncate_for_log(text, 160)})
            return summary

        image_url = _image_url_from_part(part)
        if image_url:
            summary.update({
                "image": True,
                "data_uri": image_url.startswith("data:"),
                "chars": len(image_url),
                "media": image_url.split(";", 1)[0].replace("data:", "") if image_url.startswith("data:") else "url",
            })
            return summary

        for key, value in part.items():
            if key != "type":
                summary[key] = _summarize_log_value(value)
        return summary

    total = len(messages)
    if total <= 12:
        selected = list(enumerate(messages))
    else:
        selected = list(enumerate(messages[:6])) + list(enumerate(messages[-6:], start=total - 6))

    summarized = []
    for idx, msg in selected:
        if not isinstance(msg, dict):
            summarized.append({"index": idx, "type": type(msg).__name__})
            continue
        content = msg.get("content")
        item = {
            "index": idx,
            "role": msg.get("role"),
            "content_type": type(content).__name__,
        }
        if isinstance(content, str):
            item.update({"chars": len(content), "preview": _truncate_for_log(content, 180)})
        elif isinstance(content, list):
            item["parts"] = [summarize_part(part) for part in content[:20]]
            if len(content) > 20:
                item["parts"].append({"type": "...", "more": len(content) - 20})
        else:
            item["content"] = _summarize_log_value(content)
        summarized.append(item)

    return {
        "count": total,
        "omitted_middle": max(0, total - len(selected)),
        "items": summarized,
    }


def _summarize_request_for_error_log(request_data: dict, headers: dict, build_meta: dict) -> dict:
    non_message_fields = {}
    for key, value in request_data.items():
        if key == "messages":
            continue
        if key == "tools" and isinstance(value, list):
            names = []
            for tool in value[:30]:
                if isinstance(tool, dict):
                    names.append(tool.get("function", {}).get("name") or tool.get("name") or "<unnamed>")
            non_message_fields[key] = {
                "count": len(value),
                "names": names,
                "omitted": max(0, len(value) - len(names)),
            }
            continue
        non_message_fields[key] = _summarize_log_value(value)
    header_summary = {}
    for key, value in headers.items():
        if key.lower() == "authorization":
            header_summary[key] = f"Bearer ...{value[-8:]}" if isinstance(value, str) and len(value) > 15 else "<redacted>"
        else:
            header_summary[key] = value
    return {
        "headers": header_summary,
        "body_fields": list(request_data.keys()),
        "image_stats": _detect_multimodal_images(request_data),
        "messages": _summarize_messages_for_log(request_data.get("messages", [])),
        "non_message_fields": non_message_fields,
        "relay_meta": build_meta,
    }


def _parse_upstream_error_for_log(status_code: int, resp_body: str, resp_headers=None) -> dict:
    detail = {
        "status": status_code,
        "body_len": len(resp_body or ""),
        "raw_body": _truncate_for_log(resp_body or "", 4000),
    }
    if resp_headers:
        detail["content_type"] = resp_headers.get("Content-Type", "")
        detail["request_id_header"] = (
            resp_headers.get("X-Request-ID")
            or resp_headers.get("X-Request-Id")
            or resp_headers.get("Request-Id")
        )
    try:
        parsed = json.loads(resp_body) if resp_body else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        detail["json"] = _summarize_log_value(parsed)
        detail["code"] = parsed.get("code") or parsed.get("error", {}).get("code")
        detail["msg"] = parsed.get("msg") or parsed.get("message") or parsed.get("error", {}).get("message")
        detail["requestId"] = parsed.get("requestId") or parsed.get("request_id")
        detail["extError"] = parsed.get("extError") or parsed.get("error")
    return detail


# [v1.6.1-fix] 以下三个函数已无调用方（旧图片拦截逻辑移除后成为死代码）
# [ROLLBACK] 恢复旧图片拦截逻辑时，取消注释这三个函数
# def _latest_user_message_index(messages: list) -> int:
#     for idx in range(len(messages) - 1, -1, -1):
#         msg = messages[idx]
#         if isinstance(msg, dict) and msg.get("role") == "user":
#             return idx
#     return -1
#
#
# def _message_has_image(msg: dict) -> bool:
#     if not isinstance(msg, dict):
#         return False
#     content = msg.get("content")
#     if not isinstance(content, list):
#         return False
#     return any(_image_url_from_part(part) for part in content)
#
#
# def _strip_historical_images_for_text_model(request_data: dict, latest_user_idx: int) -> dict:
#     stripped = 0
#     for idx, msg in enumerate(request_data.get("messages", [])):
#         if idx == latest_user_idx or not isinstance(msg, dict):
#             continue
#         content = msg.get("content")
#         if not isinstance(content, list):
#             continue
#         normalized = []
#         changed = False
#         for part in content:
#             if _image_url_from_part(part):
#                 stripped += 1
#                 changed = True
#                 normalized.append({"type": "text", "text": "[历史图片已省略]"})
#             else:
#                 normalized.append(part)
#         if changed:
#             msg["content"] = normalized
#     return {"stripped_images": stripped}


def _normalize_messages_for_upstream(messages: list) -> int:
    """
    [v1.6.1新增] 将 messages 中的图片 content part 归一化为标准 OpenAI image_url 格式。

    原因：上游 copilot.tencent.com/v2 在纯 API 模式下只认 image_url 格式，
    WorkBuddy 客户端发来的 input_image / image 格式需要转换。

    转换规则：
      - image_url                        → 保持不变
      - input_image/image (data+mediaType) → image_url + data URI
      - input_image/image (url 字符串)     → image_url
      - input_image/image (blob_ref 对象)  → 保持原样透传（不替换为文本）
      - image_blob_ref (顶层)              → 保持原样透传（不替换为文本）
      - 其他 type                         → 保持不变

    返回值：归一化成功的图片 part 数量（用于日志统计）。

    [ROLLBACK] 注释掉调用处（搜索 _normalize_messages_for_upstream）即可恢复原样透传。
    """
    normalized_count = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_content = []
        for part in content:
            if not isinstance(part, dict):
                new_content.append(part)
                continue
            part_type = part.get("type")
            if part_type == "image_url":
                # 已经是标准 OpenAI 格式，保持不变
                new_content.append(part)
            elif part_type in ("input_image", "image"):
                url = _image_url_from_part(part)
                if url and (url.startswith("http://") or url.startswith("https://")):
                    new_content.append({"type": "image_url", "image_url": {"url": url}})
                    normalized_count += 1
                else:
                    new_content.append(part)
            elif part_type == "image_blob_ref":
                # 顶层 image_blob_ref，保持原样透传
                new_content.append(part)
            else:
                new_content.append(part)
        msg["content"] = new_content
    return normalized_count


def _remove_unsupported_inline_images(messages: list) -> int:
    """Remove image payload formats that copilot.tencent.com/v2 rejects."""
    if not isinstance(messages, list):
        return 0

    removed_count = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        new_content = []
        removed_in_message = 0
        for part in content:
            if not isinstance(part, dict) or not _part_is_image(part):
                new_content.append(part)
                continue

            url = _image_url_from_part(part)
            if (
                part.get("type") == "image_url"
                and isinstance(url, str)
                and (url.startswith("http://") or url.startswith("https://"))
            ):
                new_content.append(part)
                continue

            removed_count += 1
            removed_in_message += 1

        if removed_in_message:
            new_content.append({
                "type": "text",
                "text": (
                    "[Image omitted by proxy: upstream chat API rejects inline/base64 "
                    "image payloads. Please use a public http/https image URL.]"
                ),
            })
        msg["content"] = new_content

    if removed_count:
        logger.warning(
            "[image_sanitize] removed %s unsupported inline/base64 image part(s)",
            removed_count,
        )
    return removed_count


def _extract_message_text(content) -> str:
    """从 message content 中提取纯文本"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("content") and isinstance(part.get("content"), str):
                    parts.append(part["content"])
        return "\n".join(parts)
    return ""


def _find_following_assistant_description(messages: list, user_msg_index: int, max_chars: int = 1200) -> str:
    """
    找这条图片 user 消息之后最近的一条 assistant 回复，作为历史图片描述。
    """
    for i in range(user_msg_index + 1, len(messages)):
        msg = messages[i]
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        text = _extract_message_text(msg.get("content"))
        if text:
            text = text.strip()
            if len(text) > max_chars:
                text = text[:max_chars] + "...[已截断]"
            return text
    return "这是一张用户在前文上传过的图片，模型已经在后续回复中看过并分析过；原始图片数据已省略。"


def _part_is_image(part: dict) -> bool:
    """判断 content part 是否是图片"""
    if not isinstance(part, dict):
        return False
    part_type = part.get("type", "")
    if part_type in ("image_url", "input_image", "image", "image_blob_ref"):
        return True
    if "image_url" in part:
        return True
    if part_type in ("image", "input_image") and ("url" in part or "data" in part):
        return True
    return False


def _has_following_assistant(messages: list, msg_index: int) -> bool:
    """判断某条消息后面是否已经有 assistant 回复。"""
    for later_msg in messages[msg_index + 1:]:
        if isinstance(later_msg, dict) and later_msg.get("role") == "assistant":
            return True
    return False


def _message_has_image(msg: dict) -> bool:
    """Return True when a message content contains any image part."""
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(_part_is_image(part) for part in content)


def _strip_history_images_with_description(messages: list) -> list:
    """
    [v1.6.1新增] 历史图片替换成文本描述。

    策略：
    - 保留最后一条带图片的 user 消息，作为当前图片请求
    - 更早的图片替换成描述，避免历史 base64 反复进入上下文
    - 描述来源：该图片所在 user 消息之后最近的一条 assistant 回复文本

    [ROLLBACK] 注释掉调用处（搜索 _strip_history_images_with_description）即可恢复。
    """
    if not isinstance(messages, list):
        return messages

    latest_user_image_index = None
    for idx, item in enumerate(messages):
        if isinstance(item, dict) and item.get("role") == "user" and _message_has_image(item):
            latest_user_image_index = idx

    new_messages = []
    stripped_total = 0

    for msg_index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            new_messages.append(msg)
            continue

        content = msg.get("content")

        # WorkBuddy 可能在同一轮请求里把 assistant/tool 中间状态放在当前 user 图片之后。
        # 所以不能用“后面有没有 assistant”判断历史图，只保留最后一条 user 图片消息。
        allow_images = msg.get("role") == "user" and msg_index == latest_user_image_index

        if not isinstance(content, list):
            new_messages.append(msg)
            continue

        image_count = 0
        new_content = []

        for part in content:
            if _part_is_image(part) and not allow_images:
                image_count += 1
                continue
            new_content.append(part)

        if image_count:
            stripped_total += image_count
            description = _find_following_assistant_description(messages, msg_index)
            new_content.append({
                "type": "text",
                "text": (
                    f"[历史图片描述，共 {image_count} 张：\n"
                    f"{description}\n"
                    f"]"
                )
            })

        new_msg = dict(msg)
        new_msg["content"] = new_content
        new_messages.append(new_msg)

    if stripped_total:
        logger.info(f"[历史图片描述] 替换 {stripped_total} 张历史图片为文本描述")

    return new_messages


def _build_workbuddy_relay_headers(api_key: str) -> dict:
    """Build the upstream headers for WorkBuddy API key relay."""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {api_key}",
    }


_SYSTEM_PROMPT_REPLACEMENT_CACHE = {
    "raw": "",
    "pattern": None,
    "values": {},
}


def _load_system_prompt_sensitive_replacements() -> list[dict]:
    try:
        enabled = load_setting("system_prompt_sensitive_enabled", "False")
        raw_value = load_setting("system_prompt_sensitive_replacements", "[]")
    except Exception as exc:
        logger.warning(f"[系统提示词敏感信息] 读取配置失败，已跳过替换: {exc}")
        return []

    if enabled != "True":
        return []

    try:
        pairs = json.loads(raw_value or "[]")
    except json.JSONDecodeError:
        logger.warning("[系统提示词敏感信息] 配置 JSON 解析失败，已跳过替换")
        return []

    if not isinstance(pairs, list):
        return []

    replacements = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        key = str(pair.get("key", "")).strip()
        if not key:
            continue
        replacements.append({"key": key, "value": str(pair.get("value", ""))})
    return replacements


def _compile_system_prompt_replacement_pattern(replacements: list[dict]):
    raw_key = json.dumps(replacements, ensure_ascii=False, sort_keys=True)
    if _SYSTEM_PROMPT_REPLACEMENT_CACHE["raw"] == raw_key:
        return (
            _SYSTEM_PROMPT_REPLACEMENT_CACHE["pattern"],
            _SYSTEM_PROMPT_REPLACEMENT_CACHE["values"],
        )

    values = {item["key"]: item["value"] for item in replacements}
    pattern = None
    if values:
        escaped_keys = [re.escape(key) for key in sorted(values, key=len, reverse=True)]
        pattern = re.compile("|".join(escaped_keys))

    _SYSTEM_PROMPT_REPLACEMENT_CACHE["raw"] = raw_key
    _SYSTEM_PROMPT_REPLACEMENT_CACHE["pattern"] = pattern
    _SYSTEM_PROMPT_REPLACEMENT_CACHE["values"] = values
    return pattern, values


def _replace_system_prompt_sensitive_words(messages: list) -> int:
    replacements = _load_system_prompt_sensitive_replacements()
    if not replacements or not isinstance(messages, list):
        return 0

    pattern, values = _compile_system_prompt_replacement_pattern(replacements)
    if not pattern:
        return 0

    replaced_count = 0

    def _replace_text(text: str) -> tuple[str, int]:
        count = 0

        def _replace_match(match):
            nonlocal count
            count += 1
            return values.get(match.group(0), "")

        return pattern.sub(_replace_match, text), count

    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue

        content = msg.get("content")
        if isinstance(content, str):
            new_content, count = _replace_text(content)
            if count:
                msg["content"] = new_content
                replaced_count += count
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                new_text, count = _replace_text(text)
                if count:
                    part["text"] = new_text
                    replaced_count += count

    return replaced_count


def _build_workbuddy_relay_body(client_body: dict) -> tuple[dict, dict]:
    """
    Build the upstream body for WorkBuddy key-pool relay.

    This proxy is not an SDK/Agent adapter. WorkBuddy already speaks most of the
    body shape its upstream expects, so the relay preserves normal generation and
    tool fields by default. Only image payload formats proven rejected by the
    upstream are removed from messages.
    """
    body = copy.deepcopy(client_body)
    body["model"] = body.get("model") or "auto"
    body["messages"] = copy.deepcopy(client_body.get("messages", []))
    sensitive_replaced = _replace_system_prompt_sensitive_words(body["messages"])
    body["stream"] = True

    dropped_fields = []
    image_stats = _detect_multimodal_images(body)

    translated_fields = []
    if "max_completion_tokens" in client_body and "max_tokens" not in client_body:
        body["max_tokens"] = client_body.get("max_completion_tokens")
        translated_fields.append("max_completion_tokens->max_tokens")

    removed_null_fields = []
    for field in list(body.keys()):
        if body[field] is None:
            removed_null_fields.append(field)
            del body[field]

    meta = {
        "mode": "workbuddy_relay",
        "dropped_fields": dropped_fields,
        "removed_null_fields": removed_null_fields,
        "translated_fields": translated_fields,
        "history_images_replaced": 0,
        "normalized_images": 0,
        "unsupported_inline_images_removed": 0,
        "system_prompt_sensitive_replaced": sensitive_replaced,
        "image_stats_before": image_stats,
        "image_stats_after": image_stats,
        "has_stream_options": "stream_options" in body,
    }
    if sensitive_replaced:
        logger.info(f"[系统提示词敏感信息] 已替换 {sensitive_replaced} 处敏感信息")
    return body, meta


RESPONSES_PASSTHROUGH_FIELDS = [
    "temperature", "top_p", "max_tokens", "stop", "tools", "tool_choice",
    "parallel_tool_calls", "stream_options", "reasoning_effort", "reasoning",
    "metadata", "user", "seed", "presence_penalty", "frequency_penalty",
]


def _responses_part_to_chat_part(part):
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return {"type": "text", "text": str(part)}
    part_type = part.get("type")
    if part_type in ("input_text", "output_text", "text"):
        return {"type": "text", "text": part.get("text", "")}
    if part_type in ("input_image", "image", "image_url"):
        image_url = part.get("image_url") or part.get("url")
        if isinstance(image_url, dict):
            image_url = image_url.get("url")
        if image_url:
            return {"type": "image_url", "image_url": {"url": image_url}}
        return {"type": "text", "text": "[Image omitted: missing image_url]"}
    if "text" in part:
        return {"type": "text", "text": str(part.get("text", ""))}
    return {"type": "text", "text": json.dumps(part, ensure_ascii=False)}


def _responses_content_to_chat_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        converted = [_responses_part_to_chat_part(part) for part in content]
        if all(p.get("type") == "text" for p in converted):
            return "\n".join(p.get("text", "") for p in converted if p.get("text"))
        return converted
    return str(content)


def _responses_input_item_to_chat_messages(item):
    if isinstance(item, str):
        return [{"role": "user", "content": item}]
    if not isinstance(item, dict):
        return [{"role": "user", "content": str(item)}]
    item_type = item.get("type")
    if item_type == "message" or "role" in item:
        role = item.get("role", "user")
        if role == "developer":
            role = "system"
        return [{"role": role, "content": _responses_content_to_chat_content(item.get("content", ""))}]
    if item_type == "function_call_output":
        msg = {"role": "tool", "content": item.get("output", "")}
        call_id = item.get("call_id") or item.get("id")
        if call_id:
            msg["tool_call_id"] = call_id
        return [msg]
    if item_type == "function_call":
        call_id = item.get("call_id") or item.get("id") or f"call_{secrets.token_hex(8)}"
        return [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "{}")},
            }],
        }]
    return [{"role": "user", "content": _responses_content_to_chat_content(item.get("content", item))}]


def _responses_to_chat_request(client_body: dict) -> tuple[dict, dict]:
    chat_body = {
        "model": client_body.get("model") or "auto",
        "stream": bool(client_body.get("stream", False)),
    }
    messages = []
    if client_body.get("instructions"):
        messages.append({"role": "system", "content": str(client_body.get("instructions"))})
    response_input = client_body.get("input", "")
    if isinstance(response_input, list):
        for item in response_input:
            messages.extend(_responses_input_item_to_chat_messages(item))
    else:
        messages.append({"role": "user", "content": str(response_input)})
    chat_body["messages"] = messages
    if "max_output_tokens" in client_body and "max_tokens" not in client_body:
        chat_body["max_tokens"] = client_body.get("max_output_tokens")
    for field_name in RESPONSES_PASSTHROUGH_FIELDS:
        if field_name in client_body and field_name not in chat_body:
            chat_body[field_name] = client_body[field_name]
    return chat_body, {"input_type": type(response_input).__name__, "message_count": len(messages)}


def _responses_usage_from_chat_usage(usage: dict) -> dict:
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    total_tokens = usage.get("total_tokens", input_tokens + output_tokens)
    result = dict(usage)
    result.setdefault("input_tokens", input_tokens)
    result.setdefault("output_tokens", output_tokens)
    result.setdefault("total_tokens", total_tokens)
    return result


def _build_responses_object(content: str, model: str, response_id: str = "",
                            usage: dict = None, reasoning: str = "") -> dict:
    response_id = response_id or f"resp_{secrets.token_hex(16)}"
    message_content = [{"type": "output_text", "text": content or "", "annotations": []}]
    if reasoning:
        message_content.insert(0, {"type": "reasoning_text", "text": reasoning})
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "background": False,
        "error": None,
        "incomplete_details": None,
        "model": model,
        "output": [{
            "id": f"msg_{secrets.token_hex(12)}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": message_content,
        }],
        "output_text": content or "",
        "usage": _responses_usage_from_chat_usage(usage or {}),
    }


def _sse_event(event_name: str, data: dict) -> bytes:
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


def _responses_stream_events_from_chat_chunk(chunk_str: str, response_id: str,
                                             text_parts: list = None) -> list[bytes]:
    events = []
    for raw_line in chunk_str.splitlines():
        line = raw_line.strip()
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            continue
        try:
            chunk_data = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        usage = chunk_data.get("usage")
        for choice in chunk_data.get("choices", []):
            delta = choice.get("delta", {})
            text_delta = delta.get("content")
            if text_delta:
                if text_parts is not None:
                    text_parts.append(text_delta)
                events.append(_sse_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "response_id": response_id,
                    "item_id": "msg_0",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text_delta,
                }))
            reasoning_delta = delta.get("reasoning_content")
            if reasoning_delta:
                events.append(_sse_event("response.reasoning_text.delta", {
                    "type": "response.reasoning_text.delta",
                    "response_id": response_id,
                    "item_id": "msg_0",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": reasoning_delta,
                }))
        if usage:
            events.append(_sse_event("response.usage.delta", {
                "type": "response.usage.delta",
                "response_id": response_id,
                "usage": _responses_usage_from_chat_usage(usage),
            }))
    return events


# 上游 API 路径（copilot.tencent.com/v2 使用 /chat/completions，不是 /v1/chat/completions）
UPSTREAM_CHAT_PATH = "/chat/completions"
UPSTREAM_MODELS_PATH = "/v1/models"
BILLING_QUERY_PATH = "/v2/billing/meter/get-user-resource"


@dataclass
class UpstreamKey:
    """上游主 Key"""
    key_id: str = ""
    api_key: str = ""        # 上游真实 API Key (sk-xxx 或 ck_xxx)
    label: str = ""          # 备注标签（如手机号）
    status: str = "active"   # active / exhausted / disabled / rate_limited / cooldown / abnormal / permanent_disabled
    used_count: int = 0      # 累计调用次数
    points: str = ""         # 积分余额
    points_updated_at: str = ""
    packages: list = field(default_factory=list)  # 积分组列表 [{cycle_remain, cycle_end, ...}]
    created_at: str = ""
    last_used_at: str = ""
    # 统计
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cached_tokens: int = 0
    total_credits: float = 0.0
    # 模型级冷却（优化项 #8/#10）：{model: expire_ts}
    model_cooldowns: dict = field(default_factory=dict)
    # 渐进退避计数器（优化项 #10）：成功后归零
    cooldown_count: int = 0
    # 上次健康检测时间戳（优化项 #17）
    last_health_check: float = 0.0
    # 自定义阈值
    min_credits_threshold: float = 0.0    # 最低积分阈值，低于此值自动禁用（0=不限制）
    auto_enable_threshold: float = 100.0  # 自动启用阈值，查分高于此值自动恢复 active


@dataclass
class SubApiKey:
    """子 API Key - 对外暴露的访问 Key"""
    key_id: str = ""
    api_key: str = ""             # 生成的子 Key (sk-xxx)
    label: str = ""              # 标签
    is_active: bool = True       # 是否启用
    allowed_models: list = field(default_factory=list)   # 允许的模型列表，空=全部
    allowed_key_ids: list = field(default_factory=list)  # 允许使用的上游 Key ID
    max_usage: int = 0           # 最大使用次数，0=无限
    used_count: int = 0          # 已使用次数
    rate_limit_rpm: int = 1000   # 每分钟最大请求数（默认1000）
    key_mode: int = 1            # 调用模式：1=专一模式（一个Key用完再换），2=临期优先（优先用最快过期的Key）
    created_at: str = ""
    # 统计
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cached_tokens: int = 0
    total_credits: float = 0.0


@dataclass
class RequestLog:
    """请求日志"""
    timestamp: float = 0
    sub_key_id: str = ""
    sub_key_label: str = ""
    main_key_id: str = ""
    main_key_label: str = ""
    model: str = ""
    event: str = ""        # start / end
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    error: str = ""


class ProxyDatabase:
    """代理服务数据存储 - JSON 文件持久化
    
    性能优化：
    - 延迟写入：数据变更后不立即写盘，由定时器批量刷盘
    - 内存优先：所有读操作直接从内存返回，不读文件
    - 单例模式：多线程共享同一实例，避免并发写冲突
    - 原子写入：先写临时文件再 rename，避免读到半截 JSON
    """

    _SAVE_INTERVAL = 5.0  # 秒，刷盘间隔
    _instance = None       # 单例实例
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, data_dir: str = "") -> "ProxyDatabase":
        """获取单例实例（线程安全）"""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(data_dir)
            return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（用于测试或强制重新加载）"""
        with cls._instance_lock:
            if cls._instance:
                cls._instance._flush_to_disk()
            cls._instance = None

    def __init__(self, data_dir: str = ""):
        import os
        if not data_dir:
            data_dir = os.path.expanduser("~/.antigravity-tools")
        os.makedirs(data_dir, exist_ok=True)
        self._db_path = os.path.join(data_dir, "proxy_db.json")
        self._lock = threading.RLock()  # 可重入锁，避免 _save() 被已持锁的方法调用时死锁
        self._data = self._load()
        self._dirty = False  # 是否有未保存的变更
        self._save_timer = None  # 延迟保存定时器
        self._key_status_version = 0  # Key 状态变更版本号，每次状态变化 +1，ProxyRouter 据此刷新缓存
        self._sub_key_version = 0  # 子 Key 变更版本号，每次增删改 +1，ProxyRouter 据此刷新认证缓存

    def _load(self) -> dict:
        """从文件加载数据（带重试，读取失败时重试而非返回空数据）"""
        import os
        if not os.path.exists(self._db_path):
            return {
                "upstream_keys": [],
                "sub_api_keys": [],
                "request_logs": [],
                "daily_stats": {},
                "settings": {"upstream_proxy": ""},
            }
        # 最多重试 3 次，应对并发写入导致的短暂读取失败
        for attempt in range(3):
            try:
                with open(self._db_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    if not content.strip():
                        # 文件为空（可能正在被原子写入），等一下重试
                        import time
                        time.sleep(0.2 * (attempt + 1))
                        continue
                    return json.loads(content)
            except (json.JSONDecodeError, ValueError) as e:
                # JSON 解析失败（可能读到半截文件），等一下重试
                logger.warning(f"[DB] proxy_db.json 读取失败(尝试 {attempt+1}/3): {e}")
                import time
                time.sleep(0.3 * (attempt + 1))
            except Exception as e:
                logger.error(f"[DB] proxy_db.json 读取异常: {e}")
                import time
                time.sleep(0.3 * (attempt + 1))
        # 重试 3 次都失败，说明文件确实损坏
        logger.error("[DB] proxy_db.json 读取失败3次，返回空数据（可能需要恢复备份）")
        return {
            "upstream_keys": [],
            "sub_api_keys": [],
            "request_logs": [],
            "daily_stats": {},
            "settings": {"upstream_proxy": ""},
        }

    def _save(self):
        """标记数据为脏，延迟保存（不立即写盘）"""
        self._dirty = True
        # 如果没有定时器，启动一个
        if self._save_timer is None or not self._save_timer.is_alive():
            self._save_timer = threading.Timer(self._SAVE_INTERVAL, self._flush_to_disk)
            self._save_timer.daemon = True
            self._save_timer.start()

    def _flush_to_disk(self):
        """实际写入磁盘（原子写入：先写临时文件，再 rename 覆盖）"""
        import os
        with self._lock:
            if not self._dirty:
                return
            try:
                tmp_path = self._db_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                # 原子 rename（Windows 上 os.replace 是原子的）
                os.replace(tmp_path, self._db_path)
                self._dirty = False
            except Exception as e:
                logger.error(f"保存代理数据失败: {e}")
                # 清理临时文件
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass

    def flush_now(self):
        """立即刷盘（用于程序退出前调用）"""
        self._flush_to_disk()

    # === 上游 Key 管理 ===

    def get_upstream_keys(self) -> list[dict]:
        with self._lock:
            return list(self._data.get("upstream_keys", []))

    def add_upstream_key(self, key_data: dict):
        with self._lock:
            self._data.setdefault("upstream_keys", []).append(key_data)
            self._key_status_version += 1
            self._dirty = True
            self._flush_to_disk()

    def update_upstream_key(self, key_id: str, updates: dict):
        with self._lock:
            keys = self._data.setdefault("upstream_keys", [])
            for k in keys:
                if k.get("key_id") == key_id:
                    k.update(updates)
                    if "status" in updates:
                        self._key_status_version += 1
                    break
            self._save()

    def delete_upstream_key(self, key_id: str):
        with self._lock:
            self._data["upstream_keys"] = [
                k for k in self._data.get("upstream_keys", [])
                if k.get("key_id") != key_id
            ]
            self._key_status_version += 1
            self._dirty = True
            self._flush_to_disk()

    def sync_quota_to_key(self, api_key_or_token: str, remaining_credits: float, total_credits: float,
                           packages: list = None):
        """同步积分查询结果到上游 Key（智能禁用/恢复）

        规则：
        - 积分为 0 → 立即禁用（disabled，不再参与轮询）
        - 积分 > 100 且之前是 disabled/exhausted → 恢复为 active
        - 0 < 积分 <= 100：保持当前状态
        - 同时更新 points 和 points_updated_at 字段
        - 如果提供了 packages，同时存储积分组信息（用于临期优先排序）

        Args:
            api_key_or_token: 账号的 API Key (ck_xxx) 或 auth_token（与 upstream key 的 api_key 对应）
            remaining_credits: 剩余积分
            total_credits: 总积分
            packages: 积分组列表（来自 ResourcePackage），格式为 [{cycle_remain, cycle_end, ...}]
        """
        with self._lock:
            keys = self._data.setdefault("upstream_keys", [])
            matched = False
            for k in keys:
                # 匹配 api_key 或 label（label 存的是手机号，api_key 存的是 ck_xxx）
                k_api_key = k.get("api_key", "")
                k_label = k.get("label", "")
                if k_api_key == api_key_or_token or k_label == api_key_or_token:
                    matched = True
                    old_status = k.get("status", "active")
                    # 更新积分
                    k["points"] = f"{remaining_credits:.0f}/{total_credits:.0f}"
                    k["points_updated_at"] = datetime.now().isoformat()

                    # 存储积分组信息（用于临期优先排序）
                    if packages is not None:
                        # 只保留临期优先排序所需的关键字段，避免存太多冗余
                        pkg_summaries = []
                        for pkg in packages:
                            if isinstance(pkg, dict):
                                pkg_summaries.append({
                                    "cycle_remain": pkg.get("cycle_remain", 0),
                                    "cycle_end": pkg.get("cycle_end", ""),
                                    "package_name": pkg.get("package_name", ""),
                                    "package_type": pkg.get("package_type", ""),
                                })
                            else:
                                # ResourcePackage 对象
                                try:
                                    pkg_summaries.append({
                                        "cycle_remain": pkg.cycle_remain,
                                        "cycle_end": pkg.cycle_end,
                                        "package_name": pkg.package_name,
                                        "package_type": pkg.package_type,
                                    })
                                except AttributeError:
                                    pass
                        k["packages"] = pkg_summaries

                    # 读取该 Key 的自定义阈值
                    min_threshold = float(k.get("min_credits_threshold", 0) or 0)
                    auto_enable = float(k.get("auto_enable_threshold", 100) or 100)

                    # 积分低于阈值 → 禁用（但不覆盖 abnormal 和 permanent_disabled 状态）
                    # ⚠️ 防御：如果 remaining=0 且 total=0，说明查分失败返回了空数据，
                    # 不能当成"积分用完"处理，否则会把正常 Key 全部误禁用
                    if remaining_credits <= 0 and total_credits <= 0:
                        logger.warning(f"[积分同步] Key {k.get('label', k.get('key_id',''))} "
                                      f"查分返回 remaining=0 total=0，疑似查分失败，跳过禁用（保持 {old_status}）")
                    elif remaining_credits <= min_threshold and total_credits > 0:
                        if old_status in ("active", "cooldown", "rate_limited", "exhausted"):
                            k["status"] = "disabled"
                            self._key_status_version += 1
                            logger.info(f"[积分同步] Key {k.get('label', k.get('key_id',''))} 积分{remaining_credits:.0f}<={min_threshold:.0f}，{old_status} -> DISABLED")
                    # 积分高于自动启用阈值 → 恢复 active（但不恢复 abnormal 和 permanent_disabled）
                    elif remaining_credits > auto_enable:
                        if old_status in ("disabled", "exhausted", "cooldown", "rate_limited"):
                            k["status"] = "active"
                            self._key_status_version += 1
                            logger.info(f"[积分同步] Key {k.get('label', k.get('key_id',''))} 积分{remaining_credits:.0f}>{auto_enable:.0f}，{old_status} -> ACTIVE")
                    # 0 < 积分 <= 100：保持当前状态
                    break
            if not matched:
                logger.warning(f"[积分同步] 未找到匹配的上游 Key: api_key_or_token={api_key_or_token[:30]}...")
            # 立即写盘（不用延迟写入，否则 quota_updated 信号触发 reload 时读到旧数据）
            self._dirty = True
            self._flush_to_disk()

    def deduct_key_points(self, key_id: str, credit_used: float):
        """实时扣除积分余额（本地估算，5分钟查分时用真实值修正）

        从 points 字段 "剩余/总量" 中减去本次消耗的 credit。
        如果 points 为空或格式不对，跳过（等查分修正）。
        """
        with self._lock:
            for k in self._data.get("upstream_keys", []):
                if k.get("key_id") != key_id:
                    continue
                points_str = k.get("points", "")
                if not points_str or "/" not in points_str:
                    break
                try:
                    remain_str, total_str = points_str.split("/")
                    remain = float(remain_str)
                    total = float(total_str)
                    new_remain = max(0, remain - credit_used)
                    k["points"] = f"{new_remain:.0f}/{total:.0f}"
                    # 延迟写入（不需要立即写盘，5分钟查分时会立即写）
                    self._save()
                except (ValueError, IndexError):
                    pass
                break

    def get_total_points_for_sub_key(self, allowed_key_ids: list = None) -> float:
        """计算子 Key 可调用的所有上游 Key 的剩余积分总和

        Args:
            allowed_key_ids: 子 Key 允许使用的上游 Key ID 列表，空=全部

        Returns:
            剩余积分总和
        """
        with self._lock:
            keys = self._data.get("upstream_keys", [])
            total = 0.0
            for k in keys:
                # 如果指定了 allowed_key_ids，只统计这些 key
                if allowed_key_ids and k.get("key_id") not in allowed_key_ids:
                    continue
                # 解析 points 字段（格式 "剩余/总量"）
                points_str = k.get("points", "")
                if points_str and "/" in points_str:
                    try:
                        remain = float(points_str.split("/")[0])
                        total += remain
                    except (ValueError, IndexError):
                        pass
            return total

    # === 子 Key 管理 ===

    def get_sub_api_keys(self) -> list[dict]:
        with self._lock:
            return list(self._data.get("sub_api_keys", []))

    def add_sub_api_key(self, key_data: dict):
        with self._lock:
            self._data.setdefault("sub_api_keys", []).append(key_data)
            self._sub_key_version += 1
            self._dirty = True
            self._flush_to_disk()

    def update_sub_api_key(self, key_id: str, updates: dict):
        with self._lock:
            keys = self._data.setdefault("sub_api_keys", [])
            for k in keys:
                if k.get("key_id") == key_id:
                    k.update(updates)
                    self._sub_key_version += 1
                    break
            self._save()

    def increment_upstream_key_stats(self, key_id: str, prompt_tokens: int = 0,
                                      completion_tokens: int = 0, total_tokens: int = 0,
                                      cached_tokens: int = 0, credits: float = 0.0):
        """原子递增上游 Key 统计计数器（在 DB 锁内读+写，避免并发丢数据）

        旧方式：从缓存读 used_count=5 → +1 → 写 6（并发请求都读到 5，全写 6，丢了）
        新方式：在锁内读当前值 → 递增 → 写回，保证原子性。
        积分不再实时扣减，改为请求完成后定时查分（见 _refresh_key_points）。
        """
        with self._lock:
            keys = self._data.setdefault("upstream_keys", [])
            for k in keys:
                if k.get("key_id") == key_id:
                    k["used_count"] = k.get("used_count", 0) + 1
                    k["last_used_at"] = datetime.now().isoformat()
                    if prompt_tokens:
                        k["total_prompt_tokens"] = k.get("total_prompt_tokens", 0) + prompt_tokens
                    if completion_tokens:
                        k["total_completion_tokens"] = k.get("total_completion_tokens", 0) + completion_tokens
                    if total_tokens:
                        k["total_tokens"] = k.get("total_tokens", 0) + total_tokens
                    if cached_tokens:
                        k["total_cached_tokens"] = k.get("total_cached_tokens", 0) + cached_tokens
                    if credits:
                        k["total_credits"] = round(k.get("total_credits", 0.0) + credits, 4)
                    # 临期积分递减：从最快过期的积分组扣除已消耗的积分
                    if credits and credits > 0:
                        pkgs = k.get("packages", [])
                        if pkgs:
                            remaining_to_deduct = credits
                            def _pkg_end_ts(p):
                                ce = str(p.get("cycle_end", ""))
                                try:
                                    from datetime import datetime as _dt
                                    if "T" in ce:
                                        return _dt.fromisoformat(ce.replace("Z", "+00:00")).timestamp()
                                    return _dt.strptime(ce, "%Y-%m-%d %H:%M:%S").timestamp()
                                except:
                                    return float('inf')
                            pkgs_sorted = sorted([p for p in pkgs if isinstance(p, dict) and float(p.get("cycle_remain", 0)) > 0],
                                                  key=_pkg_end_ts)
                            for p in pkgs_sorted:
                                if remaining_to_deduct <= 0:
                                    break
                                cur_remain = float(p.get("cycle_remain", 0))
                                if cur_remain <= 0:
                                    continue
                                deduct = min(cur_remain, remaining_to_deduct)
                                p["cycle_remain"] = round(cur_remain - deduct, 4)
                                remaining_to_deduct -= deduct
                                logger.debug(f"[临期递减] Key {k.get('label','')} 积分组 {p.get('package_name','')[:20]} 扣除 {deduct:.4f}, 剩余 {p['cycle_remain']:.4f}")
                            # 同步更新 points 字段（UI 显示用）
                            total_remain = sum(float(p.get("cycle_remain", 0)) for p in pkgs if isinstance(p, dict))
                            total_size = sum(float(p.get("cycle_size", 0)) for p in pkgs if isinstance(p, dict))
                            if total_size > 0:
                                k["points"] = f"{total_remain:.0f}/{total_size:.0f}"
                                k["points_updated_at"] = datetime.now().isoformat()
                            # 实时检查最低积分阈值，低于阈值自动禁用
                            min_threshold = float(k.get("min_credits_threshold", 0) or 0)
                            if min_threshold > 0 and total_remain <= min_threshold:
                                old_status = k.get("status", "active")
                                if old_status in ("active", "cooldown", "rate_limited", "exhausted"):
                                    k["status"] = "disabled"
                                    self._key_status_version += 1
                                    logger.info(f"[实时阈值] Key {k.get('label', k.get('key_id',''))} 积分{total_remain:.0f}<={min_threshold:.0f}，{old_status} -> DISABLED")
                    break
            # 更新每日统计
            self._update_daily_stats("upstream", key_id, prompt_tokens, completion_tokens, total_tokens, cached_tokens, credits)
            self._save()

    def _update_daily_stats(self, category: str, key_id: str, prompt_tokens: int = 0,
                            completion_tokens: int = 0, total_tokens: int = 0,
                            cached_tokens: int = 0, credits: float = 0.0):
        """更新每日统计（内部方法，需在锁内调用）"""
        today = datetime.now().strftime("%Y-%m-%d")
        daily = self._data.setdefault("daily_stats", {})
        cat = daily.setdefault(category, {})
        kid = cat.setdefault(key_id, {})
        day = kid.setdefault(today, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                                      "cached_tokens": 0, "credits": 0.0, "count": 0})
        day["prompt_tokens"] += prompt_tokens
        day["completion_tokens"] += completion_tokens
        day["total_tokens"] += total_tokens
        day["cached_tokens"] += cached_tokens
        day["credits"] = round(day["credits"] + credits, 4)
        day["count"] += 1

    def get_daily_stats(self, category: str, key_id: str) -> dict:
        """获取某个 Key 的每日统计 {date: {prompt_tokens, completion_tokens, total_tokens, cached_tokens, credits, count}}"""
        with self._lock:
            return dict(self._data.get("daily_stats", {}).get(category, {}).get(key_id, {}))

    def get_today_stats(self, category: str, key_id: str) -> dict:
        """获取今天的统计"""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            return dict(self._data.get("daily_stats", {}).get(category, {}).get(key_id, {}).get(today, {}))

    def get_usage_summary(self, days: int = None) -> dict:
        """获取使用情况汇总统计

        Args:
            days: None=总计（从 upstream_keys 的累计字段汇总）
                  1=今日
                  N=近N天

        Returns:
            {
                "prompt_tokens": int,       # 上行Token
                "completion_tokens": int,   # 下行Token
                "total_tokens": int,        # 总Token
                "cached_tokens": int,       # 缓存命中Token
                "credits": float,           # 消耗积分
                "count": int,               # 请求数量
                "cache_hit_rate": float,    # 缓存命中率(0~1)
            }
        """
        result = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "credits": 0.0,
            "count": 0,
            "cache_hit_rate": 0.0,
        }

        with self._lock:
            if days is None:
                # 总计：从 upstream_keys 累计字段汇总
                for k in self._data.get("upstream_keys", []):
                    result["prompt_tokens"] += k.get("total_prompt_tokens", 0)
                    result["completion_tokens"] += k.get("total_completion_tokens", 0)
                    result["total_tokens"] += k.get("total_tokens", 0)
                    result["cached_tokens"] += k.get("total_cached_tokens", 0)
                    result["credits"] += k.get("total_credits", 0.0)
                    result["count"] += k.get("used_count", 0)
            else:
                # 今日或近N天：从 daily_stats 汇总
                today = datetime.now()
                if days == 1:
                    date_list = [today.strftime("%Y-%m-%d")]
                else:
                    date_list = [
                        (today - timedelta(days=i)).strftime("%Y-%m-%d")
                        for i in range(days)
                    ]

                upstream_stats = self._data.get("daily_stats", {}).get("upstream", {})
                for key_id, dates in upstream_stats.items():
                    for date_str in date_list:
                        day_data = dates.get(date_str)
                        if day_data:
                            result["prompt_tokens"] += day_data.get("prompt_tokens", 0)
                            result["completion_tokens"] += day_data.get("completion_tokens", 0)
                            result["total_tokens"] += day_data.get("total_tokens", 0)
                            result["cached_tokens"] += day_data.get("cached_tokens", 0)
                            result["credits"] += day_data.get("credits", 0.0)
                            result["count"] += day_data.get("count", 0)

        # 计算缓存命中率
        if result["prompt_tokens"] > 0:
            result["cache_hit_rate"] = result["cached_tokens"] / result["prompt_tokens"]

        result["credits"] = round(result["credits"], 4)
        return result

    _points_query_timestamps: dict = {}  # {key_id: last_query_epoch}

    def refresh_key_points_if_needed(self, key_id: str):
        """请求完成后异步查分，限频 1 分钟/次。查到后调用 sync_quota_to_key 更新积分。"""
        import time as _time
        now = _time.time()
        last = ProxyDatabase._points_query_timestamps.get(key_id, 0)
        if now - last < 300:
            return  # 5 分钟内已查过，跳过

        ProxyDatabase._points_query_timestamps[key_id] = now

        # 找到该 Key 的 api_key
        with self._lock:
            api_key = None
            for k in self._data.get("upstream_keys", []):
                if k.get("key_id") == key_id:
                    api_key = k.get("api_key", "")
                    break
        if not api_key:
            return

        # 异步查分
        def _do_query():
            try:
                import requests
                url = f"https://copilot.tencent.com{BILLING_QUERY_PATH}"
                resp = requests.post(url, json={}, headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                }, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    # 兼容两种响应结构：
                    # 旧格式: {"accounts": [...]} (顶层)
                    # 新格式: {"data":{"Response":{"Data":{"Accounts": [...]}}}}
                    accounts = data.get("accounts", [])
                    if not accounts:
                        # 尝试新格式
                        try:
                            accounts = data["data"]["Response"]["Data"]["Accounts"]
                        except (KeyError, TypeError):
                            accounts = []
                    # 关键防护：accounts 为空说明上游返回了异常数据（维护/限流/格式变更），
                    # 不能当成 0 分处理，否则会把所有 Key 全部误禁用
                    if not accounts:
                        logger.warning(f"[自动查分] Key {key_id} 查分返回空 accounts，跳过更新（不误禁用）")
                        return
                    total_remain = 0.0
                    total_credits = 0.0
                    pkgs = []
                    for acc in accounts:
                        # 兼容新旧字段名
                        remain = float(acc.get("cycle_remain", acc.get("CycleCapacityRemain", acc.get("CapacityRemain", 0))))
                        total = float(acc.get("cycle_total", acc.get("CycleCapacitySize", acc.get("CapacitySize", 0))))
                        total_remain += remain
                        total_credits += total
                        pkgs.append({
                            "cycle_remain": remain,
                            "cycle_end": acc.get("cycle_end", acc.get("CycleEndTime", "")),
                            "package_name": acc.get("package_name", acc.get("PackageName", "")),
                            "package_type": acc.get("package_type", acc.get("PackageType", "")),
                        })
                    self.sync_quota_to_key(api_key, total_remain, total_credits, packages=pkgs)
                    logger.info(f"[自动查分] Key {key_id} 积分更新: {total_remain:.0f}/{total_credits:.0f}")
                else:
                    logger.warning(f"[自动查分] Key {key_id} 查分返回非200: status={resp.status_code}, body={resp.text[:200]}")
            except Exception as e:
                logger.debug(f"[自动查分] Key {key_id} 查分失败: {e}")

        import threading
        threading.Thread(target=_do_query, daemon=True).start()

    def increment_sub_api_key_stats(self, key_id: str, prompt_tokens: int = 0,
                                     completion_tokens: int = 0, total_tokens: int = 0,
                                     cached_tokens: int = 0, credits: float = 0.0):
        """原子递增子 API Key 统计计数器（同 increment_upstream_key_stats）"""
        with self._lock:
            keys = self._data.setdefault("sub_api_keys", [])
            for k in keys:
                if k.get("key_id") == key_id:
                    k["used_count"] = k.get("used_count", 0) + 1
                    if prompt_tokens:
                        k["total_prompt_tokens"] = k.get("total_prompt_tokens", 0) + prompt_tokens
                    if completion_tokens:
                        k["total_completion_tokens"] = k.get("total_completion_tokens", 0) + completion_tokens
                    if total_tokens:
                        k["total_tokens"] = k.get("total_tokens", 0) + total_tokens
                    if cached_tokens:
                        k["total_cached_tokens"] = k.get("total_cached_tokens", 0) + cached_tokens
                    if credits:
                        k["total_credits"] = round(k.get("total_credits", 0.0) + credits, 4)
                    break
            self._update_daily_stats("sub", key_id, prompt_tokens, completion_tokens, total_tokens, cached_tokens, credits)
            self._save()

    def delete_sub_api_key(self, key_id: str):
        with self._lock:
            before = len(self._data.get("sub_api_keys", []))
            self._data["sub_api_keys"] = [
                k for k in self._data.get("sub_api_keys", [])
                if k.get("key_id") != key_id
            ]
            if len(self._data["sub_api_keys"]) != before:
                self._sub_key_version += 1
            self._dirty = True
            self._flush_to_disk()

    # === 设置 ===

    def get_settings(self) -> dict:
        with self._lock:
            return dict(self._data.get("settings", {}))

    def update_settings(self, settings: dict):
        with self._lock:
            self._data.setdefault("settings", {}).update(settings)
            self._save()

    # === 请求日志 ===

    def add_request_log(self, log: dict):
        with self._lock:
            logs = self._data.setdefault("request_logs", [])
            logs.append(log)
            # 只保留最近1000条
            if len(logs) > 1000:
                self._data["request_logs"] = logs[-1000:]
            self._save()

    def get_request_logs(self, since: float = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            logs = self._data.get("request_logs", [])
            if since:
                logs = [l for l in logs if l.get("timestamp", 0) > since]
            return logs[-limit:]


class ProxyRouter:
    """代理路由器 - 选择上游 Key 并转发请求

    路由策略：顺序耗尽 — 优先使用第一个可用 Key，直到该 Key 耗尽（exhausted/disabled），
    再切换到下一个。
    
    性能优化：
    - 使用 requests.Session 连接池，复用TCP+TLS连接
    - 缓存 upstream URL，避免每次请求都读 DB 加锁
    """

    def __init__(self, db: ProxyDatabase):
        self._db = db
        self._concurrent_counts: dict[str, int] = {}  # key_id -> 并发数
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()  # 专门保护 select_key 缓存读写
        # 专一模式：记住每个子Key池当前专用的 Key（按 allowed_key_ids 的 hash 分组）
        self._dedicated_keys: dict[str, str] = {}  # {pool_hash: key_id}
        self._expiring_dedicated_keys: dict[str, str] = {}  # {pool_hash: key_id} for key_mode=2 same-expiry group
        # 轮询模式：记住每个子Key池的轮询索引
        self._round_robin_index: dict[str, int] = {}  # {pool_hash: index}
        # 连接池：每个上游域名一个 Session，复用 TCP+TLS 连接
        self._sessions: dict[str, requests.Session] = {}
        self._session_lock = threading.Lock()
        # 缓存 upstream URL，避免每次请求都拿 DB 锁
        self._cached_upstream_url: str = ""
        self._upstream_url_cache_time: float = 0
        self._UPSTREAM_URL_CACHE_TTL = 30.0  # 30秒缓存
        # 缓存上游 Key 列表，避免每次请求都拿 DB 锁
        self._cached_upstream_keys: list[dict] = []
        self._upstream_keys_cache_time: float = 0
        self._UPSTREAM_KEYS_CACHE_TTL = 10.0  # 10秒缓存
        self._last_key_status_version: int = -1  # 上次缓存加载时的 DB 版本号，-1 表示未加载过
        # 缓存子 Key 认证表（token -> sub_key dict），避免每次请求遍历+加锁
        self._cached_sub_keys: dict[str, dict] = {}
        self._sub_keys_cache_time: float = 0
        self._SUB_KEYS_CACHE_TTL = 10.0  # 10秒缓存
        self._last_sub_key_version: int = -1
        # 粘性会话（优化项 #5/#7）：{session_hash: (key_id, expire_ts)} TTL 1h
        self._sticky_sessions: dict[str, tuple] = {}
        # 模型级冷却（优化项 #8/#10）：{key_id: {model: expire_ts}}
        self._model_cooldowns: dict[str, dict] = {}
        # 渐进退避计数器（优化项 #10）：{key_id: count}
        self._cooldown_counts: dict[str, int] = {}
        # 上次健康检测时间戳（优化项 #17）：{key_id: ts}
        self._last_health_check: dict[str, float] = {}
        # 健康检测后台线程（优化项 #17）
        self._health_check_stop = threading.Event()
        self._health_check_thread: Optional[threading.Thread] = None

    def _get_session(self, base_url: str) -> requests.Session:
        """获取或创建到指定上游的 Session（连接池复用）"""
        domain = urlparse(base_url).netloc
        
        with self._session_lock:
            if domain not in self._sessions:
                session = requests.Session()
                # 配置连接池
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=100,
                    pool_maxsize=100,
                    max_retries=1,
                )
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                # 重要：绕过系统代理（Clash/V2Ray），直连上游 copilot.tencent.com
                # 否则请求会经过 Clash 再转发，可能造成 400 环路错误
                session.trust_env = False
                session.proxies = {"http": None, "https": None}
                self._sessions[domain] = session
                logger.info(f"创建上游连接池: {domain}")
            return self._sessions[domain]

    def select_key(self, model: str, allowed_key_ids: list = None, exclude: set = None,
                   key_mode: int = 1, request_data: dict = None) -> Optional[dict]:
        """选择一个可用的上游 Key（带缓存）

        Args:
            model: 模型名（用于模型级冷却过滤）
            allowed_key_ids: 子Key限定的上游Key ID列表
            exclude: 本次请求已尝试过的 key_id 集合，用于重试时跳过
            key_mode: 调用模式
                1 = 专一模式（默认）：真正粘住一个 Key，用到不可用才换下一个
                2 = 临期优先：优先调用积分组中最快过期的那组所在的 Key
                3 = 轮询模式：round-robin 轮换，每次请求换一个 Key
                4 = 会话亲和：同一会话绑定同一 Key，TTL 1 小时（优化项 #5/#7）
            request_data: 请求体 dict（key_mode=4 时用于计算 session_id）

        缓存 10 秒，减少 DB 锁竞争。
        """
        now = time.time()
        # 加锁保护缓存读写，防止并发请求同时触发缓存刷新导致读到不完整数据
        with self._cache_lock:
            db_version = self._db._key_status_version
            cache_expired = (now - self._upstream_keys_cache_time) > self._UPSTREAM_KEYS_CACHE_TTL
            version_changed = db_version != self._last_key_status_version
            if not self._cached_upstream_keys or cache_expired or version_changed:
                self._cached_upstream_keys = self._db.get_upstream_keys()
                self._upstream_keys_cache_time = now
                self._last_key_status_version = db_version

        keys = self._cached_upstream_keys

        # 筛选可用 Key
        available = []
        for k in keys:
            status = k.get("status")
            if status not in ("active",):
                # 跳过 exhausted / disabled / cooldown 状态的 Key
                continue
            if allowed_key_ids and k.get("key_id") not in allowed_key_ids:
                continue
            if exclude and k.get("key_id") in exclude:
                continue
            # 模型级冷却过滤（优化项 #8）：检查该 Key 对目标模型是否在冷却中
            kid = k.get("key_id", "")
            if model and not self._is_key_schedulable_for_model(kid, model):
                continue
            available.append(k)

        if not available:
            return None

        # 计算池标识（用于区分不同子Key绑定的上游Key池）
        pool_hash = "global"
        if allowed_key_ids:
            pool_hash = str(hash(tuple(sorted(allowed_key_ids))))

        # 负载感知辅助函数（优化项 #6）：并发计数低的优先
        def _concurrent_count(k: dict) -> int:
            return self._concurrent_counts.get(k.get("key_id", ""), 0)

        if key_mode == 4:
            # 会话亲和模式（优化项 #5/#7）：同一会话绑定同一上游 Key，TTL 1 小时
            session_id = self._get_session_id(request_data) if request_data else ""
            if session_id:
                # 检查已有的会话绑定
                binding = self._sticky_sessions.get(session_id)
                if binding:
                    bound_key_id, expire_ts = binding
                    if now > expire_ts:
                        # 绑定已过期，清理
                        self._sticky_sessions.pop(session_id, None)
                    else:
                        # 绑定有效，检查绑定的 Key 是否仍可调度
                        for k in available:
                            if k.get("key_id") == bound_key_id:
                                return k
                        # Key 不可调度，清理绑定并重新选择
                        self._sticky_sessions.pop(session_id, None)
                        logger.info(f"[会话亲和] session {session_id[:8]} 绑定的 Key 不可用，重新绑定")
            # 无绑定或绑定失效，选择并发最低的 Key 并绑定
            available.sort(key=_concurrent_count)
            chosen = available[0]
            if session_id:
                self._sticky_sessions[session_id] = (chosen.get("key_id", ""), now + 3600)
                logger.info(f"[会话亲和] session {session_id[:8]} 绑定 Key → {chosen.get('label', chosen.get('key_id', '')[:8])}")
            return chosen

        elif key_mode == 2:
            # 临期优先：找到所有 Key 中最快过期且有剩余积分的那组，优先用那个 Key
            # 排序依据：每个 Key 的 packages 中最快到期的 cycle_end 时间
            # 例如：KeyA 有 150分(明天过期) + 5000分(下月过期)，KeyB 有 3000分(后天过期)
            #   → KeyA 排前面（150分明天过期最紧急）
            # 负载感知：并发计数作为次级排序键（优化项 #6）
            def _earliest_expiring_time(k: dict) -> float:
                """返回该 Key 最快过期且有剩余积分的积分组的过期时间戳
                越小越优先（越快过期），没有过期信息的排最后
                """
                packages = k.get("packages", [])
                earliest = None
                for pkg in packages:
                    cycle_remain = 0
                    cycle_end = ""
                    if isinstance(pkg, dict):
                        cycle_remain = float(pkg.get("cycle_remain", 0))
                        cycle_end = str(pkg.get("cycle_end", ""))
                    if cycle_remain <= 0:
                        continue  # 跳过已耗尽的组
                    if not cycle_end:
                        continue  # 没有过期时间，跳过
                    # 解析过期时间
                    try:
                        from datetime import datetime as _dt
                        if "T" in cycle_end:
                            # ISO 8601 格式: 2026-06-30T23:59:59Z
                            dt = _dt.fromisoformat(cycle_end.replace("Z", "+00:00"))
                            ts = dt.timestamp()
                        else:
                            try:
                                ts = float(cycle_end)
                            except ValueError:
                                # 空格分隔格式: 2026-06-30 23:59:59
                                dt = _dt.strptime(cycle_end, "%Y-%m-%d %H:%M:%S")
                                ts = dt.timestamp()
                        if earliest is None or ts < earliest:
                            earliest = ts
                    except (ValueError, TypeError):
                        continue
                # 有过期信息的返回最早时间，没有的排到最后
                return earliest if earliest is not None else float('inf')

            earliest_time = min(_earliest_expiring_time(k) for k in available)
            earliest_group = [k for k in available if _earliest_expiring_time(k) == earliest_time]
            binding_key = f"{pool_hash}:{earliest_time}"
            dedicated_id = self._expiring_dedicated_keys.get(binding_key)
            if dedicated_id:
                for k in earliest_group:
                    if k.get("key_id") == dedicated_id:
                        return k

            earliest_group.sort(key=_concurrent_count)
            chosen = earliest_group[0]
            self._expiring_dedicated_keys[binding_key] = chosen.get("key_id", "")
            logger.info(
                f"[临期优先] 池 {pool_hash[:8]} 最早过期组 {earliest_time} 绑定 Key -> "
                f"{chosen.get('label', chosen.get('key_id', '')[:8])}"
            )
            return chosen

        elif key_mode == 3:
            # 轮询模式：round-robin，每次请求轮换到下一个 Key
            # 负载感知：先按并发计数排序，再轮询（优化项 #6）
            available.sort(key=_concurrent_count)
            idx = self._round_robin_index.get(pool_hash, 0)
            idx = idx % len(available)
            self._round_robin_index[pool_hash] = idx + 1
            return available[idx]

        else:
            # 专一模式：真正粘住一个 Key，用到它不可用才换下一个
            dedicated_id = self._dedicated_keys.get(pool_hash)
            if dedicated_id:
                # 上次用的 Key 还在 available 里，继续用它
                for k in available:
                    if k.get("key_id") == dedicated_id:
                        return k
            # 上次的 Key 不可用了（或第一次），选并发最低的并记住（负载感知 #6）
            available.sort(key=_concurrent_count)
            chosen = available[0]
            self._dedicated_keys[pool_hash] = chosen.get("key_id", "")
            logger.info(f"[专一] 池 {pool_hash[:8]} 切换专一 Key → {chosen.get('label', chosen.get('key_id', '')[:8])}")
            return chosen

    # ─── 优化项 #5/#7：粘性会话 session_id 提取 ───
    def _get_session_id(self, request_data: dict) -> str:
        """从请求体中提取会话标识（system message + 第一条 user message 的 SHA-256 hash 前 16 位）

        不依赖 X-Session-ID 头，用消息内容 hash 作为会话标识，相对稳定。
        """
        if not request_data:
            return ""
        messages = request_data.get("messages", [])
        system_content = ""
        first_user_content = ""
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            # content 可能是字符串或列表（多模态）
            if isinstance(content, list):
                content = " ".join(
                    str(c.get("text", "")) if isinstance(c, dict) else str(c)
                    for c in content
                )
            else:
                content = str(content)
            if role == "system" and not system_content:
                system_content = content
            elif role == "user" and not first_user_content:
                first_user_content = content
                break
        combined = system_content + first_user_content
        if not combined:
            return ""
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

    # ─── 优化项 #3：等待队列 ───
    def select_key_with_wait(self, model: str, allowed_key_ids: list = None,
                             exclude: set = None, key_mode: int = 1,
                             request_data: dict = None) -> Optional[dict]:
        """选择上游 Key，无可用时等待 5 秒重试一次（优化项 #3）

        select_key 返回 None 时，等待 5 秒后重试一次，仍无可用才返回 None。
        仅当有冷却中的 Key 时才等待（否则没有等待的意义）。
        """
        key = self.select_key(model, allowed_key_ids, exclude, key_mode, request_data)
        if key is not None:
            return key

        # 检查是否有冷却中的 Key（等待可能让它恢复）
        has_cooldown = False
        for k in self._cached_upstream_keys:
            if k.get("status") == "cooldown":
                if allowed_key_ids and k.get("key_id") not in allowed_key_ids:
                    continue
                if exclude and k.get("key_id") in exclude:
                    continue
                has_cooldown = True
                break

        if not has_cooldown:
            return None  # 没有冷却中的 Key，等待无意义

        logger.info("[等待队列] 无可用 Key 但有冷却中的 Key，等待 5 秒后重试...")
        threading.Event().wait(5)
        return self.select_key(model, allowed_key_ids, exclude, key_mode, request_data)

    # ─── 优化项 #8/#10：模型级冷却 + 渐进退避 ───
    def _is_key_schedulable_for_model(self, key_id: str, model: str) -> bool:
        """检查 Key 对特定模型是否可调度（不在模型级冷却中）"""
        with self._lock:
            cooldowns = self._model_cooldowns.get(key_id, {})
            expire_ts = cooldowns.get(model)
        if expire_ts and time.time() < expire_ts:
            return False
        # 清理过期条目
        if expire_ts:
            with self._lock:
                cooldowns = self._model_cooldowns.get(key_id, {})
                if model in cooldowns:
                    del cooldowns[model]
        return True

    def mark_model_cooldown(self, key_id: str, model: str) -> int:
        """标记模型级冷却，使用渐进退避（优化项 #8/#10）

        冷却时间 = min(10 * 2^(count-1), 80)，即 10→20→40→80 封顶。
        请求成功后调用 reset_cooldown_count() 归零。

        Returns:
            冷却秒数
        """
        with self._lock:
            count = self._cooldown_counts.get(key_id, 0) + 1
            self._cooldown_counts[key_id] = count
            cooldown_secs = min(10 * (2 ** (count - 1)), 80)
            expire_ts = time.time() + cooldown_secs
            if key_id not in self._model_cooldowns:
                self._model_cooldowns[key_id] = {}
            self._model_cooldowns[key_id][model] = expire_ts
        label = key_id[:8]
        logger.info(f"[模型冷却] Key {label} 模型 {model} 冷却 {cooldown_secs}s（第{count}次退避）")
        return cooldown_secs

    def reset_cooldown_count(self, key_id: str):
        """请求成功后归零渐进退避计数器（优化项 #10）"""
        with self._lock:
            if key_id in self._cooldown_counts:
                self._cooldown_counts[key_id] = 0

    # ─── 优化项 #7/#9：故障转移分类 ───
    def _classify_error(self, status_code: int, resp_body: str = "") -> str:
        """分类上游错误，决定故障转移策略

        Returns:
            "RETRY_SAME"  - 同 Key 重试 1 次再换（502/503/超时/连接错误）
            "SWITCH_KEY"  - 直接换 Key（401/403/429）
            "FATAL"       - 不重试，直接返回客户端（400 上下文超长 / 401 网关拦截）
        """
        if status_code == 0:
            # 异常（超时/连接错误）→ 同 Key 重试
            return "RETRY_SAME"
        if status_code == 400:
            if not resp_body.strip() or "input length too long" in resp_body or '"code":11115' in resp_body:
                return "FATAL"
            return "SWITCH_KEY"
        elif status_code == 401:
            # 401 返回 HTML = 网关层拦截，不重试
            if "<html>" in resp_body.lower() or "<!doctype" in resp_body.lower():
                return "FATAL"
            return "SWITCH_KEY"
        elif status_code in (502, 503):
            return "RETRY_SAME"
        elif status_code in (403, 429):
            return "SWITCH_KEY"
        else:
            return "SWITCH_KEY"

    # ─── 优化项 #17：健康检测 ───
    def start_health_check(self):
        """启动后台健康检测线程"""
        self._health_check_stop.clear()
        self._health_check_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._health_check_thread.start()

    def stop_health_check(self):
        """停止后台健康检测线程"""
        self._health_check_stop.set()

    def _health_check_loop(self):
        """后台健康检测循环：每 5 分钟 + 随机抖动检测所有 active/cooldown Key

        GET https://copilot.tencent.com/v2/v1/models 带 Bearer key，返回 200 即健康。
        失败标记 Key 为 cooldown（不是 disabled，让它能恢复）。
        """
        logger.info("[健康检测] 后台检测线程启动")
        while not self._health_check_stop.is_set():
            # 睡眠 5 分钟 + 随机抖动 0-60 秒
            sleep_secs = 300 + random.uniform(0, 60)
            if self._health_check_stop.wait(sleep_secs):
                break  # 收到停止信号

            try:
                keys = self._db.get_upstream_keys()
                upstream_url = self.get_upstream_url()
                models_url = f"{upstream_url}{UPSTREAM_MODELS_PATH}"

                for key in keys:
                    if self._health_check_stop.is_set():
                        break
                    key_id = key.get("key_id", "")
                    status = key.get("status", "active")
                    # 只检测 active 和 cooldown 状态的 Key
                    if status not in ("active", "cooldown"):
                        continue
                    api_key = key.get("api_key", "")
                    if not api_key:
                        continue

                    # 只检测 cooldown 状态的 Key 能否恢复，不主动把 active 标记成 cooldown
                    # （上游 /v1/models 可能返回 404，不能作为健康判据，避免误伤 active Key）
                    if status != "cooldown":
                        continue

                    self._last_health_check[key_id] = time.time()
                    label = key.get("label", key_id[:8])

                    try:
                        # 用最轻量的 chat 请求检测（上游 /v1/models 返回 404，不可用）
                        test_data = {
                            "model": "auto",
                            "stream": True,
                            "max_tokens": 1,
                            "messages": [
                                {"role": "system", "content": "You are helpful."},
                                {"role": "user", "content": "hi"},
                            ],
                        }
                        resp = requests.post(
                            f"{upstream_url}{UPSTREAM_CHAT_PATH}",
                            json=test_data,
                            headers=_build_workbuddy_relay_headers(api_key),
                            timeout=15,
                            proxies={"http": None, "https": None},
                        )
                        if resp.status_code in (200, 400):
                            # 200=正常 400=参数问题但Key有效 → 恢复为 active
                            self._db.update_upstream_key(key_id, {"status": "active"})
                            self._upstream_keys_cache_time = 0
                            logger.info(f"[健康检测] Key {label} cooldown → active (status={resp.status_code})")
                        elif resp.status_code in (401, 403):
                            # 认证/风控问题，保持 cooldown（不恶化到 disabled）
                            logger.warning(f"[健康检测] Key {label} 仍不可用 (status={resp.status_code})，保持 cooldown")
                        else:
                            logger.debug(f"[健康检测] Key {label} status={resp.status_code}，保持 cooldown")
                    except Exception as e:
                        logger.debug(f"[健康检测] Key {label} 检测异常: {e}，保持 cooldown")
            except Exception as e:
                logger.error(f"[健康检测] 检测循环异常: {e}")

        logger.info("[健康检测] 后台检测线程退出")

    def increment_concurrent(self, key_id: str):
        with self._lock:
            self._concurrent_counts[key_id] = self._concurrent_counts.get(key_id, 0) + 1

    def decrement_concurrent(self, key_id: str):
        with self._lock:
            if key_id in self._concurrent_counts:
                self._concurrent_counts[key_id] = max(0, self._concurrent_counts[key_id] - 1)

    def get_concurrent_keys(self) -> dict:
        """返回当前有并发请求的 key_id -> 并发数 字典（用于 UI 标记正在使用的 Key）"""
        with self._lock:
            return {k: v for k, v in self._concurrent_counts.items() if v > 0}

    def mark_key_exhausted(self, key_id: str):
        """标记 Key 为已耗尽（积分真正用完，不自动恢复）"""
        self._db.update_upstream_key(key_id, {"status": "exhausted"})
        # 立即刷新所有缓存，让 select_key 立刻跳过此 Key
        self._upstream_keys_cache_time = 0
        self._sub_keys_cache_time = 0  # 刷子Key缓存（allowed_key_ids 可能受影响）
        logger.warning(f"Key {key_id} 标记为 exhausted（积分耗尽，不自动恢复）")

    def mark_key_abnormal(self, key_id: str):
        """标记 Key 为异常（被上游风控，403 code:11140，不自动恢复）

        与 exhausted（积分耗尽）不同，abnormal 是账号被风控，
        积分可能还有，但 chat 接口被封。需要用户手动处理或换号。
        """
        self._db.update_upstream_key(key_id, {"status": "abnormal"})
        self._upstream_keys_cache_time = 0
        self._sub_keys_cache_time = 0
        logger.warning(f"Key {key_id} 标记为 abnormal（被上游风控，不自动恢复）")

    def mark_key_permanent_disabled(self, key_id: str):
        """标记 Key 为永久禁用（手动操作，不会被查分等自动恢复）

        与 disabled（积分为0自动禁用，查分>100自动恢复）不同，
        permanent_disabled 只能通过 mark_key_active() 手动恢复。
        """
        self._db.update_upstream_key(key_id, {"status": "permanent_disabled"})
        self._upstream_keys_cache_time = 0
        self._sub_keys_cache_time = 0
        logger.warning(f"Key {key_id} 标记为 permanent_disabled（永久禁用，需手动恢复）")

    def mark_key_active(self, key_id: str):
        """手动恢复 Key 为 active（用于恢复 permanent_disabled / abnormal 状态）"""
        self._db.update_upstream_key(key_id, {"status": "active"})
        self._upstream_keys_cache_time = 0
        self._sub_keys_cache_time = 0
        logger.info(f"Key {key_id} 手动恢复为 active")

    def mark_key_cooldown(self, key_id: str):
        """标记 Key 为临时冷却（429 临时限流，10秒后自动恢复）

        注意：仅用于临时限流（请求过快）。如果是额度耗尽(code 14018)，
        应调用 mark_key_exhausted()，不能自动恢复。
        """
        self._db.update_upstream_key(key_id, {"status": "cooldown"})
        # 立即刷新所有缓存，让 select_key 立刻跳过此 Key
        self._upstream_keys_cache_time = 0
        self._sub_keys_cache_time = 0
        # 启动后台线程 10 秒后自动恢复
        import threading
        def _recover():
            time.sleep(10)
            # 检查是否还是 cooldown（避免覆盖其他状态变更）
            keys = self._db.get_upstream_keys()
            for k in keys:
                if k.get("key_id") == key_id and k.get("status") == "cooldown":
                    self._db.update_upstream_key(key_id, {"status": "active"})
                    logger.info(f"Key {key_id} 冷却完成，自动恢复为 active")
                    # 刷新缓存
                    self._upstream_keys_cache_time = 0
                    break
        t = threading.Thread(target=_recover, daemon=True)
        t.start()
        logger.warning(f"Key {key_id} 被限流(429)，进入 10 秒冷却")

    def mark_key_rate_limited(self, key_id: str):
        """标记 Key 为系统限流（401/429 系统级限流，不自动恢复）

        与 cooldown（429 临时限流，10秒自动恢复）不同，
        rate_limited 需手动恢复或下次查分不限流才恢复。
        """
        self._db.update_upstream_key(key_id, {"status": "rate_limited"})
        self._upstream_keys_cache_time = 0
        self._sub_keys_cache_time = 0
        logger.warning(f"Key {key_id} 标记为 rate_limited（系统限流，需手动或查分恢复）")

    def get_upstream_url(self, model: str = "") -> str:
        """获取上游 API base URL（带缓存）
        
        缓存 30 秒，避免每次请求都拿 DB 锁读 settings。
        用户修改 upstream_proxy 设置后最多 30 秒生效。
        """
        now = time.time()
        if self._cached_upstream_url and (now - self._upstream_url_cache_time) < self._UPSTREAM_URL_CACHE_TTL:
            return self._cached_upstream_url
        
        settings = self._db.get_settings()
        custom_proxy = settings.get("upstream_proxy", "")
        if custom_proxy:
            url = custom_proxy
        else:
            url = _get_default_upstream_proxy()
        
        self._cached_upstream_url = url
        self._upstream_url_cache_time = now
        return url
    
    def invalidate_upstream_cache(self):
        """强制刷新 upstream URL 缓存（用户修改设置后调用）"""
        self._cached_upstream_url = ""
        self._upstream_url_cache_time = 0
        self._cached_upstream_keys = []
        self._upstream_keys_cache_time = 0
        self._cached_sub_keys = {}
        self._sub_keys_cache_time = 0
        self._last_sub_key_version = -1
        self._expiring_dedicated_keys.clear()

    def authenticate_sub_key(self, token: str) -> Optional[dict]:
        """验证子 API Key（带缓存，避免每次请求遍历+加锁）
        
        缓存 token -> sub_key 映射 10 秒，认证从 O(n) + DB锁 变成 O(1) dict 查找。
        """
        now = time.time()
        with self._cache_lock:
            db_version = self._db._sub_key_version
            cache_expired = (now - self._sub_keys_cache_time) > self._SUB_KEYS_CACHE_TTL
            version_changed = db_version != self._last_sub_key_version
            if not self._cached_sub_keys or cache_expired or version_changed:
                sub_keys = self._db.get_sub_api_keys()
                self._cached_sub_keys = {}
                for sk in sub_keys:
                    if sk.get("api_key") and sk.get("is_active", True):
                        self._cached_sub_keys[sk["api_key"]] = sk
                self._sub_keys_cache_time = now
                self._last_sub_key_version = db_version
        
        return self._cached_sub_keys.get(token)

    def close_all_sessions(self):
        """关闭所有上游连接池（退出时调用，释放 TCP 连接）"""
        with self._session_lock:
            for domain, session in self._sessions.items():
                try:
                    session.close()
                    logger.debug(f"关闭上游连接池: {domain}")
                except Exception:
                    pass
            self._sessions.clear()


class ProxyRequestHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器 - OpenAI 兼容接口
    
    性能优化：
    - TCP_NODELAY：禁用 Nagle 算法，SSE 小包立即发送
    - 直接 socket sendall：绕过 BufferedWriter 双重缓冲
    - 缓存 upstream URL：避免每次请求读 DB 加锁
    """

    # 类变量，由 ProxyServer 设置
    router: ProxyRouter = None
    db: ProxyDatabase = None
    server_mode: str = "local"  # "local" or "open"

    def _get_client_ip(self) -> str:
        """获取客户端真实 IP（支持 Nginx 反代）"""
        real_ip = self.headers.get("X-Real-IP", "").strip()
        if real_ip:
            return real_ip
        forwarded = self.headers.get("X-Forwarded-For", "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def setup(self):
        """连接建立时设置 socket 选项"""
        super().setup()
        # 关键优化：禁用 Nagle 算法，SSE 小包不缓冲立即发送
        # 不做这个的话每个 SSE chunk 会被 OS 缓冲 40-200ms
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    def log_message(self, format, *args):
        """覆盖默认日志"""
        logger.debug(f"Proxy: {format % args}")

    def _add_log(self, event: str, sub_key: dict = None, upstream_key: dict = None,
                 model: str = "", duration_ms: int = 0, error: str = "",
                 prompt_tokens: int = 0, completion_tokens: int = 0,
                 upstream_status: int = 0, request_path: str = ""):
        """写入请求日志到 DB（统一入口）

        Args:
            event: 事件类型（auth_fail / upstream_error / upstream_429 / start / end / error / request）
            sub_key: 子Key dict（可选）
            upstream_key: 上游Key dict（可选）
            model: 模型名
            duration_ms: 耗时
            error: 错误详情
            prompt_tokens: prompt token数
            completion_tokens: completion token数
            upstream_status: 上游返回的HTTP状态码
            request_path: 请求路径
        """
        self.db.add_request_log({
            "timestamp": time.time(),
            "sub_key_id": sub_key.get("key_id", "") if sub_key else "",
            "sub_key_label": sub_key.get("label", "") if sub_key else "",
            "main_key_id": upstream_key.get("key_id", "") if upstream_key else "",
            "main_key_label": upstream_key.get("label", "") if upstream_key else "",
            "model": model,
            "event": event,
            "duration_ms": duration_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "error": error,
            "upstream_status": upstream_status,
            "request_path": request_path,
        })

    def _log_request(self, method: str, path: str):
        """记录所有收到的请求（同时写入 DB 日志，方便在 UI 中查看）"""
        auth = self.headers.get("Authorization", "")
        auth_hint = f"Bearer ...{auth[-8:]}" if len(auth) > 8 else "(none)"
        # 开放模式显示客户端 IP，方便排查
        if self.server_mode == "open":
            client_ip = self._get_client_ip()
            logger.info(f"📨 收到请求  {method} {path} auth={auth_hint} client={client_ip}")
        else:
            logger.info(f"📨 收到请求  {method} {path} auth={auth_hint}")
        # 同时写入 DB 日志，这样在 UI 的使用日志里也能看到每个请求
        self._add_log(
            event="request",
            error=f"{method} {path} auth={auth_hint}",
            request_path=path,
        )

    def _send_json(self, status: int, data: dict, extra_headers: dict = None):
        """发送 JSON 响应

        Args:
            status: HTTP 状态码
            data: 响应数据 dict
            extra_headers: 额外响应头（如 Retry-After）
        """
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        # CORS 头：防止 WorkBuddy 的 WebView 组件因跨域限制拒绝请求
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self, quiet: bool = False) -> Optional[dict]:
        """验证子 API Key，返回 sub_key dict 或 None

        本地模式：透传模式 — 当 token 不匹配任何子Key时，自动创建一个虚拟子Key，
        让请求可以正常转发。这是为了兼容 WorkBuddy 客户端的行为——
        WorkBuddy 会用自己的 JWT token 发请求，而不是用户配置的 sk-xxx 子Key。

        开放模式：强制子Key鉴权 — 不匹配任何子Key的请求直接拒绝，
        因为开放模式下网络上的任何人都可以连接，必须用子Key控制访问。

        Args:
            quiet: 静默模式，不打印 warning（用于非核心端点）

        Returns:
            sub_key dict（可能是真实子Key或虚拟透传子Key），或者 None（认证失败时）
        """
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            if not quiet:
                client_ip = self._get_client_ip()
                logger.warning(f"[认证] 缺少 Bearer token, path={self.path}, client={client_ip}")
            return None
        token = auth[7:].strip()
        # 使用缓存认证，O(1) dict 查找，不每次拿 DB 锁遍历
        result = self.router.authenticate_sub_key(token)
        if result:
            return result

        # Generated proxy keys are authoritative. If a sk-* key is not found,
        # it was deleted/disabled or never existed, so local passthrough must
        # not silently grant access.
        if token.startswith("sk-"):
            if not quiet:
                client_ip = self._get_client_ip()
                logger.warning(f"[认证] 子Key不存在或已删除, token=...{token[-6:] if len(token) > 6 else token}, client={client_ip}")
            return None

        # 开放模式：不匹配子Key → 直接拒绝，不创建虚拟透传子Key
        if self.server_mode == "open":
            if not quiet:
                client_ip = self._get_client_ip()
                logger.warning(f"[认证] 开放模式下 token=...{token[-6:] if len(token) > 6 else token} 不匹配子Key，拒绝访问, client={client_ip}")
            return None

        # 本地模式透传：token 不匹配任何子Key，但请求带了 Bearer token
        # WorkBuddy 自己的 JWT token → 创建通用透传子Key（使用全部上游Key）
        if not quiet:
            logger.info(f"[透传] token=...{token[-6:] if len(token) > 6 else token} 不匹配子Key，启用透传模式")
        return {
            "key_id": "_passthrough_",
            "api_key": token,  # 保留原始token，但不用于上游认证
            "label": f"透传(...{token[-6:] if len(token) > 6 else token})",
            "is_active": True,
            "allowed_models": [],  # 空=全部模型
            "allowed_key_ids": [],  # 空=全部上游Key
            "max_usage": 0,
            "used_count": 0,
            "rate_limit_rpm": 1000,
            "key_mode": 1,
        }

    def do_GET(self):
        """GET 请求处理"""
        parsed = urlparse(self.path)
        path = parsed.path
        self._log_request("GET", path)

        # 根路径 / 或 /v1 — 健康检查，直接返回 200（不需要认证）
        # WorkBuddy 可能用这些路径检查端点是否可用
        if path in ("/", "/v1", "/v1/"):
            self._send_json(200, {
                "object": "api.index",
                "message": "Antigravity Proxy is running",
                "version": "1.8.0",
            })
            return

        # /v1/models - 返回可用模型列表
        if path == "/v1/models":
            sub_key = self._authenticate(quiet=True)
            # 开放模式：必须有子Key才能获取模型列表
            if self.server_mode == "open" and not sub_key:
                client_ip = self._get_client_ip()
                logger.warning(f"[认证] /v1/models 开放模式无子Key，拒绝, client={client_ip}")
                self._send_json(401, {"error": {"message": "Invalid API key", "type": "authentication_error"}})
                return
            # 本地模式透传：无论是真实子Key还是透传模式，都返回完整模型列表
            if not sub_key:
                # 没有 Bearer token at all，仍然返回完整列表（不弹登录）
                logger.info(f"[透传] /v1/models 无Bearer token，返回全部模型")
                sub_key = {"allowed_models": []}
            allowed_models = sub_key.get("allowed_models", [])
            normalized_allowed_models = [MODEL_ID_ALIASES.get(str(m).strip(), str(m).strip()) for m in allowed_models]
            model_list = SUPPORTED_MODELS if not normalized_allowed_models else [m for m in SUPPORTED_MODELS if m in normalized_allowed_models]
            models_data = {
                "object": "list",
                "data": [
                    {
                        "id": m,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "antigravity-proxy",
                        "supportsToolCall": True,
                        "supportsImages": _model_supports_images(m),
                        "supportsReasoning": True,
                        "reasoning": {"supportedEfforts": ["max"]},
                    }
                    for m in model_list
                ]
            }
            self._send_json(200, models_data)
            return

        # /v1/engines - 兼容旧版 OpenAI 客户端
        if path == "/v1/engines":
            sub_key = self._authenticate(quiet=True)
            if not sub_key:
                self._send_json(200, {"object": "list", "data": []})
                return
            self._send_json(200, {"object": "list", "data": []})
            return

        # 未识别的 GET 端点：返回 404 但不是 401（不会触发客户端重登录）
        logger.warning(f"[未识别] GET {path}")
        self._send_json(404, {"error": {"message": f"Endpoint not found: {path}", "type": "not_found"}})

    def do_POST(self):
        """POST 请求处理"""
        parsed = urlparse(self.path)
        path = parsed.path
        self._log_request("POST", path)

        # /v1/chat/completions - 核心转发
        if path == "/v1/chat/completions":
            self._handle_chat_completions()
            return

        if path == "/v1/responses":
            self._handle_responses()
            return

        # 未识别的 POST 端点：返回 404 但不是 401
        logger.warning(f"[未识别] POST {path}")
        self._send_json(404, {"error": {"message": f"Endpoint not found: {path}", "type": "not_found"}})

    def do_OPTIONS(self):
        """OPTIONS 请求处理（CORS 预检请求）

        WorkBuddy/Electron 客户端可能发送 CORS 预检请求，
        如果不处理，默认返回 501 HTML 页面，客户端无法正确解析。
        """
        self._log_request("OPTIONS", self.path)
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        # [v1.6.1-fix] 入站允许客户端发任意头（浏览器层放行），出站再由白名单丢弃
        # 之前只允许 Authorization, Content-Type, Accept，WorkBuddy 预检带 X-Product-Version 会被挡
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_responses(self):
        """Handle /v1/responses by translating it to the chat pipeline."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 50 * 1024 * 1024:
                self._send_json(413, {"error": {"message": "Request body too large (max 50MB)", "type": "invalid_request"}})
                return
            body = self.rfile.read(content_length)
            request_data = json.loads(body)
            chat_request, meta = _responses_to_chat_request(request_data)
            logger.info(
                "[responses] translated request input_type=%s messages=%s stream=%s",
                meta.get("input_type"),
                meta.get("message_count"),
                chat_request.get("stream"),
            )
        except Exception as e:
            self._send_json(400, {"error": {"message": f"Invalid request body: {e}", "type": "invalid_request"}})
            return

        self._handle_chat_completions(
            request_data_override=chat_request,
            response_mode="responses",
            request_path="/v1/responses",
        )

    def _handle_chat_completions(self, request_data_override: dict = None,
                                 response_mode: str = "chat",
                                 request_path: str = "/v1/chat/completions"):
        """处理 /v1/chat/completions 请求
        
        自动重试：上游 Key 报错时自动换下一个 Key 重试，
        最多尝试 3 个不同 Key，全部失败才返回错误给客户端。
        """
        t0 = time.time()
        
        # 1. 验证子 Key
        # 关键：不返回 401/403！WorkBuddy 客户端收到 401 会触发重新登录。
        # 用 503 (Service Unavailable) 代替，表示"服务暂时不可用"，不触发认证流程。
        sub_key = self._authenticate()
        if not sub_key:
            # 开放模式：返回标准 401，客户端不是 WorkBuddy，不会触发重登录
            if self.server_mode == "open":
                client_ip = self._get_client_ip()
                error_detail = "无效的 API Key"
                logger.warning(f"[认证] {error_detail}, client={client_ip}")
                self._add_log(event="auth_fail", error=error_detail, request_path="/v1/chat/completions")
                self._send_json(401, {"error": {"message": "Invalid API key", "type": "authentication_error"}})
                return
            # 本地模式：不返回 401/403！WorkBuddy 客户端收到 401 会触发重新登录。
            # 用 503 (Service Unavailable) 代替，表示"服务暂时不可用"，不触发认证流程。
            error_detail = "请求缺少 Bearer token"
            logger.warning(f"[认证] {error_detail}，返回503")
            self._add_log(event="auth_fail", error=error_detail, request_path="/v1/chat/completions")
            self._send_json(503, {"error": {"message": "Service temporarily unavailable", "type": "server_error"}})
            return

        is_passthrough = sub_key.get("key_id") == "_passthrough_"
        if is_passthrough:
            logger.info(f"[透传] chat请求使用透传模式，自动选上游Key")
            self._add_log(event="request", sub_key=sub_key, error="透传模式:自动选上游Key", request_path="/v1/chat/completions")

        # 2. 检查子 Key 状态（透传模式跳过）
        if not is_passthrough:
            if not sub_key.get("is_active", True):
                error_detail = f"子Key已禁用, sub={sub_key.get('label', sub_key.get('key_id', ''))}"
                logger.warning(f"[禁用] {error_detail}")
                self._add_log(event="auth_fail", sub_key=sub_key, error=error_detail, request_path="/v1/chat/completions")
                self._send_json(503, {"error": {"message": "Service temporarily unavailable", "type": "server_error"}})
                return

            max_usage = sub_key.get("max_usage", 0)
            used_count = sub_key.get("used_count", 0)
            if max_usage > 0 and used_count >= max_usage:
                error_detail = f"子Key使用次数超限, sub={sub_key.get('label', '')} used={used_count}/{max_usage}"
                logger.warning(f"[限流] {error_detail}")
                self._add_log(event="auth_fail", sub_key=sub_key, error=error_detail, request_path="/v1/chat/completions")
                self._send_json(429, {"error": {"message": "Usage limit exceeded", "type": "rate_limit"}})
                return

        # 3. 解析请求体（优化项 #14：请求体大小限制 50MB）
        if request_data_override is not None:
            request_data = request_data_override
        else:
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length > 50 * 1024 * 1024:
                    logger.warning(f"[安全] 请求体过大: {content_length} bytes，拒绝")
                    self._add_log(
                        event="error",
                        sub_key=sub_key,
                        error=f"请求体过大: {content_length} bytes",
                        request_path=request_path,
                    )
                    self._send_json(413, {"error": {"message": "Request body too large (max 50MB)", "type": "invalid_request"}})
                    return
                body = self.rfile.read(content_length)
                request_data = json.loads(body)
            except Exception as e:
                self._send_json(400, {"error": {"message": f"Invalid request body: {e}", "type": "invalid_request"}})
                return

        model = request_data.get("model", "")
        if not model:
            model = "auto"  # 默认模型，让上游服务端路由
            request_data["model"] = model  # [v1.6.1-fix] 写回 request_data，否则白名单转发时上游收不到 model
        else:
            original_model = str(model).strip()
            aliased_model = MODEL_ID_ALIASES.get(original_model, original_model)
            if aliased_model != original_model:
                logger.info(f"[模型别名] {original_model} -> {aliased_model}")
                model = aliased_model
                request_data["model"] = model
        # 与服务器端 chat.py 一致：messages 少于 2 条时补 system 消息
        # 上游 copilot.tencent.com 要求至少 2 条 message，否则返回 400
        messages = request_data.get("messages", [])
        # [v1.6.1-fix] 严格校验 messages 类型，防止字符串/None/dict 导致后续 .insert() 崩溃
        if not isinstance(messages, list) or not messages:
            self._send_json(400, {"error": {"message": "messages is required and must be a non-empty array", "type": "invalid_request"}})
            return
        if len(messages) < 2:
            messages.insert(0, {"role": "system", "content": "You are a helpful assistant."})
            request_data["messages"] = messages

        # [v1.6.1-fix] 旧的图片拦截逻辑已移除（模型能力检查 + 历史图片剥离）
        # 原因：新的白名单制 + 图片格式归一化已覆盖所有场景，旧逻辑多余且可能误拦截
        # [ROLLBACK] 恢复方法：把下面的代码取消注释
        # latest_user_idx = _latest_user_message_index(messages)
        # latest_user_has_image = latest_user_idx >= 0 and _message_has_image(messages[latest_user_idx])
        # image_stats = _detect_multimodal_images(request_data)
        # history_strip_stats = {"stripped_images": 0}
        # if model in IMAGE_UNSUPPORTED_TEXT_MODELS and image_stats["image_count"] and not latest_user_has_image:
        #     history_strip_stats = _strip_historical_images_for_text_model(request_data, latest_user_idx)
        #     image_stats = _detect_multimodal_images(request_data)
        #     if history_strip_stats["stripped_images"]:
        #         logger.info(
        #             f"[历史图片省略] model={model}, stripped_images={history_strip_stats['stripped_images']}"
        #         )
        # if latest_user_has_image and model in IMAGE_UNSUPPORTED_TEXT_MODELS:
        #     error_detail = f"{model} 不支持图片输入，如需图片识别请切换到 glm-5v-turbo 模型"
        #     logger.warning(
        #         f"[图片拒绝] model={model}, images={image_stats['image_count']}, "
        #         f"data_uri={image_stats['data_uri_count']}, max_image_chars={image_stats['max_image_chars']}"
        #     )
        #     self._add_log(event="error", sub_key=sub_key, model=model, error=error_detail, request_path="/v1/chat/completions")
        #     self._send_json(400, {
        #         "error": {
        #             "message": error_detail,
        #             "type": "unsupported_multimodal",
        #             "code": "model_image_not_supported",
        #         }
        #     })
        #     return
        # ── 旧逻辑结束 ──

        # 4. 检查模型是否允许（透传模式跳过）
        if not is_passthrough:
            allowed_models = sub_key.get("allowed_models", [])
            normalized_allowed_models = [MODEL_ID_ALIASES.get(str(m).strip(), str(m).strip()) for m in allowed_models]
            if normalized_allowed_models and model not in normalized_allowed_models:
                # 用 503 代替 403，避免触发 WorkBuddy 认证流程
                error_detail = f"模型 {model} 不允许, 允许: {allowed_models}"
                logger.warning(f"[模型] {error_detail}")
                self._add_log(event="error", sub_key=sub_key, model=model, error=error_detail, request_path="/v1/chat/completions")
                self._send_json(503, {"error": {"message": f"Model {model} not available", "type": "server_error"}})
                return

        # 5. 获取上游 URL
        upstream_url = self.router.get_upstream_url(model)
        target_url = f"{upstream_url}{UPSTREAM_CHAT_PATH}"

        # ─── 自动重试循环：最多尝试 3 个不同 Key ───
        MAX_RETRY_KEYS = 3
        total_attempts = 0
        tried_key_ids = set()       # 已尝试过的 key_id（换 Key 时加入）
        same_key_retried = set()    # 已同 Key 重试过的 key_id（优化项 #7：RETRY_SAME 每键只重试一次）
        last_error = None           # 最后一次的错误信息
        last_error_status = 503     # 最后一次的错误状态码
        last_cooldown_secs = 0      # 最后一次冷却秒数（用于 Retry-After 头，优化项 #9）
        last_upstream_error_log = None  # 最后一次上游原始错误详情（仅用于本地排查日志）
        _ctx_compressed = [False]    # 上下文是否已压缩过（用 list 包装以便在嵌套函数中修改）
        allowed_key_ids = sub_key.get("allowed_key_ids", [])
        key_mode = sub_key.get("key_mode", 1)

        while len(tried_key_ids) < MAX_RETRY_KEYS:
            total_attempts += 1
            # 选择上游 Key（排除已尝试的），带等待队列（优化项 #3）
            upstream_key = self.router.select_key_with_wait(
                model,
                allowed_key_ids if allowed_key_ids else None,
                exclude=tried_key_ids,
                key_mode=key_mode,
                request_data=request_data,
            )
            if not upstream_key:
                # 没有可用的 Key 了 — 打印详细诊断信息
                all_keys = self.router._cached_upstream_keys or []
                active_count = sum(1 for k in all_keys if k.get("status") == "active")
                excluded_count = len(tried_key_ids) if tried_key_ids else 0
                cooldown_count = sum(1 for k in all_keys if k.get("status") == "cooldown")
                # 检查模型级冷却
                model_cooldown_count = 0
                if model:
                    for k in all_keys:
                        kid = k.get("key_id", "")
                        if kid and not self.router._is_key_schedulable_for_model(kid, model):
                            model_cooldown_count += 1
                logger.warning(
                    f"[重试] 第{total_attempts}次：无可用的上游 Key | "
                    f"总计={len(all_keys)} active={active_count} cooldown={cooldown_count} "
                    f"excluded={excluded_count} model_cooldown={model_cooldown_count} "
                    f"tried_key_ids={tried_key_ids}"
                )
                break

            key_id = upstream_key.get("key_id", "")
            label = upstream_key.get("label", key_id[:8])
            logger.info(f"[重试] 第{total_attempts}次尝试 Key: {label}")

            # 更新并发计数
            self.router.increment_concurrent(key_id)

            try:
                # ─── v1.6.6 WorkBuddy Relay：保留客户端 body，只替换上游 Key ───
                upstream_api_key = upstream_key.get("api_key", "")
                req_headers = _build_workbuddy_relay_headers(upstream_api_key)
                client_wants_stream = request_data.get("stream", False)
                upstream_request_data, build_meta = _build_workbuddy_relay_body(request_data)
                sensitive_replaced = build_meta.get("system_prompt_sensitive_replaced", 0)
                if sensitive_replaced:
                    self._add_log(
                        event="sensitive_replace",
                        sub_key=sub_key,
                        upstream_key=upstream_key,
                        model=model,
                        error=f"系统提示词敏感信息替换 {sensitive_replaced} 处",
                        request_path="/v1/chat/completions",
                    )

                if (
                    build_meta["removed_null_fields"]
                    or build_meta["translated_fields"]
                    or build_meta["dropped_fields"]
                    or build_meta["history_images_replaced"]
                    or build_meta["normalized_images"]
                    or build_meta["unsupported_inline_images_removed"]
                ):
                    logger.info(
                        f"[v1.6.6] WorkBuddy Relay 调整字段: "
                        f"dropped={build_meta['dropped_fields']}; "
                        f"removed_null={build_meta['removed_null_fields']}; "
                        f"translated={build_meta['translated_fields']}; "
                        f"history_images_replaced={build_meta['history_images_replaced']}; "
                        f"normalized_images={build_meta['normalized_images']}; "
                        f"unsupported_inline_images_removed={build_meta['unsupported_inline_images_removed']}"
                    )

                image_stats = _detect_multimodal_images(upstream_request_data)

                # 记录请求体大小和消息数（用于排查上下文超长问题）
                msg_count = len(upstream_request_data.get("messages", []))
                body_size = len(json.dumps(upstream_request_data, ensure_ascii=False))

                # 检测请求是否包含图片内容（排查图片上下文超限问题）
                has_image = image_stats["image_count"] > 0

                logger.info(f"[代理] v1.6.6 WorkBuddy Relay 请求 {int((time.time()-t0)*1000)}ms, model={model}, key={label}, "
                           f"messages={msg_count}, body={body_size}B ({body_size//1024}KB), "
                           f"stream={client_wants_stream}, image={has_image}, images={image_stats['image_count']}, "
                           f"data_uri={image_stats['data_uri_count']}, max_image_chars={image_stats['max_image_chars']}, "
                           f"dropped={build_meta['dropped_fields']}, "
                           f"history_images_replaced={build_meta['history_images_replaced']}, "
                           f"normalized_images={build_meta['normalized_images']}, "
                           f"unsupported_inline_images_removed={build_meta['unsupported_inline_images_removed']}, "
                           f"mode={build_meta['mode']}, stream_options={build_meta['has_stream_options']}")

                # ─── 发送请求到上游 ───
                session = self.router._get_session(upstream_url)
                t_send = time.time()
                resp = session.post(
                    target_url,
                    json=upstream_request_data,
                    headers=req_headers,
                    timeout=120,
                    stream=True,
                )
                logger.info(f"[代理] 上游响应 {int((time.time()-t_send)*1000)}ms, status={resp.status_code}, "
                           f"body={body_size//1024}KB, key={label}")

                if resp.status_code != 200:
                    self.router.decrement_concurrent(key_id)
                    resp_body = resp.text

                    # 故障转移分类（优化项 #7）
                    error_type = self.router._classify_error(resp.status_code, resp_body)
                    upstream_error_log = _parse_upstream_error_for_log(resp.status_code, resp_body, resp.headers)
                    request_error_log = _summarize_request_for_error_log(
                        upstream_request_data, req_headers, build_meta
                    )
                    last_upstream_error_log = upstream_error_log
                    logger.warning(
                        "[上游错误详情] %s",
                        _safe_json_for_log({
                            "model": model,
                            "key": label,
                            "url": target_url,
                            "attempt": total_attempts,
                            "error_type": error_type,
                            "upstream": upstream_error_log,
                            "request": request_error_log,
                        }, limit=16000),
                    )

                    # 根据状态码标记 Key
                    if resp.status_code == 429:
                        # 区分「额度耗尽」和「临时限流」
                        is_quota_exhausted = False
                        try:
                            err_json = json.loads(resp_body)
                            err_code = (err_json.get("error", {}).get("data", {}).get("code", 0)
                                        or err_json.get("code", 0))
                            if err_code in (14018, 14019):  # 14018=额度已用尽, 14019=额度不足
                                is_quota_exhausted = True
                        except (json.JSONDecodeError, AttributeError):
                            pass

                        if is_quota_exhausted:
                            # 积分耗尽是永久的，不能自动恢复，标记为 exhausted
                            self.router.mark_key_exhausted(key_id)
                            error_detail = f"Key {label} 额度耗尽(429/14018): {resp_body[:200]}"
                        else:
                            # 临时限流：模型级冷却 + 渐进退避（优化项 #8/#10）
                            cooldown_secs = self.router.mark_model_cooldown(key_id, model)
                            last_cooldown_secs = cooldown_secs
                            error_detail = f"Key {label} 模型 {model} 被限流(429)，冷却 {cooldown_secs}s: {resp_body[:200]}"
                        logger.warning(f"[重试] {error_detail}")
                        self._add_log(event="upstream_429", sub_key=sub_key, upstream_key=upstream_key,
                                      model=model, error=error_detail, upstream_status=429)
                    elif resp.status_code == 401:
                        # 区分：401 返回 JSON（真正的认证失败） vs 返回 HTML（网关层拦截）
                        is_html_response = "<html>" in resp_body.lower() or "<!doctype" in resp_body.lower()
                        if is_html_response:
                            # openresty/APISIX 网关层拦截（可能是超长上下文被 WAF 拦截）
                            # 不冷却 Key（不是 Key 的问题），直接返回错误，不重试（FATAL）
                            error_detail = f"Key {label} 401 返回 HTML（网关拦截）: {resp_body[:200]}"
                            logger.warning(f"[拒绝] {error_detail}")
                            self._add_log(event="upstream_error", sub_key=sub_key, upstream_key=upstream_key,
                                          model=model, error=error_detail, upstream_status=401)
                            self._send_json(502, {
                                "error": {
                                    "message": "请求被上游网关拦截，可能是上下文过长或请求格式异常。请尝试新开对话。",
                                    "type": "upstream_gateway_rejected"
                                }
                            })
                            return
                        # 401：纯 API 头不应该出现 invalid_format，如果有说明 Key 有问题
                        logger.info(f"[401] Key {label} 上游返回: status=401, body={resp_body[:500]}")
                        # 401 认证失败 → 标记为 rate_limited（不自动恢复，需手动或下次查分恢复）
                        self.router.mark_key_rate_limited(key_id)
                        error_detail = f"Key {label} 认证失败(401): {resp_body[:200]}"
                        logger.warning(f"[重试] {error_detail}")
                        self._add_log(event="upstream_error", sub_key=sub_key, upstream_key=upstream_key,
                                      model=model, error=error_detail, upstream_status=401)
                    elif resp.status_code == 400 and not resp_body.strip():
                        # 400 空 body：上游临时问题，不判定为上下文过长，直接换 Key 重试
                        logger.warning(f"[重试] Key {label} 上游返回 400 空 body，换 Key 重试")
                        self._add_log(event="upstream_error", sub_key=sub_key, upstream_key=upstream_key,
                                      model=model, error="上游返回400空body", upstream_status=400)
                        last_error = json.dumps({"error": {"message": "上游返回为空，请重试", "type": "upstream_empty_response"}})
                        last_error_status = 502
                    elif resp.status_code == 400 and ("input length too long" in resp_body or '"code":11115' in resp_body):
                        # Let WorkBuddy handle context compaction itself. Returning the
                        # recognizable 11115/input-length error avoids creating invalid
                        # partial tool-message histories in the proxy.
                        error_detail = f"Key {label} 上游返回 400 input length too long"
                        logger.warning(f"[拒绝] {error_detail}")
                        self._add_log(event="end", sub_key=sub_key, upstream_key=upstream_key,
                                      model=model, error="context_too_long")
                        self._send_json(400, {
                            "code": 11115,
                            "msg": "input length too long",
                            "error": {
                                "message": "input length too long",
                                "type": "context_length_exceeded",
                                "code": 11115,
                            }
                        })
                        return
                    else:
                        # 检测 403 风控（code:11140 request illegal）
                        if resp.status_code == 403 and '"code":11140' in resp_body:
                            error_detail = f"Key {label} 被风控(403/11140): {resp_body[:200]}"
                            logger.warning(f"[风控] {error_detail}")
                            self._add_log(event="upstream_error", sub_key=sub_key, upstream_key=upstream_key,
                                          model=model, error=error_detail, upstream_status=403)
                            # 标记为 abnormal，不再参与轮询
                            self.router.mark_key_abnormal(key_id)
                        elif resp.status_code == 400:
                            # 400 但不是空body也不是 input length too long — 记录真正发给上游的参数用于排查
                            code = upstream_error_log.get("code")
                            msg = upstream_error_log.get("msg")
                            request_id = upstream_error_log.get("requestId") or upstream_error_log.get("request_id_header")
                            ext_error = upstream_error_log.get("extError")
                            logger.warning(
                                "[400排查] %s",
                                _safe_json_for_log({
                                    "model": model,
                                    "key": label,
                                    "code": code,
                                    "msg": msg,
                                    "requestId": request_id,
                                    "extError": ext_error,
                                    "raw_body": upstream_error_log.get("raw_body"),
                                    "request": request_error_log,
                                }, limit=16000),
                            )
                            error_detail = _safe_json_for_log({
                                "key": label,
                                "status": 400,
                                "code": code,
                                "msg": msg,
                                "requestId": request_id,
                                "extError": ext_error,
                                "raw_body": upstream_error_log.get("raw_body"),
                                "request_fields": request_error_log.get("body_fields"),
                                "non_message_fields": request_error_log.get("non_message_fields"),
                                "messages": request_error_log.get("messages"),
                                "image_stats": request_error_log.get("image_stats"),
                                "relay_meta": build_meta,
                            }, limit=6000)
                            # 记录到 DB 日志面板，方便用户排查原始 400 错误
                            self._add_log(event="upstream_error", sub_key=sub_key, upstream_key=upstream_key,
                                          model=model, error=error_detail, upstream_status=400)
                            # 11133 可能是上游节点偶发故障（不是参数问题），换 Key 重试通常能解决
                            # 不冷却该 Key（避免误伤），只标记 tried 换下一个 Key
                        else:
                            error_detail = f"Key {label} 上游返回 {resp.status_code}: {resp_body[:200]}"
                            logger.warning(f"[重试] {error_detail}")
                            self._add_log(event="upstream_error", sub_key=sub_key, upstream_key=upstream_key,
                                          model=model, error=error_detail, upstream_status=resp.status_code)

                    # ─── 故障转移决策（优化项 #7）───
                    # FATAL 已在上面 return，此处只处理 RETRY_SAME 和 SWITCH_KEY
                    if error_type == "RETRY_SAME" and key_id not in same_key_retried:
                        # 同 Key 重试一次（不加入 tried_key_ids）
                        same_key_retried.add(key_id)
                        logger.info(f"[故障转移] Key {label} {resp.status_code} RETRY_SAME，同 Key 重试一次")
                        # 设置 last_error 以防最终失败
                        last_error = resp_body
                        last_error_status = resp.status_code
                        continue  # ← 同 Key 重试（不排除此 Key）

                    # SWITCH_KEY 或 RETRY_SAME 已用完 → 换 Key
                    tried_key_ids.add(key_id)

                    # 记录错误，准备重试
                    last_error = resp_body
                    # 关键修复：上游的 4xx 认证错误不能原样转发给客户端！
                    if resp.status_code in (401, 403):
                        last_error_status = 502
                        last_error = json.dumps({
                            "error": {"message": "上游认证失败，请检查上游 Key 是否有效", "type": "upstream_auth_error"}
                        })
                    elif resp.status_code == 400:
                        last_error_status = 502
                        last_error = json.dumps({
                            "error": {"message": "上游拒绝请求，可能是参数问题", "type": "upstream_bad_request"}
                        })
                    else:
                        last_error_status = resp.status_code
                    continue  # ← 重试下一个 Key

                # ─── 200 成功：开始流式转发（优化项 #11：延迟首输，返回 tuple）───
                start_time = time.time()
                should_retry, error_msg = self._forward_stream_response(
                    resp, upstream_key, sub_key, model, start_time,
                    client_wants_stream, key_id, upstream_request_data,
                    response_mode=response_mode
                )
                self.router.decrement_concurrent(key_id)

                if should_retry:
                    # 流式转发在发送响应头前失败，可换 Key 重试
                    logger.info(f"[故障转移] Key {label} 流式转发失败: {error_msg}，换 Key 重试")
                    tried_key_ids.add(key_id)
                    last_error = json.dumps({"error": {"message": error_msg, "type": "server_error"}})
                    last_error_status = 502
                    continue

                # 成功：重置渐进退避计数器（优化项 #10）
                self.router.reset_cooldown_count(key_id)
                return  # 成功或致命错误已发送给客户端，退出重试循环

            except Exception as e:
                self.router.decrement_concurrent(key_id)
                error_detail = f"Key {label} 转发异常: {type(e).__name__}: {e}"
                logger.error(f"[重试] {error_detail}")
                self._add_log(event="error", sub_key=sub_key, upstream_key=upstream_key,
                              model=model, error=error_detail)

                # 故障转移分类：超时/连接错误 → RETRY_SAME（优化项 #7）
                if isinstance(e, (requests.Timeout, requests.ConnectionError, ConnectionError, socket.timeout, OSError)):
                    error_type = "RETRY_SAME"
                else:
                    error_type = "SWITCH_KEY"

                if error_type == "RETRY_SAME" and key_id not in same_key_retried:
                    same_key_retried.add(key_id)
                    logger.info(f"[故障转移] Key {label} 异常 RETRY_SAME，同 Key 重试一次")
                    last_error = json.dumps({"error": {"message": f"Proxy error: {e}", "type": "server_error"}})
                    last_error_status = 500
                    continue

                tried_key_ids.add(key_id)
                last_error = json.dumps({"error": {"message": f"Proxy error: {e}", "type": "server_error"}})
                last_error_status = 500
                continue  # ← 重试下一个 Key

        # ─── 所有重试都失败 ───
        if last_error:
            error_detail = f"所有重试失败(请求{total_attempts}次，尝试了{len(tried_key_ids)}个Key): {last_error[:300] if isinstance(last_error, str) else last_error[:300]}"
            logger.error(f"[失败] {error_detail}")
            if last_upstream_error_log:
                logger.error(
                    "[失败详情] last_upstream=%s",
                    _safe_json_for_log(last_upstream_error_log, limit=8000),
                )
            self._add_log(event="error", sub_key=sub_key, model=model, error=error_detail)
            # 优化项 #9：429 附带 Retry-After 头
            if last_error_status == 429 and last_cooldown_secs > 0:
                self.send_response(last_error_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", str(last_cooldown_secs))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(last_error if isinstance(last_error, bytes) else last_error.encode())
            else:
                self.send_response(last_error_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(last_error if isinstance(last_error, bytes) else last_error.encode())
        else:
            error_detail = "无可用的上游 Key（Key池为空或全部耗尽/冷却中）"
            logger.error(f"[失败] {error_detail}")
            self._add_log(event="error", sub_key=sub_key, model=model, error=error_detail)
            self._send_json(503, {"error": {"message": "No available upstream keys", "type": "server_error"}})

    def _compress_context_with_ai(self, request_data: dict, upstream_key: dict,
                                     model: str, upstream_url: str) -> bool:
        """用 AI 生成对话摘要，替换旧消息。

        保留 system 消息 + 最近 5 条对话，旧消息发给上游 AI 生成摘要。
        返回 True 表示压缩成功（request_data 已被修改）。
        """
        messages = request_data.get("messages", [])
        if len(messages) <= 8:
            return False

        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        if len(non_system) <= 5:
            return False

        recent = non_system[-5:]
        old = non_system[:-5]

        # 旧消息转纯文本（去掉图片 base64）
        old_text_parts = []
        for msg in old:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                        elif part.get("type") in ("image_url", "image", "input_image"):
                            text_parts.append("[图片]")
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = " ".join(text_parts)
            if content:
                old_text_parts.append(f"[{role}] {content}")

        old_text = "\n".join(old_text_parts)
        if not old_text.strip() or len(old_text) < 100:
            return False

        if len(old_text) > 12000:
            old_text = old_text[:12000] + "\n...(已截断)"

        summary_request = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个对话摘要助手。请将用户提供的对话历史总结为简洁的要点，保留关键技术信息、用户意图和上下文。用中文输出，不超过500字。"},
                {"role": "user", "content": f"请总结以下对话历史的要点：\n\n{old_text}"},
            ],
            "stream": True,
            "max_tokens": 1000,
        }

        try:
            api_key = upstream_key.get("api_key", "")
            resp = self.router._get_session(upstream_url).post(
                f"{upstream_url}{UPSTREAM_CHAT_PATH}",
                json=summary_request,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {api_key}",
                },
                timeout=30,
                stream=True,
            )
            if resp.status_code != 200:
                logger.warning(f"[压缩] 摘要请求失败: status={resp.status_code}")
                return False

            content_parts = []
            for line in resp.iter_lines():
                if not line:
                    continue
                line_str = line.decode("utf-8", errors="replace")
                if line_str.startswith("data: "):
                    data_str = line_str[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {})
                            if delta.get("content"):
                                content_parts.append(delta["content"])
                    except json.JSONDecodeError:
                        pass

            summary = "".join(content_parts).strip()
            if not summary:
                logger.warning("[压缩] AI摘要返回空内容")
                return False

            new_messages = system_msgs + [
                {"role": "system", "content": f"[以下是之前对话的摘要]\n{summary}"}
            ] + recent
            old_count = len(messages)
            request_data["messages"] = new_messages
            logger.info(f"[压缩] AI摘要成功, 摘要长度={len(summary)}, 消息数 {old_count}→{len(new_messages)}")
            return True

        except Exception as e:
            logger.warning(f"[压缩] AI摘要异常: {e}")
            return False

    def _truncate_context(self, request_data: dict, keep_recent: int = 6) -> int:
        """粗暴截断：保留 system + 最近 N 条非system消息。返回截掉的消息数。"""
        messages = request_data.get("messages", [])
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        old_count = len(messages)
        keep = min(keep_recent, len(non_system))
        new_messages = system_msgs + non_system[-keep:]
        request_data["messages"] = new_messages
        removed = old_count - len(new_messages)
        logger.info(f"[截断] 粗暴截断: {old_count}→{len(new_messages)} 条消息, 截掉{removed}条")
        return removed

    def _handle_context_too_long(self, request_data: dict, upstream_key: dict,
                                   model: str, upstream_url: str,
                                   context_compressed: list) -> str:
        """处理上下文超长：先尝试AI摘要，失败则粗暴截断。

        Args:
            context_compressed: [bool] 单元素列表，标记是否已压缩过
        Returns:
            "compressed" / "truncated" / "failed"（已压缩过，不能再压缩）
        """
        if context_compressed[0]:
            return "failed"

        if self._compress_context_with_ai(request_data, upstream_key, model, upstream_url):
            context_compressed[0] = True
            return "compressed"

        self._truncate_context(request_data)
        context_compressed[0] = True
        return "truncated"

    def _detect_stream_error(self, chunk_str: str) -> Optional[str]:
        """扫描 SSE chunk 检测错误（优化项 #4）

        Returns:
            "context_too_long" - 上下文超长
            "stream_error" - 首 chunk 包含错误事件
            None - 无错误
        """
        if not chunk_str or not chunk_str.strip():
            return None
        chunk_lower = chunk_str.lower()
        # 上下文超长关键词
        for keyword in ("context_length_exceeded", "input length too long",
                         "context window", "maximum context length"):
            if keyword in chunk_lower:
                return "context_too_long"
        # 错误事件检测（首 chunk 延迟首输用）
        if '"error"' in chunk_str:
            try:
                for line in chunk_str.split("\n"):
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str and data_str != "[DONE]":
                            chunk_json = json.loads(data_str)
                            if chunk_json.get("error"):
                                err_msg = str(chunk_json["error"].get("message", "")).lower()
                                for keyword in ("context_length_exceeded", "input length too long",
                                                "context window", "maximum context length"):
                                    if keyword in err_msg:
                                        return "context_too_long"
                                return "stream_error"
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return None

    def _extract_usage(self, chunk_str: str, usage_dict: dict):
        """从 SSE chunk 中提取 usage 统计到 usage_dict（原地更新）"""
        try:
            if '"usage"' in chunk_str and '"prompt_tokens"' in chunk_str:
                for line in chunk_str.split("\n"):
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str and data_str != "[DONE]":
                            chunk_json = json.loads(data_str)
                            usage = chunk_json.get("usage")
                            if usage and isinstance(usage, dict) and usage.get("prompt_tokens"):
                                usage_dict.clear()
                                usage_dict.update(usage)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    def _forward_stream_response(self, resp, upstream_key, sub_key, model, start_time,
                                  client_wants_stream, key_id, request_data=None,
                                  response_mode: str = "chat"):
        """处理上游 200 响应的流式/非流式转发

        优化项：
        - #10 首字节超时(10s) + keep-alive 心跳(15s) + 空闲超时(60s)
        - #11 延迟首输：拿到首个 chunk 检测异常后才写响应头，异常返回 should_retry=True
        - #12 客户端断开后继续 drain 上游（只为拿 usage）
        - #13 首字时间记录
        - #15 非流式 usage 原样透传
        - #4 流式上下文超长检测

        Returns:
            tuple(should_retry: bool, error_msg: str)
            - should_retry=True: 可重试错误，响应头未发送，调用方应换 Key 重试
            - should_retry=False: 成功或致命错误已发送给客户端
        """
        sub_key_id = sub_key.get("key_id", "")
        last_usage = {}
        first_token_ms = None
        client_disconnected = False
        response_id = f"resp_{secrets.token_hex(16)}"
        response_stream_text_parts = []
        _chunk_count = 0       # 接收的 chunk 总数
        _has_usage_chunk = False  # 是否收到包含 usage 的 chunk
        _last_chunk_preview = ""  # 最后一个 chunk 的预览（排查 usage 丢失）

        # 提取底层 socket 用于 select 超时控制（优化项 #10）
        _stream_sock = None
        try:
            _stream_sock = resp.raw._fp.fp._sock
        except (AttributeError, OSError):
            pass

        if client_wants_stream:
            # ─── 直接流式转发，不再用 select（避免和 iter_content 缓冲冲突导致卡顿）───
            # 保存生成器引用，首 chunk 和后续续读用同一个生成器
            _stream_gen = resp.iter_content(chunk_size=4096)

            # 读首个 chunk（10s 超时通过 socket 设置）
            if _stream_sock:
                try:
                    _stream_sock.settimeout(10.0)
                except (AttributeError, OSError):
                    pass

            try:
                first_chunk_data = next(_stream_gen, None)
            except (socket.timeout, OSError) as e:
                logger.warning(f"[代理] 首 chunk 超时(10s), key={upstream_key.get('label','')}, err={e}")
                return (True, "first_byte_timeout")
            except Exception as e:
                logger.warning(f"[代理] 首 chunk 读取异常: {e}")
                return (True, "first_chunk_error")

            if first_chunk_data is None:
                logger.warning(f"[代理] 上游返回空响应, key={upstream_key.get('label','')}")
                return (True, "empty_response")

            first_chunk_str = first_chunk_data.decode("utf-8", errors="replace")

            # 检测首 chunk 中的错误（优化项 #4/#11）
            error_type = self._detect_stream_error(first_chunk_str)
            if error_type == "context_too_long":
                # Return a WorkBuddy-recognizable prompt-too-long error so the client
                # can run its own compact flow.
                self._send_json(400, {
                    "code": 11115,
                    "msg": "input length too long",
                    "error": {
                        "message": "input length too long",
                        "type": "context_length_exceeded",
                        "code": 11115,
                    }
                })
                self._add_log(event="end", sub_key=sub_key, upstream_key=upstream_key,
                              model=model, duration_ms=int((time.time() - start_time) * 1000),
                              error="context_too_long")
                return (False, "")
            if error_type == "stream_error":
                # 首 chunk 包含错误事件，换 Key 重试
                logger.warning(f"[代理] 首 chunk 包含错误事件: {first_chunk_str[:200]}")
                return (True, "first_chunk_error_event")

            # 首 chunk 正常，记录首字时间（优化项 #13）
            first_token_ms = int((time.time() - start_time) * 1000)
            logger.info(f"[代理] 首字时间 {first_token_ms}ms, key={upstream_key.get('label','')}")

            # 从首 chunk 提取 usage
            self._extract_usage(first_chunk_str, last_usage)
            _chunk_count = 1
            if '"usage"' in first_chunk_str:
                _has_usage_chunk = True

            # 发送响应头（现在才发送 — 延迟首输）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.flush()

            # 发送首 chunk
            try:
                if response_mode == "responses":
                    self.request.sendall(_sse_event("response.created", {
                        "type": "response.created",
                        "response": _build_responses_object("", model, response_id, {}),
                    }))
                    self.request.sendall(_sse_event("response.in_progress", {
                        "type": "response.in_progress",
                        "response_id": response_id,
                    }))
                    for event_data in _responses_stream_events_from_chat_chunk(
                        first_chunk_str, response_id, response_stream_text_parts
                    ):
                        self.request.sendall(event_data)
                else:
                    self.request.sendall(first_chunk_data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                client_disconnected = True
                logger.info("[代理] 客户端断开连接（首 chunk），继续 drain 上游")

            # 切换 socket 超时为 60s（空闲超时）
            if _stream_sock:
                try:
                    _stream_sock.settimeout(60.0)
                except (AttributeError, OSError):
                    pass

            # ─── 继续转发后续 chunk（统一用 iter_content 生成器，不再用 select）───
            # select + raw.read 和 iter_content 缓冲冲突会导致卡顿，统一用生成器
            last_data_time = time.time()
            for chunk in _stream_gen:
                if chunk:
                    chunk_str = chunk.decode("utf-8", errors="replace")
                    _chunk_count += 1
                    self._extract_usage(chunk_str, last_usage)
                    if '"usage"' in chunk_str and '"prompt_tokens"' in chunk_str:
                        _has_usage_chunk = True
                        _last_chunk_preview = chunk_str[-300:] if len(chunk_str) > 300 else chunk_str
                    if not client_disconnected:
                        try:
                            if response_mode == "responses":
                                for event_data in _responses_stream_events_from_chat_chunk(
                                    chunk_str, response_id, response_stream_text_parts
                                ):
                                    self.request.sendall(event_data)
                            else:
                                self.request.sendall(chunk)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            client_disconnected = True
                            logger.info("[代理] 客户端断开连接，继续 drain 上游（优化项 #12）")
                    last_data_time = time.time()

            if response_mode == "responses" and not client_disconnected:
                try:
                    completed = _build_responses_object(
                        "".join(response_stream_text_parts),
                        model,
                        response_id,
                        last_usage,
                    )
                    self.request.sendall(_sse_event("response.output_text.done", {
                        "type": "response.output_text.done",
                        "response_id": response_id,
                        "item_id": "msg_0",
                        "output_index": 0,
                        "content_index": 0,
                        "text": completed.get("output_text", ""),
                    }))
                    self.request.sendall(_sse_event("response.completed", {
                        "type": "response.completed",
                        "response": completed,
                    }))
                    self.request.sendall(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    client_disconnected = True

            self.close_connection = True

            # 记录 usage 提取结果（排查上下文超限问题）
            if last_usage:
                logger.info(f"[代理] 流式完成 chunks={_chunk_count}, usage提取成功: "
                           f"prompt_tokens={last_usage.get('prompt_tokens',0)}, "
                           f"completion_tokens={last_usage.get('completion_tokens',0)}, "
                           f"total_tokens={last_usage.get('total_tokens',0)}, "
                           f"key={upstream_key.get('label','')}")
            else:
                logger.warning(f"[代理] 流式完成 chunks={_chunk_count} 但 usage 为空! "
                              f"has_usage_chunk={_has_usage_chunk}, "
                              f"WorkBuddy 无法获取上下文大小→不会触发压缩, "
                              f"key={upstream_key.get('label','')}")
                if _last_chunk_preview:
                    logger.info(f"[代理] 最后含usage的chunk预览: {_last_chunk_preview}")
                elif _chunk_count > 0:
                    logger.warning(f"[代理] 所有 {_chunk_count} 个 chunk 中均未找到 usage 字段；"
                                  f"v1.6.6 WorkBuddy Relay 不主动添加 stream_options")
            # 流式转发到此结束，fall through 到下方的统计代码（else 块被跳过）

        else:
            # ─── 非流式：收集完整 SSE 响应，拼装成标准 JSON 返回 ───
            # 设置首字节超时
            if _stream_sock:
                try:
                    _stream_sock.settimeout(10.0)
                except (AttributeError, OSError):
                    pass

            content_parts = []
            reasoning_parts = []
            model_name = model
            chat_id = ""
            first_chunk_received = False

            for line in resp.iter_lines():
                if not line:
                    continue
                line_str = line.decode("utf-8", errors="replace")

                # 首字时间记录（优化项 #13/#16）
                if not first_chunk_received:
                    first_chunk_received = True
                    first_token_ms = int((time.time() - start_time) * 1000)
                    logger.info(f"[代理] 非流式首字时间 {first_token_ms}ms, key={upstream_key.get('label','')}")
                    # 切换为 60s 超时
                    if _stream_sock:
                        try:
                            _stream_sock.settimeout(60.0)
                        except (AttributeError, OSError):
                            pass

                if line_str.startswith("data: "):
                    data_str = line_str[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk_data = json.loads(data_str)
                        chat_id = chunk_data.get("id", chat_id)
                        model_name = chunk_data.get("model", model_name)
                        # 捕获 usage（最后一个 chunk 包含 token/credit 统计）
                        usage = chunk_data.get("usage")
                        if usage and isinstance(usage, dict) and usage.get("prompt_tokens"):
                            last_usage = usage
                        for choice in chunk_data.get("choices", []):
                            delta = choice.get("delta", {})
                            if "content" in delta and delta["content"]:
                                content_parts.append(delta["content"])
                            if "reasoning_content" in delta and delta["reasoning_content"]:
                                reasoning_parts.append(delta["reasoning_content"])
                    except json.JSONDecodeError:
                        pass

            full_content = "".join(content_parts)
            full_reasoning = "".join(reasoning_parts)
            result = {
                "id": chat_id or f"chatcmpl-{secrets.token_hex(8)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_name,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": full_content,
                        "reasoning_content": full_reasoning,
                    },
                    "finish_reason": "stop",
                }],
                # 优化项 #15：usage 原样透传（保留 cached_tokens/credit 等扩展字段）
                "usage": last_usage if last_usage else {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
            if response_mode == "responses":
                result = _build_responses_object(
                    full_content,
                    model_name,
                    response_id,
                    last_usage if last_usage else {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                    reasoning=full_reasoning,
                )

            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                client_disconnected = True

        # ─── 异步更新统计 ───
        duration_ms = int((time.time() - start_time) * 1000)
        logger.debug(f"[代理] 请求完成, 总耗时 {duration_ms}ms, 首字 {first_token_ms}ms")

        def _update_stats():
            try:
                prompt_t = last_usage.get("prompt_tokens", 0)
                completion_t = last_usage.get("completion_tokens", 0)
                total_t = last_usage.get("total_tokens", 0)
                cached_t = last_usage.get("cached_tokens", 0) or last_usage.get("prompt_cache_hit_tokens", 0)
                credit = last_usage.get("credit", 0.0)

                # 上游 Key 统计（原子递增）
                self.db.increment_upstream_key_stats(
                    key_id,
                    prompt_tokens=prompt_t,
                    completion_tokens=completion_t,
                    total_tokens=total_t,
                    cached_tokens=cached_t,
                    credits=credit,
                )

                # 实时扣除积分余额（本地估算，5分钟查分时用真实值修正）
                if credit > 0:
                    self.db.deduct_key_points(key_id, credit)

                # 子 Key 统计（原子递增）— 透传模式没有真实子Key，跳过
                if sub_key_id != "_passthrough_":
                    self.db.increment_sub_api_key_stats(
                        sub_key_id,
                        prompt_tokens=prompt_t,
                        completion_tokens=completion_t,
                        total_tokens=total_t,
                        cached_tokens=cached_t,
                        credits=credit,
                    )

                # 写请求日志（含首字时间，优化项 #13）
                self.db.add_request_log({
                    "timestamp": time.time(),
                    "sub_key_id": sub_key_id,
                    "sub_key_label": sub_key.get("label", ""),
                    "main_key_id": key_id,
                    "main_key_label": upstream_key.get("label", ""),
                    "model": model,
                    "event": "end",
                    "duration_ms": duration_ms,
                    "prompt_tokens": last_usage.get("prompt_tokens", 0),
                    "completion_tokens": last_usage.get("completion_tokens", 0),
                    "first_token_ms": first_token_ms or 0,
                })

                # 请求完成后异步查分（限频 1 分钟/次）
                self.db.refresh_key_points_if_needed(key_id)
            except Exception as e:
                logger.error(f"[统计] 更新统计失败: {e}")

        threading.Thread(target=_update_stats, daemon=True).start()

        return (False, "")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器 — 每个请求在独立线程中处理，支持并发"""
    daemon_threads = True  # 子线程随主线程退出
    request_queue_size = 128  # 连接队列从默认5增到128，高并发不排队


class ProxyServer:
    """本地 API 代理服务器"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8002, mode: str = "local"):
        self.host = host
        self.port = port
        self.mode = mode  # "local" or "open"
        self.db = ProxyDatabase.get_instance()
        self.router = ProxyRouter(self.db)
        self._server = None
        self._thread = None
        self._running = False

    def start(self) -> bool:
        """启动代理服务"""
        if self._running:
            return True

        try:
            # 设置请求处理器的共享变量
            ProxyRequestHandler.router = self.router
            ProxyRequestHandler.db = self.db
            ProxyRequestHandler.server_mode = self.mode  # "local" or "open"

            self._server = ThreadingHTTPServer((self.host, self.port), ProxyRequestHandler)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            self._running = True
            # 启动后台健康检测线程（优化项 #17）
            self.router.start_health_check()
            logger.info(f"API 代理服务已启动: http://{self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"启动代理服务失败: {e}")
            return False

    def stop(self):
        """停止代理服务"""
        # 停止后台健康检测线程（优化项 #17）
        if self.router:
            self.router.stop_health_check()
        # 退出前刷盘，防止数据丢失
        if self.db:
            self.db.flush_now()
        # 关闭所有上游连接池，释放 TCP 连接
        if self.router:
            self.router.close_all_sessions()
        if self._server:
            self._server.shutdown()       # 停止 serve_forever() 循环
            self._server.server_close()    # 关闭 socket，释放端口
            self._server = None
        self._running = False
        self._thread = None
        logger.info("API 代理服务已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"
