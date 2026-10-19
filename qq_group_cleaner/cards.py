"""Read the card guard's public configuration; keep matching semantics identical."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import regex


class CardRuleError(ValueError):
    pass


@lru_cache(maxsize=64)
def compiled(pattern, ignore_case):
    return regex.compile(pattern, regex.IGNORECASE if ignore_case else 0)


@dataclass(frozen=True)
class CardRule:
    pattern: str
    strip_spaces: bool = True
    ignore_case: bool = False
    format_help: str = ""

    def matches(self, card):
        if not isinstance(card, str) or len(card) > 512:
            raise CardRuleError("群名片资料缺失或长度异常，保留成员。")
        text = card.strip() if self.strip_spaces else card
        if not text:
            return False
        try:
            return compiled(self.pattern, self.ignore_case).fullmatch(text, timeout=0.05) is not None
        except (TimeoutError, regex.error) as exc:
            raise CardRuleError("群名片正则匹配异常或超时，停止本次清理，请检查名片规范插件的规则。") from exc


def rule_for_group(source, gid):
    if not isinstance(source, dict) or not isinstance(source.get("groups"), list):
        raise CardRuleError("请先在群名片规范插件中配置这个群的名片规则。")
    rows = [r for r in source["groups"] if isinstance(r, dict) and str(r.get("group_id")) == gid]
    if len(rows) != 1:
        raise CardRuleError("群名片规范插件中缺少本群规则或群号重复，请检查配置。")
    row = rows[0]
    pattern = row.get("pattern", r"(?:大[一二三四五]|研[一二三])-\S{2,40}")
    advanced = row.get("advanced", {})
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 2000 or not isinstance(advanced, dict):
        raise CardRuleError("群名片规范插件的正则或匹配选项无效。")
    strip_spaces, ignore_case = advanced.get("strip_spaces", True), advanced.get("ignore_case", False)
    if not isinstance(strip_spaces, bool) or not isinstance(ignore_case, bool):
        raise CardRuleError("群名片规范插件的匹配选项必须使用开关。")
    try:
        compiled(pattern, ignore_case)
    except regex.error as exc:
        raise CardRuleError("群名片规范插件的正则无效，请修正后再启用清理。") from exc
    label = row.get("format_help", "")
    return CardRule(pattern, strip_spaces, ignore_case, label if isinstance(label, str) else "")


@lru_cache(maxsize=4)
def _read_config(path, signature):
    raw = Path(path).read_bytes()
    if len(raw) > 1024 * 1024:
        raise CardRuleError("群名片规范插件配置文件过大，暂缓清理。")
    return json.loads(raw.decode("utf-8-sig"))


def read_card_guard_config(path):
    try:
        path = Path(path)
        info = path.stat()
        return _read_config(str(path), (info.st_mtime_ns, info.st_size, info.st_ino))
    except (OSError, ValueError, UnicodeError) as exc:
        raise CardRuleError("无法读取群名片规范插件配置，已停止清理，请检查该插件配置文件。") from exc
