"""
Library scoping by unit type. A canonical library serves a heterogeneous fleet;
each item carries a `types` list saying which unit kinds it applies to (empty =
shared/all). scoped_library() slices the library for one unit's type — this is
what actually gets deployed, so a broadcaster never receives an x410-only item.
"""
from api import models as m


def _lib():
    return m.Library(
        scripts=[
            m.LibraryScript(name="shared.py", content="x", types=[]),
            m.LibraryScript(name="x410_only.py", content="y", types=["x410"]),
            m.LibraryScript(name="bc_only.py", content="z", types=["broadcaster"]),
        ],
        tasks=[
            m.TaskConfig(name="shared_task", command=["python3", "shared.py"]),
            m.TaskConfig(name="x410_task", command=["python3", "x410_only.py"],
                         types=["x410"]),
            m.TaskConfig(name="both_task", command=["python3", "shared.py"],
                         types=["broadcaster", "x410"]),
        ],
        sequences=[
            m.Sequence(id="s1", name="shared_seq", steps=[]),
            m.Sequence(id="s2", name="x410_seq", steps=[], types=["x410"]),
        ],
    )


def test_applies_to_type_empty_is_shared():
    assert m.applies_to_type([], "broadcaster") is True
    assert m.applies_to_type([], "x410") is True


def test_applies_to_type_membership():
    assert m.applies_to_type(["x410"], "x410") is True
    assert m.applies_to_type(["x410"], "broadcaster") is False
    assert m.applies_to_type(["broadcaster", "x410"], "broadcaster") is True


def test_scoped_library_broadcaster():
    lib = m.scoped_library(_lib(), "broadcaster")
    assert {s.name for s in lib.scripts} == {"shared.py", "bc_only.py"}
    assert {t.name for t in lib.tasks} == {"shared_task", "both_task"}
    assert {q.id for q in lib.sequences} == {"s1"}


def test_scoped_library_x410():
    lib = m.scoped_library(_lib(), "x410")
    assert {s.name for s in lib.scripts} == {"shared.py", "x410_only.py"}
    assert {t.name for t in lib.tasks} == {"shared_task", "x410_task", "both_task"}
    assert {q.id for q in lib.sequences} == {"s1", "s2"}


def test_scoped_library_does_not_mutate_source():
    src = _lib()
    m.scoped_library(src, "broadcaster")
    assert len(src.scripts) == 3 and len(src.tasks) == 3 and len(src.sequences) == 2
