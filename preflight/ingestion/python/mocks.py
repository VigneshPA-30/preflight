"""Find mock/patch statements. This is the lying-mock detector's input.

Handled forms (all of them name a thing to replace):
  patch("a.b.f")                 mock.patch / unittest.mock.patch      (string path)
  patch.object(Obj, "f")                                               (object + attribute)
  patch.multiple("a.b", f=..., g=...)                                  (module + keyword names)
  mocker.patch("a.b.f")  mocker.patch.object(Obj, "f")                 (pytest-mock)
  monkeypatch.setattr("a.b.f", v)  monkeypatch.setattr(Obj, "f", v)    (pytest)
Ignored on purpose: patch.dict (data, not functions), patch.stopall.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

DOTTED_PATH = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
# keywords of patch()/patch.multiple() that configure the mock; they are not attribute names
PATCH_OPTIONS = {"new", "spec", "create", "spec_set", "autospec", "new_callable",
                 "return_value", "side_effect", "wraps", "name", "unsafe"}
SETUP_NAMES = {"setUp", "setUpClass", "setup_method", "setup_class", "setup"}


def dotted(node: ast.AST | None) -> str | None:
    """'a.b.c' for a Name/Attribute chain. 'super().x' and 'Foo(args).x' are supported. Else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):          # super().x  or  Foo(args).x
        inner = dotted(node.func)
        if inner is None:
            return None
        parts.append(inner + "()")
    else:
        return None
    return ".".join(reversed(parts))


@dataclass(slots=True)
class RawMock:
    owner: str              # qname of the function (or class) that holds the patch
    line: int
    via: str
    scope: str              # decorator | body | class_decorator | class_setup
    path: str = ""          # string form: "a.b.f"
    obj_expr: str = ""      # object form: dotted expression of the object
    attr: str = ""          # object form: attribute name
    file: str = ""


def _const_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def classify(call: ast.Call, owner: str, scope: str) -> list[RawMock]:
    """Return the patches this call performs (empty list if it is not a patch call)."""
    name = dotted(call.func)
    if not name:
        return []
    parts = name.split(".")
    last = parts[-1]
    prev = parts[-2] if len(parts) > 1 else ""
    line = call.lineno
    out: list[RawMock] = []

    def mk(**kw) -> RawMock:
        return RawMock(owner=owner, line=line, scope=scope, **kw)

    mocker = "mocker" in parts

    if last == "object" and prev == "patch":
        if len(call.args) >= 2 and (a := _const_str(call.args[1])) and (o := dotted(call.args[0])):
            out.append(mk(via=("mocker." if mocker else "") + "patch.object", obj_expr=o, attr=a))
    elif last == "multiple" and prev == "patch":
        target = _const_str(call.args[0]) if call.args else None
        o = dotted(call.args[0]) if call.args and target is None else None
        for kw in call.keywords:
            if not kw.arg or kw.arg in PATCH_OPTIONS:
                continue
            via = ("mocker." if mocker else "") + "patch.multiple"
            if target and DOTTED_PATH.match(target + ".x"):
                out.append(mk(via=via, path=f"{target}.{kw.arg}"))
            elif o:
                out.append(mk(via=via, obj_expr=o, attr=kw.arg))
    elif last == "dict" and prev == "patch":
        return []
    elif last == "patch":
        target = _const_str(call.args[0]) if call.args else None
        if target is None:
            for kw in call.keywords:
                if kw.arg == "target":
                    target = _const_str(kw.value)
        if target and DOTTED_PATH.match(target):  # filters out client.patch("/url")
            out.append(mk(via=("mocker." if mocker else "") + "patch", path=target))
    elif last == "setattr" and prev in {"monkeypatch", "mp"}:
        first = _const_str(call.args[0]) if call.args else None
        if first is not None:
            if DOTTED_PATH.match(first):
                out.append(mk(via="monkeypatch.setattr", path=first))
        elif len(call.args) >= 2 and (a := _const_str(call.args[1])) and (o := dotted(call.args[0])):
            out.append(mk(via="monkeypatch.setattr", obj_expr=o, attr=a))
    return out


def is_mock_call(call: ast.Call) -> bool:
    """True for patch-API calls, so they are not counted as ordinary calls."""
    name = dotted(call.func) or ""
    parts = name.split(".")
    return (parts[-1] == "patch" or name.endswith(("patch.object", "patch.multiple", "patch.dict"))
            or (len(parts) > 1 and parts[-2] in {"monkeypatch", "mp"} and parts[-1].startswith("set")))
