"""Find identifiers a Vue template uses that its <script setup> never defines.

Catches exactly the class of bug the Spotrac removal introduced: a block cut
from the script taking a handler the template still calls, which Vue reports
only at runtime (or silently, as a disabled button).
"""
import pathlib
import re

JS_GLOBALS = {
    "true", "false", "null", "undefined", "Math", "Number", "String", "Boolean",
    "Array", "Object", "JSON", "Date", "console", "window", "document", "parseInt",
    "parseFloat", "isNaN", "Set", "Map", "item", "key", "value", "event", "$event",
    "in", "of", "new", "typeof", "instanceof", "return", "if", "else",
}

for path in sorted(pathlib.Path("frontend/src").glob("*.vue")):
    src = path.read_text(encoding="utf-8")
    m = re.search(r"<script setup>(.*?)</script>", src, re.S)
    t = re.search(r"<template>(.*?)</template>\s*(?:<style|$)", src, re.S)
    if not m or not t:
        print(f"{path.name}: (no script setup / template found)")
        continue
    script, template = m.group(1), t.group(1)

    # Everything the script defines or imports.
    defined = set()
    defined |= set(re.findall(r"(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)", script))
    defined |= set(re.findall(r"async\s+function\s+([A-Za-z_$][\w$]*)", script))
    for imp in re.findall(r"import\s+(.+?)\s+from", script):
        defined |= set(re.findall(r"[A-Za-z_$][\w$]*", imp))
    # defineProps destructuring and v-for bindings
    defined |= set(re.findall(r"defineProps\(\{([^}]*)\}", script, re.S) and
                   re.findall(r"([A-Za-z_$][\w$]*)\s*:", " ".join(
                       re.findall(r"defineProps\(\{([^}]*)\}", script, re.S))) or [])
    defined |= set(re.findall(r"v-for=\"\(?\s*([A-Za-z_$][\w$]*)", template))
    defined |= set(re.findall(r"v-for=\"\([^,]+,\s*([A-Za-z_$][\w$]*)", template))
    defined |= set(re.findall(r"v-slot:?[\w]*=\"([A-Za-z_$][\w$]*)", template))

    def idents(expr):
        """Identifiers in a template expression, ignoring string literals and
        property accesses (`a.b` contributes `a`, never `b`)."""
        expr = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", " ", expr)
        expr = re.sub(r"\.\s*[A-Za-z_$][\w$]*", "", expr)
        return set(re.findall(r"\b([A-Za-z_$][\w$]*)\b(?!\s*:)", expr))

    # Everything the template references in a binding.
    used = set()
    for expr in re.findall(r'(?:@|v-|:)[\w.\-\[\]]+="([^"]*)"', template):
        used |= idents(expr)
    for expr in re.findall(r"\{\{(.*?)\}\}", template, re.S):
        used |= idents(expr)

    missing = sorted(
        u for u in used
        if u not in defined and u not in JS_GLOBALS and not u.isupper()
    )
    # Drop property accesses (x.y -> y) and obvious literals.
    props_after_dot = set(re.findall(r"\.([A-Za-z_$][\w$]*)", template))
    missing = [u for u in missing if u not in props_after_dot]

    print(f"{path.name}: {'OK' if not missing else 'MISSING -> ' + ', '.join(missing)}")
