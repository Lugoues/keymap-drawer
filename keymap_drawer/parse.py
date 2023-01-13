"""Module to parse QMK/ZMK keymaps to a simplified KeymapData-like format."""
import sys
import re
import json
from io import StringIO
from abc import ABC
from itertools import chain
from copy import deepcopy

import pyparsing as pp
from pcpp.preprocessor import Preprocessor, OutputDirective, Action

from .keymap import Layer, ComboSpec, KeymapData


class KeymapParser(ABC):
    def __init__(self, columns: int | None, remove_prefixes: bool):
        self.columns = columns
        self.remove_prefixes = remove_prefixes
        self._dict_args = {"exclude_defaults": True, "exclude_unset": True, "by_alias": True}

    def rearrange_layer(self, layer_keys):
        if self.columns:
            return [layer_keys[i : i + self.columns] for i in range(0, len(layer_keys), self.columns)]
        return layer_keys


class QmkJsonParser(KeymapParser):
    def __init__(self, columns: int | None, remove_prefixes: bool):
        super().__init__(columns, remove_prefixes)

    def parse(self, path) -> dict:
        prefix_re = re.compile(r"\bKC_")
        mo_re = re.compile(r"MO\((\S+)\)")
        mts_re = re.compile(r"([A-Z_]+)_T\((\S+)\)")
        mtl_re = re.compile(r"MT\((\S+),(\S+)\)")
        lt_re = re.compile(r"LT\((\S+),(\S+)\)")

        with open(path, "rb") if path != "-" else sys.stdin as f:
            raw = json.load(f)

        layout = {}
        if "keyboard" in raw:
            layout["qmk_keyboard"] = raw["keyboard"]
        if "layout" in raw:
            layout["qmk_layout"] = raw["layout"]

        layers = {}
        for ind, layer in enumerate(raw["layers"]):
            parsed_keys = []
            for key in layer:
                if self.remove_prefixes:
                    key = prefix_re.sub("", key)

                if m := mo_re.fullmatch(key):
                    parsed = f"L{m.group(1).strip()}"
                elif m := mts_re.fullmatch(key):
                    parsed = {"t": m.group(2).strip(), "h": m.group(1)}
                elif m := mtl_re.fullmatch(key):
                    parsed = {"t": m.group(2).strip(), "h": m.group(1).strip()}
                elif m := lt_re.fullmatch(key):
                    parsed = {"t": m.group(2).strip(), "h": f"L{m.group(1).strip()}"}
                else:
                    parsed = key

                parsed_keys.append(parsed)

            layer_keys = Layer(keys=parsed_keys).dict(**self._dict_args)["keys"]
            layers[f"L{ind}"] = {"keys": self.rearrange_layer(layer_keys)}

        return {"layout": layout, "layers": layers}


class ZmkKeymapParser(KeymapParser):
    def __init__(self, columns: int | None, remove_prefixes: bool, preprocess: bool):
        super().__init__(columns, remove_prefixes)
        self.preprocess = preprocess
        self._bindings_re = re.compile(r"bindings = <(.*?)>")
        self._keypos_re = re.compile(r"key-positions = <(.*?)>")
        self._layers_re = re.compile(r"layers = <(.*?)>")
        self._label_re = re.compile(r'label = "(.*?)"')

    def _remove_prefix(self, binding):
        if not self.remove_prefixes:
            return binding
        return re.sub(r"&(kp|bt|mt|lt|none|trans|out) +", "", binding)

    def _get_prepped(self, path):
        clean_prep = re.compile(r"^\s*#.*?$", re.MULTILINE)
        with open(path, "r", encoding="utf-8") if path != "-" else sys.stdin as f:
            if self.preprocess:

                def include_handler(*args):
                    raise OutputDirective(Action.IgnoreAndPassThrough)

                preprocessor = Preprocessor()
                preprocessor.line_directive = None
                preprocessor.on_include_not_found = include_handler
                preprocessor.parse(f)
                with StringIO() as f_out:
                    preprocessor.write(f_out)
                    prepped = f_out.getvalue()
            else:
                prepped = f.read()
            return clean_prep.sub("", prepped)

    def _find_nodes_with_name(
        self, parsed: pp.ParseResults, node_name: str | None = None
    ) -> list[tuple[str, pp.ParseResults]]:
        found_nodes = []
        for top_node in parsed:
            if not isinstance(top_node, pp.ParseResults):
                continue
            for elt_p, elt_n in zip(top_node[:-1], top_node[1:]):
                if (
                    isinstance(elt_p, str)
                    and (node_name is None or elt_p == node_name)
                    and isinstance(elt_n, pp.ParseResults)
                ):
                    found_nodes.append((elt_p, elt_n))
        return found_nodes

    def _get_layers(self, parsed) -> dict[str, Layer]:
        layer_parents = self._find_nodes_with_name(parsed, "keymap")
        layer_nodes = chain.from_iterable(self._find_nodes_with_name(node) for node in layer_parents)
        layers = {}
        for node_name, node in layer_nodes:
            try:
                node_str = " ".join(item for item in node.as_list() if isinstance(item, str))
                layer_name = m.group(1) if (m := self._label_re.search(node_str)) else node_name
                layers[layer_name] = Layer(
                    keys=[
                        self._remove_prefix("&" + stripped)
                        for binding in self._bindings_re.search(node_str).group(1).split(" &")
                        if (stripped := binding.strip())
                    ]
                )
            except Exception:
                continue
        return layers

    def _get_combos(self, parsed, layer_names) -> list[ComboSpec]:
        combo_parents = self._find_nodes_with_name(parsed, "combos")
        combo_nodes = chain.from_iterable(self._find_nodes_with_name(node) for node in combo_parents)
        combos = []
        for _, node in combo_nodes:
            try:
                node_str = " ".join(item for item in node.as_list() if isinstance(item, str))
                binding = self._bindings_re.search(node_str).group(1)
                combo = {
                    "k": self._remove_prefix(binding),
                    "p": [int(pos) for pos in self._keypos_re.search(node_str).group(1).split()],
                }
                if m := self._layers_re.search(node_str):
                    combo["l"] = [layer_names[int(layer)] for layer in m.group(1).split()]
                combos.append(ComboSpec(**combo))
            except Exception:
                continue
        return combos

    def parse(self, path) -> dict:
        prepped = self._get_prepped(path)

        unparsed = "{ " + prepped + " };"
        parsed = (
            pp.nested_expr("{", "};")
            .ignore("//" + pp.SkipTo(pp.lineEnd))
            .ignore(pp.c_style_comment)
            .parse_string(unparsed)[0]
        )
        layers = self._get_layers(parsed)
        layer_names = list(layers.keys())
        combos = self._get_combos(parsed, layer_names)

        layers = KeymapData.assign_combos_to_layers({"layers": layers, "combos": combos})["layers"]
        layers_dict = {name: layer.dict(**self._dict_args) for name, layer in layers.items()}
        for name, layer in layers_dict.items():
            layer["keys"] = self.rearrange_layer(layer["keys"])

        return {"layers": layers_dict}
