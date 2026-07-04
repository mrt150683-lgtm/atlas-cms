from pathlib import Path

from cms.graph_builder import build_graph
from cms.scanner import scan

HELPER = '''\
"""Helper utilities."""


class Base:
    pass


class Greeter(Base):
    """Greets people."""

    def greet(self, name):
        return format_name(name)


def format_name(name):
    """Normalise a name."""
    return name.title()
'''

MAIN = '''\
"""Entry point."""

from pkg.helper import format_name, Greeter


def run():
    g = Greeter()
    print(format_name("bob"))
    helper_shortcut()


def helper_shortcut():
    return format_name("x")
'''


def _project(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helper.py").write_text(HELPER, encoding="utf-8")
    (pkg / "main.py").write_text(MAIN, encoding="utf-8")
    return tmp_path


def test_nodes_and_contains(tmp_path: Path) -> None:
    graph = build_graph(scan(_project(tmp_path)))

    assert graph.has_node("file:pkg/helper.py")
    assert graph.has_node("class:pkg/helper.py::Greeter")
    assert graph.has_node("func:pkg/helper.py::Greeter.greet")
    assert graph.has_node("func:pkg/main.py::run")

    assert graph.edges["file:pkg/helper.py", "class:pkg/helper.py::Greeter"]["type"] == "CONTAINS"
    assert (
        graph.edges["class:pkg/helper.py::Greeter", "func:pkg/helper.py::Greeter.greet"]["type"]
        == "CONTAINS"
    )

    greet = graph.nodes["func:pkg/helper.py::Greeter.greet"]
    assert greet["start_line"] == 11
    assert greet["signature"] == "def greet(self, name)"


def test_imports_calls_inherits(tmp_path: Path) -> None:
    graph = build_graph(scan(_project(tmp_path)))

    assert graph.edges["file:pkg/main.py", "file:pkg/helper.py"]["type"] == "IMPORTS"
    # cross-file call via from-import
    assert (
        graph.edges["func:pkg/main.py::run", "func:pkg/helper.py::format_name"]["type"] == "CALLS"
    )
    # same-file call
    assert (
        graph.edges["func:pkg/main.py::run", "func:pkg/main.py::helper_shortcut"]["type"] == "CALLS"
    )
    # instantiation counts as a call to the class
    assert graph.has_edge("func:pkg/main.py::run", "class:pkg/helper.py::Greeter")
    # inheritance within a file
    assert (
        graph.edges["class:pkg/helper.py::Greeter", "class:pkg/helper.py::Base"]["type"]
        == "INHERITS"
    )


def test_external_import_node(tmp_path: Path) -> None:
    (tmp_path / "solo.py").write_text("import os\n", encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    assert graph.has_node("ext:os")
    assert graph.edges["file:solo.py", "ext:os"]["type"] == "IMPORTS"
