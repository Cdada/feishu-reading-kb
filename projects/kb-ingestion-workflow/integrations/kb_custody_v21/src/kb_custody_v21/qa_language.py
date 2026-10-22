"""Bounded aliases from common terminology or explicit source declarations."""
import re
import unicodedata

# These are names for the same concept, not merely related research topics.
COMMON = (("地理信息系统", "GIS"), ("人工智能", "AI"), ("机器学习", "ML"),
          ("自然语言处理", "NLP"), ("大型语言模型", "大语言模型", "LLM"),
          ("光学字符识别", "OCR"), ("开放街道地图", "OpenStreetMap", "OSM"),
          ("可持续发展目标", "SDGs"))

def declared_aliases(text):
    # Require an explicitly quoted name and declaration; proximity alone is
    # insufficient to turn two source phrases into equivalent concepts.
    pattern = r'[“「]([^”」\n]{2,40})[”」]\s*[（(](?:以下简称|简称|又称)\s*[“「]?([^”」)）\n]{2,20})[”」]?[)）]'
    return [list(pair) for pair in dict.fromkeys(re.findall(pattern, text))][:16]

def variants(text, materials=()):
    text = unicodedata.normalize('NFKC', text)
    groups = list(COMMON)
    for material in materials:
        groups.extend(material.get('aliases', []))
    result = [text]
    for group in groups:
        for term in group:
            pattern = re.escape(unicodedata.normalize('NFKC', term))
            if term.isascii():
                pattern = r'(?<![A-Za-z0-9_])'+pattern+r'(?![A-Za-z0-9_])'
            if not re.search(pattern, text, re.I):
                continue
            for replacement in group:
                candidate = re.sub(pattern, lambda _: replacement, text, flags=re.I)
                if candidate not in result:
                    result.append(candidate)
                if len(result) >= 8:
                    return result
    return result
