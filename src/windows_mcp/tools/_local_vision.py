"""Local vision processing backends for Windows-MCP:
1. winocr (Native Windows.Media.Ocr engine)
2. OpenAI-compatible Vision LLM (URL + optional Bearer token + model)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any

import requests
from PIL import Image as PILImage

from windows_mcp.infrastructure.config import CONFIG_FILE, load_config

logger = logging.getLogger(__name__)


def _get_vision_settings() -> dict[str, str | None]:
    """Load vision settings with precedence: environment variables > config.toml > defaults."""
    cfg_vision = None
    try:
        if CONFIG_FILE.exists():
            cfg_vision = load_config(CONFIG_FILE).vision
    except Exception:
        logger.debug("Could not read vision section from config.toml", exc_info=True)

    mode = (
        os.getenv("WINDOWS_MCP_VISION_MODE")
        or (cfg_vision.mode if cfg_vision else None)
        or "image"
    )
    llm_base_url = (
        os.getenv("WINDOWS_MCP_LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or (cfg_vision.llm_base_url if cfg_vision else None)
    )
    llm_api_key = (
        os.getenv("WINDOWS_MCP_LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or (cfg_vision.llm_api_key if cfg_vision else None)
    )
    llm_model = (
        os.getenv("WINDOWS_MCP_LLM_MODEL")
        or (cfg_vision.llm_model if cfg_vision else None)
    )
    ocr_lang = (
        os.getenv("WINDOWS_MCP_OCR_LANG")
        or (cfg_vision.ocr_lang if cfg_vision else None)
        or "pt-BR"
    )
    return {
        "mode": mode,
        "llm_base_url": llm_base_url,
        "llm_api_key": llm_api_key,
        "llm_model": llm_model,
        "ocr_lang": ocr_lang,
    }


def run_winocr(
    pil_image: PILImage.Image,
    *,
    coord_scale: float = 1.0,
    region_offset: tuple[int, int] = (0, 0),
    lang: str | None = None,
) -> dict[str, Any]:
    """Run native Windows.Media.Ocr on a PIL Image and return structured JSON with screen coordinates."""
    started_at = time.perf_counter()
    settings = _get_vision_settings()
    ocr_lang = lang or settings["ocr_lang"] or "pt-BR"

    try:
        import winocr
    except ImportError as exc:
        return {
            "backend": "winocr",
            "error": f"winocr is not installed: {exc}",
        }

    try:
        try:
            raw = winocr.recognize_pil_sync(pil_image, lang=ocr_lang)
        except Exception:
            raw = winocr.recognize_pil_sync(pil_image)

        offset_x, offset_y = region_offset
        lines_out: list[dict[str, Any]] = []
        for line in raw.get("lines", []):
            line_text = (line.get("text") or "").strip()
            if not line_text:
                continue
            words = line.get("words") or []
            if words:
                min_x = min(float(w["bounding_rect"]["x"]) for w in words)
                min_y = min(float(w["bounding_rect"]["y"]) for w in words)
                max_x = max(
                    float(w["bounding_rect"]["x"]) + float(w["bounding_rect"]["width"])
                    for w in words
                )
                max_y = max(
                    float(w["bounding_rect"]["y"]) + float(w["bounding_rect"]["height"])
                    for w in words
                )
                screen_left = round(min_x * coord_scale) + offset_x
                screen_top = round(min_y * coord_scale) + offset_y
                screen_right = round(max_x * coord_scale) + offset_x
                screen_bottom = round(max_y * coord_scale) + offset_y
                center_x = round((screen_left + screen_right) / 2)
                center_y = round((screen_top + screen_bottom) / 2)
                lines_out.append(
                    {
                        "text": line_text,
                        "center": [center_x, center_y],
                        "bbox": [screen_left, screen_top, screen_right, screen_bottom],
                    }
                )
            else:
                lines_out.append({"text": line_text})

        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        return {
            "backend": "winocr",
            "language": ocr_lang,
            "elapsed_ms": elapsed_ms,
            "full_text": (raw.get("text") or "").strip(),
            "line_count": len(lines_out),
            "lines": lines_out,
        }
    except Exception as exc:
        logger.warning("winocr failed", exc_info=True)
        return {
            "backend": "winocr",
            "error": str(exc),
        }


def _resolve_chat_completions_endpoint(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def _discover_default_model(base_url: str, headers: dict[str, str]) -> str:
    """Attempt to query /v1/models on the OpenAI-compatible server if no model was specified."""
    url = base_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        models_url = url[: -len("/chat/completions")] + "/models"
    elif url.endswith("/v1"):
        models_url = f"{url}/models"
    else:
        models_url = f"{url}/v1/models"
    try:
        resp = requests.get(models_url, headers=headers, timeout=5)
        if resp.ok:
            data = resp.json().get("data") or []
            if data and isinstance(data, list) and "id" in data[0]:
                return str(data[0]["id"])
    except Exception:
        pass
    return "default"


def run_openai_vision_llm(
    screenshot_bytes: bytes,
    *,
    image_format: str = "jpeg",
    prompt: str | None = None,
    llm_url: str | None = None,
    llm_token: str | None = None,
    llm_model: str | None = None,
    coord_scale: float = 1.0,
    region_offset: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    """Send the screenshot to an OpenAI-compatible Vision LLM endpoint and return structured output."""
    started_at = time.perf_counter()
    settings = _get_vision_settings()

    base_url = llm_url or settings["llm_base_url"] or "http://localhost:11434/v1"
    api_key = llm_token if llm_token is not None else settings["llm_api_key"]
    model = llm_model or settings["llm_model"]

    endpoint = _resolve_chat_completions_endpoint(base_url)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key and str(api_key).strip():
        headers["Authorization"] = f"Bearer {str(api_key).strip()}"

    if not model:
        model = _discover_default_model(base_url, headers)

    mime_type = "image/jpeg" if image_format.lower() in ("jpg", "jpeg") else "image/png"
    b64_image = base64.b64encode(screenshot_bytes).decode("ascii")

    default_prompt = (
        "Analyze this Windows desktop screenshot and respond in JSON with keys:\n"
        "- 'summary': concise description of the active screen/page state\n"
        "- 'main_text': all readable article/page/dialog text in reading order\n"
        "- 'interactive_elements': key visible buttons/links/inputs with approximate [x, y] image pixel coordinates.\n"
        "Respond in Portuguese unless the content is in another language."
    )
    user_prompt = prompt.strip() if prompt and prompt.strip() else default_prompt
    if coord_scale != 1.0 or region_offset != (0, 0):
        user_prompt += (
            f"\n(Note: image coordinates scale factor to real screen is {coord_scale}x, "
            f"region offset is {region_offset}.)"
        )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{b64_image}"},
                    },
                ],
            }
        ],
        "temperature": 0.1,
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=60)
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        if not resp.ok:
            return {
                "backend": "llm",
                "endpoint": endpoint,
                "model": model,
                "status_code": resp.status_code,
                "elapsed_ms": elapsed_ms,
                "error": resp.text[:1000],
            }
        body = resp.json()
        choices = body.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content = msg.get("content") or ""

        # Try parsing JSON if the model returned a JSON block
        parsed_json = None
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3:
                stripped = "\n".join(lines[1:-1]).strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed_json = json.loads(stripped)
            except Exception:
                parsed_json = None

        result: dict[str, Any] = {
            "backend": "llm",
            "endpoint": endpoint,
            "model": body.get("model", model),
            "elapsed_ms": elapsed_ms,
        }
        if parsed_json is not None:
            result["result"] = parsed_json
        else:
            result["response"] = content
        if "usage" in body:
            result["usage"] = body["usage"]
        return result
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        logger.warning("OpenAI-compatible Vision LLM call failed", exc_info=True)
        return {
            "backend": "llm",
            "endpoint": endpoint,
            "model": model,
            "elapsed_ms": elapsed_ms,
            "error": str(exc),
        }
